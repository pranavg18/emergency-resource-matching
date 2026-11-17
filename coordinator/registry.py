"""Which storage nodes exist, their state and role, and each shard's in-sync set.

All state lives in process memory under one lock. After a coordinator restart
it is rebuilt from registrations: a node whose heartbeat gets 404 registers
again.

Admission rule, used both when a node registers and when a node marked DOWN
heartbeats again:

* If no other node in the shard is in sync, this node holds every
  acknowledged write (or the shard is empty), so it becomes primary, UP.
* Otherwise another node has writes this one may lack. It becomes a SYNCING
  replica, leaves the in-sync set, and a sync is requested. If it was the
  primary, an in-sync UP node takes over.
"""

from __future__ import annotations

import copy
import logging
import threading
import time
from dataclasses import dataclass, field
from typing import Callable, Optional

from .timefmt import format_rfc3339

log = logging.getLogger(__name__)

UP, SYNCING, DOWN = "UP", "SYNCING", "DOWN"
PRIMARY, REPLICA = "primary", "replica"


class RegistryError(Exception):
    """A registration that conflicts with what the coordinator knows."""


class UnknownNode(Exception):
    """A heartbeat from a node that has not registered (since a restart)."""


@dataclass
class NodeInfo:
    node_id: str
    shard: int
    url: str
    state: str = SYNCING
    role: str = REPLICA
    max_version: int = 0
    last_heartbeat: float = field(default_factory=time.time)  # wall clock, for display
    last_seen: float = field(default_factory=time.monotonic)  # for timeouts


@dataclass
class ShardInfo:
    shard: int
    primary: Optional[str] = None
    in_sync: set[str] = field(default_factory=set)
    nodes: dict[str, NodeInfo] = field(default_factory=dict)


class Registry:
    def __init__(self, shard_count: int, on_needs_sync: Optional[Callable[[str], None]] = None):
        self._lock = threading.RLock()
        self._shards = [ShardInfo(shard) for shard in range(shard_count)]
        self._shard_of: dict[str, int] = {}
        self._on_needs_sync = on_needs_sync or (lambda node_id: None)

    @property
    def shard_count(self) -> int:
        return len(self._shards)

    def register(self, node_id: str, shard: int, url: str, max_version: int) -> NodeInfo:
        with self._lock:
            known_shard = self._shard_of.get(node_id)
            if known_shard is not None and known_shard != shard:
                raise RegistryError(f"{node_id} is registered for shard {known_shard}, not {shard}")
            info = self._shards[shard]
            node = info.nodes.get(node_id) or NodeInfo(node_id=node_id, shard=shard, url=url)
            node.url = url
            node.max_version = max_version
            self._touch(node)
            info.nodes[node_id] = node
            self._shard_of[node_id] = shard
            needs_sync = self._admit(info, node)
            result = copy.copy(node)
        log.info("node %s registered for shard %d: %s %s", node_id, shard, result.state, result.role)
        if needs_sync:
            self._on_needs_sync(node_id)
        return result

    def heartbeat(self, node_id: str) -> NodeInfo:
        needs_sync = False
        with self._lock:
            shard = self._shard_of.get(node_id)
            if shard is None:
                raise UnknownNode(node_id)
            info = self._shards[shard]
            node = info.nodes[node_id]
            self._touch(node)
            if node.state == DOWN:
                log.info("node %s is back after being marked DOWN", node_id)
                needs_sync = self._admit(info, node)
            result = copy.copy(node)
        if needs_sync:
            self._on_needs_sync(node_id)
        return result

    def primary(self, shard: int) -> Optional[NodeInfo]:
        """The shard's primary if it is UP, else None."""
        with self._lock:
            info = self._shards[shard]
            node = info.nodes.get(info.primary) if info.primary else None
            return copy.copy(node) if node is not None and node.state == UP else None

    def snapshot(self) -> list[ShardInfo]:
        """A deep copy of every shard, safe to read without the lock."""
        with self._lock:
            return copy.deepcopy(self._shards)

    @staticmethod
    def _touch(node: NodeInfo) -> None:
        node.last_heartbeat = time.time()
        node.last_seen = time.monotonic()

    def _admit(self, info: ShardInfo, node: NodeInfo) -> bool:
        """Apply the admission rule. Returns True if the node needs a sync."""
        others_in_sync = info.in_sync - {node.node_id}
        if not others_in_sync:
            info.in_sync = {node.node_id}
            info.primary = node.node_id
            node.state, node.role = UP, PRIMARY
            for other in info.nodes.values():
                if other.node_id != node.node_id:
                    other.role = REPLICA
            return False

        info.in_sync.discard(node.node_id)
        node.state, node.role = SYNCING, REPLICA
        if info.primary in (None, node.node_id):
            info.primary = self._pick_primary(info)
        return True

    @staticmethod
    def _pick_primary(info: ShardInfo) -> Optional[str]:
        for node_id in sorted(info.in_sync):
            candidate = info.nodes[node_id]
            if candidate.state == UP:
                candidate.role = PRIMARY
                return node_id
        return None


def describe(node: NodeInfo, keys: Optional[int]) -> dict:
    """One node as `GET /cluster` shows it."""
    return {
        "node_id": node.node_id,
        "url": node.url,
        "state": node.state,
        "role": node.role,
        "last_heartbeat": format_rfc3339(node.last_heartbeat),
        "keys": keys,
    }
