"""Flask app for a storage node. gunicorn runs `storage_node.app:create_app()`."""

from __future__ import annotations

import logging
import os
import sqlite3
from typing import Optional

from flask import Flask

from . import web
from .config import NodeConfig
from .store import Store

log = logging.getLogger(__name__)


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

    log.info("storage node %s (shard %d) started, data in %s", config.node_id, config.shard, config.db_path)
    return app
