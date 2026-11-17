"""Coordinator settings, read once from the environment at startup."""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Mapping, Optional

SHARD_COUNT = 3

# Timeout on every call to a storage node. A hung node must not hold up the
# caller (and, later, the shard's write lock) for long.
NODE_CALL_TIMEOUT_S = 2.0


class ConfigError(ValueError):
    """A variable is invalid."""


def _positive_int(env: Mapping[str, str], name: str, default: int) -> int:
    raw = (env.get(name) or "").strip()
    if not raw:
        return default
    try:
        value = int(raw, 10)
    except ValueError:
        raise ConfigError(f"{name} must be an integer, got {raw!r}") from None
    if value <= 0:
        raise ConfigError(f"{name} must be positive, got {value}")
    return value


@dataclass(frozen=True)
class CoordinatorConfig:
    heartbeat_interval_ms: int = 2000
    heartbeat_timeout_ms: int = 6000
    log_level: str = "INFO"

    @classmethod
    def from_env(cls, environ: Optional[Mapping[str, str]] = None) -> "CoordinatorConfig":
        env = os.environ if environ is None else environ
        interval = _positive_int(env, "HEARTBEAT_INTERVAL_MS", cls.heartbeat_interval_ms)
        timeout = _positive_int(env, "HEARTBEAT_TIMEOUT_MS", cls.heartbeat_timeout_ms)
        if timeout <= interval:
            raise ConfigError(
                f"HEARTBEAT_TIMEOUT_MS ({timeout}) must be larger than HEARTBEAT_INTERVAL_MS ({interval})"
            )
        return cls(
            heartbeat_interval_ms=interval,
            heartbeat_timeout_ms=timeout,
            log_level=(env.get("LOG_LEVEL") or "INFO").strip().upper(),
        )
