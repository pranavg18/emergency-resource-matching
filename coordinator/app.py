"""Flask app for the coordinator. gunicorn runs `coordinator.app:create_app()`."""

from __future__ import annotations

import logging
from typing import Optional

from flask import Flask

from . import web
from .config import CoordinatorConfig

log = logging.getLogger(__name__)


def create_app(config: Optional[CoordinatorConfig] = None) -> Flask:
    config = config or CoordinatorConfig.from_env()
    web.configure_logging(config.log_level, service="coordinator")

    app = Flask(__name__)
    web.install(app)

    @app.get("/healthz")
    def healthz():
        return {"status": "ok"}

    @app.get("/readyz")
    def readyz():
        # The coordinator holds no state it must load first: ready once serving.
        return {"status": "ready"}

    log.info("coordinator started")
    return app
