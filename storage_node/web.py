"""HTTP plumbing for every route: JSON errors, request ids and JSON logs.

The coordinator has its own copy of this module rather than sharing one, so
that each storage-layer component stays self-contained.
"""

from __future__ import annotations

import json
import logging
import sys
import uuid
from typing import Any

from flask import Flask, g, has_request_context, jsonify, request
from werkzeug.exceptions import HTTPException

from .timefmt import format_rfc3339

REQUEST_ID_HEADER = "X-Request-ID"

# Polled every few seconds; logged at DEBUG so they do not drown out the rest.
QUIET_PATHS = frozenset({"/healthz", "/readyz", "/internal/nodes/heartbeat", "/internal/stats"})

_CODES_BY_STATUS = {400: "INVALID_REQUEST", 404: "NOT_FOUND", 405: "METHOD_NOT_ALLOWED"}

log = logging.getLogger(__name__)


class ApiError(Exception):
    """Raised by a route to return the contract's error body."""

    def __init__(self, status: int, code: str, message: str, **extra: Any):
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message
        self.extra = extra


def invalid(message: str) -> ApiError:
    return ApiError(400, "INVALID_REQUEST", message)


def error_response(status: int, code: str, message: str, **extra: Any):
    return jsonify({"error": {"code": code, "message": message, **extra}}), status


def json_body() -> dict:
    """The request body as a JSON object, or a 400."""
    body = request.get_json(force=True, silent=True)
    if not isinstance(body, dict):
        raise invalid("body must be a JSON object")
    return body


def install(app: Flask) -> None:
    """Attach request-id handling, logging and JSON error handlers."""
    app.json.sort_keys = False

    @app.before_request
    def _assign_request_id():
        g.request_id = request.headers.get(REQUEST_ID_HEADER) or str(uuid.uuid4())

    @app.after_request
    def _finish(response):
        request_id = g.get("request_id")
        if request_id:
            response.headers[REQUEST_ID_HEADER] = request_id
        level = logging.DEBUG if request.path in QUIET_PATHS else logging.INFO
        log.log(level, "%s %s -> %d", request.method, request.path, response.status_code)
        return response

    @app.errorhandler(ApiError)
    def _api_error(err: ApiError):
        return error_response(err.status, err.code, err.message, **err.extra)

    @app.errorhandler(HTTPException)
    def _http_error(err: HTTPException):
        status = err.code or 500
        code = _CODES_BY_STATUS.get(status, "INVALID_REQUEST" if status < 500 else "INTERNAL")
        if status == 404:
            message = f"no route for {request.method} {request.path}"
        elif status == 405:
            message = f"{request.method} is not allowed on {request.path}"
        else:
            message = err.description or err.name
        return error_response(status, code, message)

    @app.errorhandler(Exception)
    def _unexpected(err: Exception):
        log.exception("unhandled error")
        return error_response(500, "INTERNAL", "internal error")


class JsonFormatter(logging.Formatter):
    """One JSON object per line, carrying the request id when there is one."""

    def __init__(self, static_fields: dict[str, Any]):
        super().__init__()
        self._static = static_fields

    def format(self, record: logging.LogRecord) -> str:
        entry: dict[str, Any] = {
            "ts": format_rfc3339(record.created),
            "level": record.levelname.lower(),
            **self._static,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        if has_request_context():
            request_id = g.get("request_id")
            if request_id:
                entry["request_id"] = request_id
        if record.exc_info:
            entry["exc"] = self.formatException(record.exc_info)
        return json.dumps(entry, default=str)


def configure_logging(level: str, **static_fields: Any) -> None:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter(static_fields))
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(level)
    for noisy in ("werkzeug", "urllib3"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
