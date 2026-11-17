"""HTTP calls from the coordinator to storage nodes.

Every call has an explicit timeout, so a node that hangs (rather than refusing
connections) cannot hold up a caller, or the shard lock the caller may hold,
for longer than that. A timeout, refused connection or 5xx reply raises
`NodeUnreachable`; the caller decides whether that marks the node DOWN.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass
from typing import Any, Optional

import requests
from flask import g, has_request_context

from .config import NODE_CALL_TIMEOUT_S
from .web import REQUEST_ID_HEADER


class NodeUnreachable(Exception):
    """The node did not answer usefully: network error, timeout, or 5xx."""


@dataclass(frozen=True)
class NodeReply:
    status: int
    body: dict


_local = threading.local()


def current_request_id() -> Optional[str]:
    """The incoming request's id, if called while handling a request.

    Worker-pool threads (scatter-gather) have no request context, so callers
    that fan out must read this first and pass it to `call` explicitly.
    """
    if has_request_context():
        return g.get("request_id")
    return None


def _session() -> requests.Session:
    # One session (and connection pool) per thread: Session is not
    # guaranteed to be thread-safe.
    session = getattr(_local, "session", None)
    if session is None:
        session = _local.session = requests.Session()
    return session


def call(
    method: str,
    base_url: str,
    path: str,
    *,
    json: Optional[dict] = None,
    params: Optional[dict] = None,
    timeout: float = NODE_CALL_TIMEOUT_S,
    request_id: Optional[str] = None,
) -> NodeReply:
    request_id = request_id or current_request_id()
    headers = {REQUEST_ID_HEADER: request_id} if request_id else {}
    try:
        response = _session().request(
            method, f"{base_url}{path}", json=json, params=params, headers=headers, timeout=timeout
        )
    except requests.RequestException as exc:
        raise NodeUnreachable(f"{method} {base_url}{path}: {exc.__class__.__name__}") from exc
    if response.status_code >= 500:
        raise NodeUnreachable(f"{method} {base_url}{path}: HTTP {response.status_code}")
    try:
        body: Any = response.json()
    except ValueError as exc:
        raise NodeUnreachable(f"{method} {base_url}{path}: reply is not JSON") from exc
    if not isinstance(body, dict):
        raise NodeUnreachable(f"{method} {base_url}{path}: reply is not a JSON object")
    return NodeReply(response.status_code, body)
