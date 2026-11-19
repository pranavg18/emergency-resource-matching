"""Map-based web client: `python -m client.web`, then open http://localhost:5050.

A local client, like the CLI, not a service in the cluster. It serves one page
and forwards that page's API calls to the same endpoints the CLI uses (the
Ingress or Compose gateway on :8088, and the coordinator on :8008). The
browser therefore only ever talks to this one origin: no cross-origin requests,
and nothing in the cluster or the services changes.

  /api/requests, /api/resources, /api/match/...  ->  the public API (--api)
  /api/cluster                                   ->  coordinator /cluster
  /api/all-requests                              ->  every stored request, read
                                                     through the coordinator

The public API has no "list requests" endpoint (only GET /requests/{id}), so
the map reads the request list from storage through the coordinator, as
client.check does. That path is read-only; every change goes through the
services.

Other web pages open in the same browser can also reach this loopback port, so
the proxy only answers its own page: the Host header must name this machine
(which defeats DNS rebinding), a write that carries an Origin must come from
this page's origin, and a write must be sent as JSON. A cross-site page cannot
send a JSON body without a CORS preflight, which this server never approves.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
from typing import Iterable, Optional
from urllib.parse import urlsplit

import requests
from flask import Flask, Response, jsonify, request, send_from_directory

STATIC_DIR = Path(__file__).with_name("web_static")
PROXIED_PREFIXES = ("requests", "resources", "match")
UPSTREAM_TIMEOUT_S = 15
MAX_LISTED_REQUESTS = 500
CLOSED_STATUSES = ("COMPLETED", "CANCELLED")
LOOPBACK_HOSTS = ("localhost", "127.0.0.1", "::1")


def _error(status: int, code: str, message: str):
    return jsonify(error={"code": code, "message": message}), status


def _hostname(netloc: str) -> str:
    return (urlsplit(f"//{netloc}").hostname or "").lower()


def create_app(api_url: str = "http://localhost:8088", coordinator_url: str = "http://localhost:8008",
               session: Optional[requests.Session] = None, allowed_hosts: Iterable[str] = ()) -> Flask:
    api_url, coordinator_url = api_url.rstrip("/"), coordinator_url.rstrip("/")
    http = session or requests.Session()
    hosts = {*LOOPBACK_HOSTS, *(h.lower() for h in allowed_hosts)}
    app = Flask(__name__, static_folder=None)
    app.json.sort_keys = False

    @app.before_request
    def only_this_page():
        if _hostname(request.host) not in hosts:
            return _error(403, "FORBIDDEN_HOST", f"this client only answers on {', '.join(sorted(hosts))}")
        if request.method in ("POST", "PUT"):
            origin = request.headers.get("Origin")
            if origin is not None and urlsplit(origin).netloc.lower() != request.host.lower():
                return _error(403, "FORBIDDEN_ORIGIN", "writes are accepted only from this client's own page")
            if request.mimetype != "application/json":
                return _error(415, "UNSUPPORTED_MEDIA_TYPE", "send the body as application/json")
        return None

    def upstream_unavailable(what: str, exc: Exception):
        return _error(502, "UPSTREAM_UNAVAILABLE", f"cannot reach the {what}: {exc.__class__.__name__}. "
                      "Is the port-forward (or Compose stack) running?")

    @app.get("/")
    def index():
        return send_from_directory(STATIC_DIR, "index.html")

    @app.get("/static/<path:name>")
    def static_file(name: str):
        return send_from_directory(STATIC_DIR, name)

    @app.get("/healthz")
    def healthz():
        return {"status": "ok"}

    @app.get("/api/config")
    def config():
        return {"api_url": api_url, "coordinator_url": coordinator_url,
                "max_radius_km": float(os.environ.get("MAX_RADIUS_KM", "50"))}

    @app.route("/api/<path:path>", methods=["GET", "POST", "PUT"])
    def proxy(path: str):
        """Forward one call to the public API and relay its answer unchanged."""
        if path.split("/", 1)[0] not in PROXIED_PREFIXES:
            return _error(404, "NOT_FOUND", f"no route for {request.method} /api/{path}")
        headers = {"Content-Type": "application/json"}
        if "X-Request-ID" in request.headers:
            headers["X-Request-ID"] = request.headers["X-Request-ID"]
        try:
            reply = http.request(request.method, f"{api_url}/{path}", params=request.args,
                                 data=request.get_data() or None, headers=headers,
                                 timeout=UPSTREAM_TIMEOUT_S)
        except requests.RequestException as exc:
            return upstream_unavailable("API", exc)
        relayed = {k: v for k, v in reply.headers.items() if k.lower() in ("content-type", "x-request-id")}
        return Response(reply.content, status=reply.status_code, headers=relayed)

    @app.get("/api/cluster")
    def cluster():
        try:
            reply = http.get(f"{coordinator_url}/cluster", timeout=UPSTREAM_TIMEOUT_S)
        except requests.RequestException as exc:
            return upstream_unavailable("coordinator", exc)
        return Response(reply.content, status=reply.status_code,
                        headers={"Content-Type": reply.headers.get("Content-Type", "application/json")})

    @app.get("/api/all-requests")
    def all_requests():
        """Stored requests read through the coordinator: open ones first, then newest first.

        The list is capped, and closed requests are the ones dropped, so an old
        request that still needs action never disappears behind newer closed ones.
        """
        try:
            reply = http.get(f"{coordinator_url}/data", params={"prefix": "request:"}, timeout=UPSTREAM_TIMEOUT_S)
        except requests.RequestException as exc:
            return upstream_unavailable("coordinator", exc)
        if reply.status_code != 200:
            return Response(reply.content, status=reply.status_code,
                            headers={"Content-Type": reply.headers.get("Content-Type", "application/json")})
        values = []
        for record in reply.json().get("records", []):
            try:
                value = json.loads(record["value"])
            except (KeyError, TypeError, ValueError):
                continue
            # Skip anything that is not a request record (e.g. left by another tool).
            if isinstance(value, dict) and isinstance(value.get("id"), str) and isinstance(value.get("location"), dict):
                values.append(value)
        values.sort(key=lambda v: str(v.get("created_at", "")), reverse=True)
        values.sort(key=lambda v: v.get("status") in CLOSED_STATUSES)  # stable: open first, each newest first
        open_count = sum(v.get("status") not in CLOSED_STATUSES for v in values)
        return {"count": len(values), "open": open_count, "requests": values[:MAX_LISTED_REQUESTS],
                "truncated": len(values) > MAX_LISTED_REQUESTS}

    return app


def main() -> None:
    parser = argparse.ArgumentParser(description="Map-based web client for the emergency matching system")
    parser.add_argument("--api", default=os.environ.get("API_URL", "http://localhost:8088"),
                        help="public API: the Ingress or Compose gateway (default %(default)s)")
    parser.add_argument("--coordinator", default="http://localhost:8008",
                        help="coordinator, for the cluster panel and request list (default %(default)s)")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=5050)
    args = parser.parse_args()
    app = create_app(args.api, args.coordinator, allowed_hosts=[args.host])
    print(f"Open http://{args.host}:{args.port}  (API {args.api}, coordinator {args.coordinator})", flush=True)
    app.run(host=args.host, port=args.port, threaded=True)


if __name__ == "__main__":
    main()
