"""Routes storage operations to the shard that owns each key.

Callers never learn a shard number or a node address. A write goes to the
shard's primary under that shard's lock; a scan asks every shard in parallel
and fails as a whole (503) if any shard cannot answer, rather than returning a
silently partial list.
"""

from __future__ import annotations

import json
import logging
import threading
from concurrent.futures import ThreadPoolExecutor
from typing import Optional

from . import nodes
from .nodes import NodeReply, NodeUnreachable
from .registry import NodeInfo, Registry
from .sharding import shard_for_key
from .web import ApiError

log = logging.getLogger(__name__)


def _unavailable(shard: int, why: str) -> ApiError:
    return ApiError(503, "STORAGE_UNAVAILABLE", f"shard {shard} is unavailable: {why}")


def _relay(reply: NodeReply) -> ApiError:
    """Pass a node's 4xx error through unchanged."""
    error = reply.body.get("error") if isinstance(reply.body.get("error"), dict) else {}
    code = error.get("code", "INVALID_REQUEST")
    message = error.get("message", f"storage node answered {reply.status}")
    extra = {k: v for k, v in error.items() if k not in ("code", "message")}
    return ApiError(reply.status, code, message, **extra)


def _matches(value: str, filters: dict[str, str]) -> bool:
    if not filters:
        return True
    try:
        record = json.loads(value)
    except ValueError:
        return False
    return isinstance(record, dict) and all(record.get(k) == v for k, v in filters.items())


class Router:
    def __init__(self, registry: Registry, pool: ThreadPoolExecutor):
        self._registry = registry
        self._pool = pool
        # Re-entrant: a write that discovers its primary has failed can apply
        # the resulting role change while still holding the lock.
        self._locks = [threading.RLock() for _ in range(registry.shard_count)]

    def shard_of(self, key: str) -> int:
        return shard_for_key(key, self._registry.shard_count)

    def shard_lock(self, shard: int) -> threading.RLock:
        return self._locks[shard]

    # --- writes ----------------------------------------------------------

    def _primary_or_503(self, shard: int) -> NodeInfo:
        primary = self._registry.primary(shard)
        if primary is None:
            raise _unavailable(shard, "no primary is up")
        return primary

    def put(self, key: str, value: str, if_version: Optional[int]) -> dict:
        shard = self.shard_of(key)
        body: dict = {"value": value}
        if if_version is not None:
            body["if_version"] = if_version
        with self._locks[shard]:
            primary = self._primary_or_503(shard)
            reply = self._on_primary(shard, primary, "PUT", f"/internal/data/{key}", json=body)
            version, updated_at = reply.body["version"], reply.body["updated_at"]
            self._replicate(shard, key, {"value": value, "version": version, "deleted": False, "updated_at": updated_at})
            return {"key": key, "version": version, "updated_at": updated_at}

    def delete(self, key: str, if_version: Optional[int]) -> dict:
        shard = self.shard_of(key)
        params = {"if_version": str(if_version)} if if_version is not None else None
        with self._locks[shard]:
            primary = self._primary_or_503(shard)
            reply = self._on_primary(shard, primary, "DELETE", f"/internal/data/{key}", params=params)
            version, updated_at = reply.body["version"], reply.body["updated_at"]
            self._replicate(shard, key, {"value": "", "version": version, "deleted": True, "updated_at": updated_at})
            return {"key": key, "version": version, "deleted": True, "updated_at": updated_at}

    def _replicate(self, shard: int, key: str, row: dict) -> None:
        """Copy the primary's row to every other live node, still under the shard lock.

        Synchronous, so an acknowledged write is on every replica that is up.
        Holding the lock means two writes to one key reach a replica in the
        same order they reached the primary.
        """
        for replica in self._registry.replicas(shard):
            try:
                reply = nodes.call("PUT", replica.url, f"/internal/replica/{key}", json=row)
                if reply.status != 200:
                    raise NodeUnreachable(f"replica write answered {reply.status}: {reply.body}")
            except NodeUnreachable as exc:
                self._replica_failed(shard, replica, exc)

    def _replica_failed(self, shard: int, replica: NodeInfo, exc: Exception) -> None:
        log.warning("replica %s missed a write: %s", replica.node_id, exc)

    def _on_primary(self, shard: int, primary: NodeInfo, method: str, path: str, **kwargs) -> NodeReply:
        try:
            reply = nodes.call(method, primary.url, path, **kwargs)
        except NodeUnreachable as exc:
            log.warning("primary %s failed: %s", primary.node_id, exc)
            raise _unavailable(shard, f"primary {primary.node_id} did not answer") from None
        if reply.status != 200:
            raise _relay(reply)
        return reply

    # --- reads -----------------------------------------------------------

    def _readable(self, shard: int) -> list[NodeInfo]:
        primary = self._registry.primary(shard)
        return [primary] if primary is not None else []

    def get(self, key: str) -> dict:
        shard = self.shard_of(key)
        for node in self._readable(shard):
            try:
                reply = nodes.call("GET", node.url, f"/internal/data/{key}")
            except NodeUnreachable as exc:
                log.warning("read from %s failed: %s", node.node_id, exc)
                continue
            if reply.status == 200:
                return reply.body
            raise _relay(reply)
        raise _unavailable(shard, "no readable node is up")

    def _scan_shard(self, shard: int, prefix: str) -> list[dict]:
        for node in self._readable(shard):
            try:
                reply = nodes.call("GET", node.url, "/internal/data", params={"prefix": prefix})
            except NodeUnreachable as exc:
                log.warning("scan of %s failed: %s", node.node_id, exc)
                continue
            if reply.status == 200 and isinstance(reply.body.get("records"), list):
                return reply.body["records"]
        raise _unavailable(shard, "no readable node is up")

    def scan(self, prefix: str, filters: dict[str, str]) -> dict:
        shards = range(self._registry.shard_count)
        results = list(self._pool.map(lambda shard: self._scan_shard(shard, prefix), shards))
        records = [
            record
            for shard_records in results
            for record in shard_records
            if _matches(record.get("value", ""), filters)
        ]
        return {"count": len(records), "records": records}
