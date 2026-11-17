"""Flask app for a storage node. gunicorn runs `storage_node.app:create_app()`."""

from __future__ import annotations

import logging
import os
import re
import sqlite3
from typing import Optional

from flask import Flask, request

from . import web
from .config import NodeConfig
from .heartbeat import HeartbeatClient
from .store import KEY_PATTERN, SCAN_PREFIXES, NotFound, Record, Store, VersionConflict
from .web import ApiError, invalid, json_body

log = logging.getLogger(__name__)
_DIGITS = re.compile(r"^[0-9]+$")


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


def _body_version(body: dict) -> Optional[int]:
    """`if_version` from a JSON body: absent/null, or an integer >= 0."""
    raw = body.get("if_version")
    if raw is None:
        return None
    if isinstance(raw, bool) or not isinstance(raw, int) or raw < 0:
        raise invalid("if_version must be an integer >= 0")
    return raw


def _query_version() -> Optional[int]:
    """`if_version` from the query string."""
    raw = request.args.get("if_version")
    if raw is None:
        return None
    if not _DIGITS.match(raw):
        raise invalid("if_version must be an integer >= 0")
    return int(raw)


_TIMESTAMP = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z$")


def _replica_record(key: str, body: dict) -> Record:
    """A row sent by the coordinator, copied from the primary."""
    version = body.get("version")
    if isinstance(version, bool) or not isinstance(version, int) or version < 1:
        raise invalid("version must be an integer >= 1")
    deleted = body.get("deleted", False)
    if not isinstance(deleted, bool):
        raise invalid("deleted must be true or false")
    updated_at = body.get("updated_at")
    if not isinstance(updated_at, str) or not _TIMESTAMP.match(updated_at):
        raise invalid("updated_at must be an RFC 3339 UTC timestamp with milliseconds")
    if deleted:
        if body.get("value", "") != "":
            raise invalid("a tombstone's value must be empty")
        value = ""
    else:
        value = _checked_value(body)
    return Record(key, value, version, deleted, updated_at)


def _not_found(key: str) -> ApiError:
    return ApiError(404, "NOT_FOUND", f"{key} does not exist")


def _conflict(key: str, err: VersionConflict) -> ApiError:
    return ApiError(
        409, "VERSION_CONFLICT", f"{key} is at version {err.current_version}", current_version=err.current_version
    )


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
        body = json_body()
        value = _checked_value(body)
        try:
            record = store.put(key, value, if_version=_body_version(body))
        except VersionConflict as err:
            raise _conflict(key, err) from None
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
        if_version = _query_version()
        try:
            record = store.delete(key, if_version=if_version)
        except NotFound:
            raise _not_found(key) from None
        except VersionConflict as err:
            raise _conflict(key, err) from None
        return {"key": record.key, "version": record.version, "deleted": True, "updated_at": record.updated_at}

    @app.put("/internal/replica/<key>")
    def put_replica(key: str):
        _checked_key(key)
        applied, version = store.apply_replica(_replica_record(key, json_body()))
        return {"key": key, "version": version, "applied": applied}

    @app.get("/internal/data")
    def scan_data():
        prefix = request.args.get("prefix")
        if prefix not in SCAN_PREFIXES:
            raise invalid(f"prefix must be one of {', '.join(SCAN_PREFIXES)}")
        records = [record.envelope() for record in store.scan(prefix)]
        return {"count": len(records), "records": records}

    @app.get("/internal/stats")
    def stats():
        return {"node_id": config.node_id, "shard": config.shard, **store.stats()}

    # gunicorn calls create_app() inside its single worker process, so the
    # thread lives in the process that serves requests.
    HeartbeatClient(config, max_version=lambda: store.stats()["max_version"]).start()

    log.info("storage node %s (shard %d) started, data in %s", config.node_id, config.shard, config.db_path)
    return app
