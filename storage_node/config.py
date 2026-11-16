"""Storage node settings, read once from the environment at startup."""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Mapping, Optional

SHARD_COUNT = 3
DEFAULT_COORDINATOR_URL = "http://coordinator:8000"
DB_FILENAME = "node.db"


class ConfigError(ValueError):
    """A required variable is missing or invalid."""


def _require(env: Mapping[str, str], name: str) -> str:
    value = (env.get(name) or "").strip()
    if not value:
        raise ConfigError(f"{name} is required but not set")
    return value


@dataclass(frozen=True)
class NodeConfig:
    node_id: str
    shard: int
    node_url: str
    data_dir: str
    coordinator_url: str = DEFAULT_COORDINATOR_URL
    log_level: str = "INFO"

    @property
    def db_path(self) -> str:
        return os.path.join(self.data_dir, DB_FILENAME)

    @classmethod
    def from_env(cls, environ: Optional[Mapping[str, str]] = None) -> "NodeConfig":
        env = os.environ if environ is None else environ
        raw_shard = _require(env, "SHARD_ID")
        try:
            shard = int(raw_shard, 10)
        except ValueError:
            raise ConfigError(f"SHARD_ID must be an integer, got {raw_shard!r}") from None
        if not 0 <= shard < SHARD_COUNT:
            raise ConfigError(f"SHARD_ID must be between 0 and {SHARD_COUNT - 1}, got {shard}")
        return cls(
            node_id=_require(env, "NODE_ID"),
            shard=shard,
            node_url=_require(env, "NODE_URL").rstrip("/"),
            data_dir=_require(env, "DATA_DIR"),
            coordinator_url=(env.get("COORDINATOR_URL") or DEFAULT_COORDINATOR_URL).strip().rstrip("/"),
            log_level=(env.get("LOG_LEVEL") or "INFO").strip().upper(),
        )
