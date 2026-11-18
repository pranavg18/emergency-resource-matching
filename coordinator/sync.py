"""Brings a SYNCING node back into the in-sync set.

Started from a background thread whenever the registry admits a node as
SYNCING: never inside the register or heartbeat handler, where a slow sync
would delay the reply and get the node marked DOWN again.

Each attempt has two passes against the shard's current primary:

1. Without the shard lock, so writes keep flowing while the bulk is copied.
2. With the shard lock held, so no write can land mid-pass: the node becomes an
   exact copy, then joins the in-sync set as an UP replica in the same locked
   section. From that moment it receives and holds every acknowledged write.

An attempt that cannot finish (no primary up, a node call fails, the primary
changed in between) is retried with backoff. The sync is abandoned if the node
stops being SYNCING (for example it went DOWN); it is requested again when the
node is re-admitted.
"""

from __future__ import annotations

import logging
import threading
import time
from enum import Enum

from . import nodes
from .nodes import NodeUnreachable
from .registry import SYNCING, Registry
from .router import Router

log = logging.getLogger(__name__)

SYNC_CALL_TIMEOUT_S = 30.0
FIRST_BACKOFF_S = 0.5
MAX_BACKOFF_S = 5.0


class Outcome(Enum):
    DONE = "done"
    RETRY = "retry"
    ABANDON = "abandon"


class SyncManager:
    def __init__(self, registry: Registry, router: Router):
        self._registry = registry
        self._router = router
        self._lock = threading.Lock()
        self._running: set[str] = set()

    def request(self, node_id: str) -> None:
        """Start syncing `node_id` unless a sync for it is already running."""
        with self._lock:
            if node_id in self._running:
                return
            self._running.add(node_id)
        threading.Thread(target=self._run, args=(node_id,), name=f"sync-{node_id}", daemon=True).start()

    def _run(self, node_id: str) -> None:
        backoff = FIRST_BACKOFF_S
        try:
            while True:
                try:
                    outcome = self._attempt(node_id)
                except Exception:
                    log.exception("sync of %s failed unexpectedly", node_id)
                    outcome = Outcome.RETRY
                if outcome is not Outcome.RETRY:
                    return
                time.sleep(backoff)
                backoff = min(backoff * 2, MAX_BACKOFF_S)
        finally:
            with self._lock:
                self._running.discard(node_id)

    def _sync_pass(self, url: str, source_url: str, final: bool) -> bool:
        try:
            reply = nodes.call(
                "POST", url, "/internal/sync/run",
                json={"source_url": source_url, "final": final}, timeout=SYNC_CALL_TIMEOUT_S,
            )
        except NodeUnreachable as exc:
            log.warning("sync pass (final=%s) failed: %s", final, exc)
            return False
        if reply.status != 200:
            log.warning("sync pass (final=%s) answered %d: %s", final, reply.status, reply.body)
            return False
        return True

    def _attempt(self, node_id: str) -> Outcome:
        node = self._registry.node(node_id)
        if node is None or node.state != SYNCING:
            return Outcome.ABANDON
        primary = self._registry.primary(node.shard)
        if primary is None:
            return Outcome.RETRY  # nothing to copy from until a primary is up

        if not self._sync_pass(node.url, primary.url, final=False):
            return Outcome.RETRY

        with self._router.shard_lock(node.shard):
            node = self._registry.node(node_id)
            if node is None or node.state != SYNCING:
                return Outcome.ABANDON
            current = self._registry.primary(node.shard)
            if current is None or current.node_id != primary.node_id:
                return Outcome.RETRY  # the primary changed under us: start over
            if not self._sync_pass(node.url, primary.url, final=True):
                return Outcome.RETRY
            self._registry.mark_in_sync(node_id)
        return Outcome.DONE
