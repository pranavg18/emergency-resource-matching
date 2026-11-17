"""Timestamps in the contract's format: RFC 3339, UTC, `Z`, milliseconds."""

from __future__ import annotations

import time
from datetime import datetime, timezone


def format_rfc3339(epoch_seconds: float) -> str:
    moment = datetime.fromtimestamp(epoch_seconds, tz=timezone.utc)
    return moment.isoformat(timespec="milliseconds").replace("+00:00", "Z")


def now_rfc3339() -> str:
    return format_rfc3339(time.time())
