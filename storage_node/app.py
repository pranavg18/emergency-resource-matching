"""Flask app for a storage node. gunicorn runs `storage_node.app:create_app()`."""

from __future__ import annotations

import logging
import os
import sqlite3
from typing import Optional

from flask import Flask, request

from . import web
from .config import NodeConfig
from .store import KEY_PATTERN, NotFound, Store
from .web import ApiError, invalid, json_body

log = logging.getLogger(__name__)


def _checked_key(key: str) -> str:
    if not KEY_PATTERN.match(key):
        raise invalid(f"malformed key {key!r}")
    return key


def _checked_value(body: dict) -> str:
    value = body.get("value")
    if not isinstance(value, str):
        raise invalid("value must be a string holding the record's JSON text")
    if value == "":
        raise invalid("value must not be empty")
    try:
        value.encode("utf-8")
    except UnicodeEncodeError:
        raise invalid("value must be valid Unicode") from None
    return value


def _flag(name: str) -> bool:
    raw = request.args.get(name)
    if raw is None:
        return False
    lowered = raw.lower()
    if lowered not in ("true", "false"):
        raise invalid(f"{name} must be true or false")
    return lowered == "true"


def _not_found(key: str) -> ApiError:
    return ApiError(404, "NOT_FOUND", f"{key} does not exist")


def create_app(config: Optional[NodeConfig] = None) -> Flask:
    config = config or NodeConfig.from_env()
    web.configure_logging(config.log_level, service="storage-node", node_id=config.node_id, shard=config.shard)
    os.makedirs(config.data_dir, exist_ok=True)
    store = Store(config.db_path)

    app = Flask(__name__)
    web.install(app)

    @app.get("/healthz")
    def healthz():
        return {"status": "ok"}

    @app.get("/readyz")
    def readyz():
        # Ready once the SQLite file is usable. SYNCING does not matter here:
        # the coordinator, not Kubernetes, decides who may serve reads.
        try:
            store.ping()
        except sqlite3.Error as exc:
            return {"status": "not_ready", "reason": f"sqlite: {exc}"}, 503
        return {"status": "ready"}

    @app.put("/internal/data/<key>")
    def put_data(key: str):
        _checked_key(key)
        value = _checked_value(json_body())
        record = store.put(key, value)
        return {"key": record.key, "version": record.version, "updated_at": record.updated_at}

    @app.get("/internal/data/<key>")
    def get_data(key: str):
        _checked_key(key)
        include_deleted = _flag("include_deleted")
        record = store.get(key)
        if record is None or (record.deleted and not include_deleted):
            raise _not_found(key)
        return record.envelope()

    @app.delete("/internal/data/<key>")
    def delete_data(key: str):
        _checked_key(key)
        try:
            record = store.delete(key)
        except NotFound:
            raise _not_found(key) from None
        return {"key": record.key, "version": record.version, "deleted": True, "updated_at": record.updated_at}

    log.info("storage node %s (shard %d) started, data in %s", config.node_id, config.shard, config.db_path)
    return app
