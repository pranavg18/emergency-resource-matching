"""Recovery sync: make this node a copy of a source node (the shard's primary).

One pass compares the source's digest with ours and copies every row that
differs: a different version, or the same version with different content (two
nodes can hold different values under one version if a primary committed a
write and died before replicating it). The coordinator runs two passes:

1. `final=False` while writes keep flowing, to copy the bulk. A row older
   than ours is skipped, since a live write may have landed after we read the
   source; nothing is deleted.
2. `final=True` while it holds the shard's write lock, so nothing changes
   underneath. The copy is then exact: rows are overwritten whatever version we
   held, and rows the source does not have are removed. Those were never
   acknowledged, so dropping them loses nothing.

Fencing. If the coordinator's call times out, this handler keeps running. A
later pass could then finish, the node rejoin the in-sync set and take new
writes, and the old pass would still be holding rows it read minutes ago. So
every pass carries an epoch (chosen by the coordinator, always increasing). A
pass is accepted only if its epoch is the newest this node has seen, and every
write it makes happens under the fence's lock after re-checking that it is
still the newest. A superseded pass stops before touching the store again.
"""

from __future__ import annotations

import logging
import threading
from contextlib import contextmanager
from typing import Iterator, Optional
from urllib.parse import quote

import requests

from .store import Record, Store

log = logging.getLogger(__name__)

SOURCE_TIMEOUT_S = 10.0


class SyncFailed(Exception):
    """The source could not be read."""


class SyncSuperseded(Exception):
    """A pass with a newer epoch has started; this one must not change anything."""


class SyncFence:
    """Tracks the newest sync epoch this node has accepted."""

    def __init__(self):
        self._lock = threading.Lock()
        self._current = -1

    def begin(self, epoch: int) -> None:
        with self._lock:
            if epoch < self._current:
                raise SyncSuperseded(f"epoch {epoch} is older than {self._current}")
            self._current = epoch

    def check(self, epoch: int) -> None:
        with self._lock:
            if epoch != self._current:
                raise SyncSuperseded(f"epoch {epoch} was superseded by {self._current}")

    @contextmanager
    def holding(self, epoch: int) -> Iterator[None]:
        """Run one store mutation only if `epoch` is still the newest."""
        with self._lock:
            if epoch != self._current:
                raise SyncSuperseded(f"epoch {epoch} was superseded by {self._current}")
            yield


def _get(session: requests.Session, url: str, request_id: Optional[str], **params) -> requests.Response:
    headers = {"X-Request-ID": request_id} if request_id else {}
    try:
        response = session.get(url, params=params or None, headers=headers, timeout=SOURCE_TIMEOUT_S)
    except requests.RequestException as exc:
        raise SyncFailed(f"GET {url}: {exc.__class__.__name__}") from exc
    if response.status_code >= 500:
        raise SyncFailed(f"GET {url}: HTTP {response.status_code}")
    return response


def run_sync(
    store: Store,
    fence: SyncFence,
    source_url: str,
    final: bool,
    epoch: int,
    request_id: Optional[str] = None,
) -> dict:
    fence.begin(epoch)
    with requests.Session() as session:
        response = _get(session, f"{source_url}/internal/sync/digest", request_id)
        try:
            source = response.json()["entries"]
        except (ValueError, KeyError, TypeError) as exc:
            raise SyncFailed("source digest is not valid") from exc

        local = store.digest()
        copied = 0
        for key, entry in source.items():
            if local.get(key) == entry:
                continue
            fence.check(epoch)  # stop early if a newer pass has started
            reply = _get(
                session, f"{source_url}/internal/data/{quote(key, safe=':')}", request_id, include_deleted="true"
            )
            if reply.status_code == 404:
                continue  # cannot happen (rows are never removed on a primary); nothing to copy
            if reply.status_code != 200:
                raise SyncFailed(f"source answered {reply.status_code} for {key}")
            row = reply.json()
            record = Record(row["key"], row["value"], row["version"], row["deleted"], row["updated_at"])
            with fence.holding(epoch):
                if store.overwrite(record, allow_older=final):
                    copied += 1

        removed = 0
        if final:
            for key in set(local) - set(source):
                with fence.holding(epoch):
                    store.remove(key)
                removed += 1

    log.info("sync from %s (final=%s): copied %d, removed %d", source_url, final, copied, removed)
    return {"copied": copied, "deleted": removed, "final": final, "epoch": epoch}
