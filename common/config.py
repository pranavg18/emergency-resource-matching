"""Configuration read from environment variables.

The helpers turn a missing or malformed variable into a `ConfigError` with the
variable's name in the message, so a misconfigured container fails at startup
with a clear reason instead of later with a confusing one. `Settings` holds
the variables shared by the application services, with the defaults listed in
the API contract.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Mapping, Optional

# Every internal HTTP call sets an explicit timeout (the `requests` library has
# none by default). Recovery sync is the one slower call.
INTERNAL_HTTP_TIMEOUT_S = 2.0
SYNC_HTTP_TIMEOUT_S = 30.0

_TRUE = frozenset({"1", "true", "yes", "on"})
_FALSE = frozenset({"0", "false", "no", "off"})
_MISSING = object()


class ConfigError(ValueError):
    """A required variable is missing, or a value cannot be parsed."""


def _raw(name: str, environ: Optional[Mapping[str, str]]) -> Optional[str]:
    env = os.environ if environ is None else environ
    value = env.get(name)
    if value is None or not value.strip():
        return None
    return value.strip()


def _missing(name: str, default):
    if default is _MISSING:
        raise ConfigError(f"{name} is required but not set")
    return default


def _check_range(name: str, number, min_value, max_value):
    if min_value is not None and number < min_value:
        raise ConfigError(f"{name} must be at least {min_value}, got {number}")
    if max_value is not None and number > max_value:
        raise ConfigError(f"{name} must be at most {max_value}, got {number}")
    return number


def get_str(name: str, default=_MISSING, *, environ: Optional[Mapping[str, str]] = None) -> str:
    """Return the variable, or `default`. Blank counts as unset."""
    value = _raw(name, environ)
    return _missing(name, default) if value is None else value


def get_int(
    name: str,
    default=_MISSING,
    *,
    min_value: Optional[int] = None,
    max_value: Optional[int] = None,
    environ: Optional[Mapping[str, str]] = None,
) -> int:
    value = _raw(name, environ)
    if value is None:
        return _missing(name, default)
    try:
        number = int(value, 10)
    except ValueError:
        raise ConfigError(f"{name} must be an integer, got {value!r}") from None
    return _check_range(name, number, min_value, max_value)


def get_float(
    name: str,
    default=_MISSING,
    *,
    min_value: Optional[float] = None,
    max_value: Optional[float] = None,
    environ: Optional[Mapping[str, str]] = None,
) -> float:
    value = _raw(name, environ)
    if value is None:
        return _missing(name, default)
    try:
        number = float(value)
    except ValueError:
        raise ConfigError(f"{name} must be a number, got {value!r}") from None
    if number != number or number in (float("inf"), float("-inf")):
        raise ConfigError(f"{name} must be a finite number, got {value!r}")
    return _check_range(name, number, min_value, max_value)


def get_bool(name: str, default=_MISSING, *, environ: Optional[Mapping[str, str]] = None) -> bool:
    value = _raw(name, environ)
    if value is None:
        return _missing(name, default)
    lowered = value.lower()
    if lowered in _TRUE:
        return True
    if lowered in _FALSE:
        return False
    raise ConfigError(f"{name} must be true or false, got {value!r}")


@dataclass(frozen=True)
class Settings:
    """Variables shared by request-, resource- and matching-service."""

    coordinator_url: str = "http://coordinator:8000"
    rabbitmq_url: str = "amqp://guest:guest@rabbitmq:5672/"
    max_radius_km: float = 50.0
    max_attempts: int = 5
    match_delay_ms: int = 0
    sweep_interval_s: int = 30
    log_level: str = "INFO"

    @classmethod
    def from_env(cls, environ: Optional[Mapping[str, str]] = None) -> "Settings":
        defaults = cls()
        return cls(
            coordinator_url=get_str("COORDINATOR_URL", defaults.coordinator_url, environ=environ).rstrip("/"),
            rabbitmq_url=get_str("RABBITMQ_URL", defaults.rabbitmq_url, environ=environ),
            max_radius_km=get_float("MAX_RADIUS_KM", defaults.max_radius_km, min_value=0.0, environ=environ),
            max_attempts=get_int("MAX_ATTEMPTS", defaults.max_attempts, min_value=1, environ=environ),
            match_delay_ms=get_int("MATCH_DELAY_MS", defaults.match_delay_ms, min_value=0, environ=environ),
            sweep_interval_s=get_int("SWEEP_INTERVAL_S", defaults.sweep_interval_s, min_value=1, environ=environ),
            log_level=get_str("LOG_LEVEL", defaults.log_level, environ=environ).upper(),
        )
