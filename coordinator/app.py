"""Flask app for the coordinator. gunicorn runs `coordinator.app:create_app()`."""

from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Optional

import requests
from flask import Flask

from . import validation, web
from .config import NODE_CALL_TIMEOUT_S, SHARD_COUNT, CoordinatorConfig
from .monitor import FailureDetector
from .registry import DOWN, NodeInfo, Registry, RegistryError, UnknownNode, describe
from .router import Router
from .web import ApiError, invalid, json_body

log = logging.getLogger(__name__)

MAX_NODE_ID_LENGTH = 64


def _node_id(body: dict) -> str:
    node_id = body.get("node_id")
    if not isinstance(node_id, str) or not node_id.strip() or len(node_id) > MAX_NODE_ID_LENGTH:
        raise invalid(f"node_id must be a non-empty string of at most {MAX_NODE_ID_LENGTH} characters")
    return node_id


def _int_field(body: dict, name: str, *, default: Any = None, low: int = 0, high: Optional[int] = None) -> int:
    value = body.get(name, default)
    if isinstance(value, bool) or not isinstance(value, int) or value < low or (high is not None and value > high):
        bounds = f"between {low} and {high}" if high is not None else f">= {low}"
        raise invalid(f"{name} must be an integer {bounds}")
    return value


def _url(body: dict) -> str:
    url = body.get("url")
    if not isinstance(url, str) or not url.startswith(("http://", "https://")):
        raise invalid("url must be an http(s) URL")
    return url.rstrip("/")


def _sync_requested(node_id: str) -> None:
    # Recovery sync arrives with feat/recovery-sync. Until then the node
    # stays SYNCING and is never read from, which is the safe default.
    log.info("sync requested for %s (recovery sync not implemented yet)", node_id)


def create_app(config: Optional[CoordinatorConfig] = None) -> Flask:
    config = config or CoordinatorConfig.from_env()
    web.configure_logging(config.log_level, service="coordinator")

    registry = Registry(SHARD_COUNT, on_needs_sync=_sync_requested)
    pool = ThreadPoolExecutor(max_workers=8, thread_name_prefix="node-call")
    router = Router(registry, pool)
    # gunicorn calls create_app() inside its single worker, so the detector
    # runs in the process that owns the registry.
    FailureDetector(registry, router, config.heartbeat_interval_ms, config.heartbeat_timeout_ms).start()

    app = Flask(__name__)
    web.install(app)

    @app.get("/healthz")
    def healthz():
        return {"status": "ok"}

    @app.get("/readyz")
    def readyz():
        # The coordinator holds no state it must load first: ready once serving.
        return {"status": "ready"}

    @app.post("/internal/nodes/register")
    def register_node():
        body = json_body()
        try:
            node = registry.register(
                node_id=_node_id(body),
                shard=_int_field(body, "shard", low=0, high=SHARD_COUNT - 1),
                url=_url(body),
                max_version=_int_field(body, "max_version", default=0),
            )
        except RegistryError as err:
            raise invalid(str(err)) from None
        return {
            "node_id": node.node_id,
            "state": node.state,
            "role": node.role,
            "heartbeat_interval_ms": config.heartbeat_interval_ms,
        }

    @app.post("/internal/nodes/heartbeat")
    def heartbeat():
        node_id = _node_id(json_body())
        try:
            node = registry.heartbeat(node_id)
        except UnknownNode:
            raise ApiError(404, "UNKNOWN_NODE", f"{node_id} is not registered; register again") from None
        return {"state": node.state, "role": node.role}

    # --- Data API: the only storage interface the services see ----------

    @app.put("/data/<key>")
    def put_data(key: str):
        validation.checked_key(key)
        body = json_body()
        value = validation.checked_value(body)
        return router.put(key, value, validation.body_version(body))

    @app.get("/data/<key>")
    def get_data(key: str):
        return router.get(validation.checked_key(key))

    @app.delete("/data/<key>")
    def delete_data(key: str):
        validation.checked_key(key)
        return router.delete(key, validation.query_version())

    @app.get("/data")
    def scan_data():
        return router.scan(validation.scan_prefix(), validation.scan_filters())

    def _key_count(node: NodeInfo) -> Optional[int]:
        if node.state == DOWN:
            return None
        try:
            response = requests.get(f"{node.url}/internal/stats", timeout=NODE_CALL_TIMEOUT_S)
            return response.json()["keys"] if response.status_code == 200 else None
        except (requests.RequestException, ValueError, KeyError):
            return None

    @app.get("/cluster")
    def cluster():
        shards = registry.snapshot()
        nodes = [node for info in shards for node in info.nodes.values()]
        counts = dict(zip((node.node_id for node in nodes), pool.map(_key_count, nodes)))
        return {
            "shards": [
                {
                    "shard": info.shard,
                    "primary": info.primary,
                    "in_sync": sorted(info.in_sync),
                    "nodes": [
                        describe(node, counts[node.node_id])
                        for node in sorted(info.nodes.values(), key=lambda n: n.node_id)
                    ],
                }
                for info in shards
            ]
        }

    log.info("coordinator started")
    return app
