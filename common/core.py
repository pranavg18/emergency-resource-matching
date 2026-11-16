"""Shared domain validation and HTTP error contract."""
import hashlib
import json
import logging
import math
import time
import uuid
from contextvars import ContextVar
from datetime import datetime, timezone

from flask import Flask, g, jsonify, request
from werkzeug.exceptions import HTTPException

TYPES = {"water", "food", "medicine", "transportation", "shelter"}
correlation = ContextVar("correlation_id", default="background")
CLOSED = {"COMPLETED", "CANCELLED"}
OPEN = {"PENDING", "UNMATCHED"}
TRANSITIONS = {
    "PENDING": {"MATCHED", "UNMATCHED", "CANCELLED"},
    "UNMATCHED": {"MATCHED", "CANCELLED"},
    "MATCHED": {"IN_PROGRESS", "CANCELLED"},
    "IN_PROGRESS": {"COMPLETED", "CANCELLED"},
    "COMPLETED": set(), "CANCELLED": set(),
}


class APIError(Exception):
    def __init__(self, message, status=400):
        super().__init__(message)
        self.status = status


class Conflict(APIError):
    def __init__(self, message="version conflict"):
        super().__init__(message, 409)


class Unavailable(APIError):
    def __init__(self, message="storage unavailable"):
        super().__init__(message, 503)


def now():
    return datetime.now(timezone.utc).isoformat()


def shard_for(key):
    return int(hashlib.md5(key.encode(), usedforsecurity=False).hexdigest(), 16) % 3


def body():
    value = request.get_json(silent=True)
    if not isinstance(value, dict):
        raise APIError("body must be a JSON object")
    return value


def integer(value, name, minimum=0):
    if type(value) is not int or value < minimum:
        raise APIError(f"{name} must be an integer >= {minimum}")
    return value


def validate_location(loc):
    if not isinstance(loc, dict):
        raise APIError("location must contain lat and lon")
    for field, limit in (("lat", 90), ("lon", 180)):
        x = loc.get(field)
        if type(x) not in (int, float) or not math.isfinite(x) or abs(x) > limit:
            raise APIError(f"location.{field} must be between {-limit} and {limit}")
    if not isinstance(loc.get("label", ""), str):
        raise APIError("location.label must be a string")
    return {"lat": loc["lat"], "lon": loc["lon"], "label": loc.get("label", "")}


def new_record(data, resource=False):
    if not isinstance(data.get("type"), str) or data["type"] not in TYPES:
        raise APIError(f"type must be one of {sorted(TYPES)}")
    value = {"id": str(uuid.uuid4()), "type": data["type"],
             "location": validate_location(data.get("location")),
             "created_at": now(), "updated_at": now()}
    if resource:
        n = integer(data.get("availability"), "availability")
        value.update(availability=n, status="AVAILABLE" if n else "BUSY",
                     assigned_request_ids={})
    else:
        if not isinstance(data.get("details", ""), str):
            raise APIError("details must be a string")
        value.update(details=data.get("details", ""), status="PENDING", queued=False,
                     candidate_resource_id=None, assigned_resource_id=None, match_attempt=0)
    return value


def transition(current, target, client=False):
    if not isinstance(target, str) or target not in TRANSITIONS or (client and target in {"MATCHED", "UNMATCHED"}):
        raise APIError("status cannot be set by this operation")
    if current != target and target not in TRANSITIONS[current]:
        raise Conflict(f"illegal transition: {current} -> {target}")


def log(event, **fields):
    logging.getLogger("emergency").info(json.dumps({"event": event, **fields}))


def app_base(name, readiness=None):
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    logging.getLogger("pika").setLevel(logging.WARNING)
    app = Flask(name)
    app.config["MAX_CONTENT_LENGTH"] = 1024 * 1024

    @app.before_request
    def before():
        g.correlation_id = request.headers.get("X-Request-ID", str(uuid.uuid4()))[:128]
        g.started = time.monotonic()

    @app.after_request
    def after(response):
        response.headers["X-Request-ID"] = g.correlation_id
        if request.path not in {"/healthz", "/readyz"}:
            log("http", service=name, method=request.method, path=request.path,
                status=response.status_code, correlation_id=g.correlation_id,
                duration_ms=round((time.monotonic() - g.started) * 1000, 2))
        return response

    @app.errorhandler(APIError)
    def api_error(exc):
        return jsonify(error=str(exc)), exc.status

    @app.errorhandler(HTTPException)
    def http_error(exc):
        return jsonify(error=exc.description), exc.code

    @app.get("/healthz")
    def health():
        return {"status": "ok", "service": name}

    @app.get("/readyz")
    def ready():
        if readiness:
            readiness()
        return {"status": "ready"}

    return app
