"""Background thread that marks silent nodes DOWN.

Nodes heartbeat every interval; one silent for the whole timeout (6 s by
default, three missed heartbeats) is marked DOWN. The role change happens
under the shard's write lock, so it never lands in the middle of a write.
Heartbeats themselves never take that lock.
"""

from __future__ import annotations

import logging
import threading

from .registry import Registry
from .router import Router

log = logging.getLogger(__name__)


class FailureDetector:
    def __init__(self, registry: Registry, router: Router, interval_ms: int, timeout_ms: int):
        self._registry = registry
        self._router = router
        self._period_s = interval_ms / 2000  # check twice per heartbeat interval
        self._timeout_s = timeout_ms / 1000
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name="failure-detector", daemon=True)

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._thread.join(timeout=5)

    def _run(self) -> None:
        while not self._stop.wait(self._period_s):
            try:
                self.check()
            except Exception:  # keep detecting even if one pass fails
                log.exception("failure detection pass failed")

    def check(self) -> None:
        for node_id, shard in self._registry.stale(self._timeout_s):
            with self._router.shard_lock(shard):
                self._registry.mark_down_if_stale(node_id, self._timeout_s)
