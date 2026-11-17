"""Checks on keys, values and versions arriving at the coordinator's data API.

Rejecting bad input here means a malformed request never reaches a node, and
never takes a shard's write lock.
"""

from __future__ import annotations

import re
from typing import Optional

from flask import request

from .sharding import KEY_PATTERN, SCAN_PREFIXES
from .web import invalid

_DIGITS = re.compile(r"^[0-9]+$")


def checked_key(key: str) -> str:
    if not KEY_PATTERN.match(key):
        raise invalid(f"malformed key {key!r}")
    return key


def checked_value(body: dict) -> str:
    value = body.get("value")
    if not isinstance(value, str):
        raise invalid("value must be a string holding the record's JSON text")
    if value == "":
        raise invalid("value must not be empty")
    try:
        value.encode("utf-8")
    except UnicodeEncodeError:
        raise invalid("value must be valid Unicode") from None
    return value


def body_version(body: dict) -> Optional[int]:
    raw = body.get("if_version")
    if raw is None:
        return None
    if isinstance(raw, bool) or not isinstance(raw, int) or raw < 0:
        raise invalid("if_version must be an integer >= 0")
    return raw


def query_version() -> Optional[int]:
    raw = request.args.get("if_version")
    if raw is None:
        return None
    if not _DIGITS.match(raw):
        raise invalid("if_version must be an integer >= 0")
    return int(raw)


def scan_prefix() -> str:
    prefix = request.args.get("prefix")
    if prefix not in SCAN_PREFIXES:
        raise invalid(f"prefix must be one of {', '.join(SCAN_PREFIXES)}")
    return prefix


def scan_filters() -> dict[str, str]:
    """Optional top-level field filters for scans: `type` and `status`."""
    filters = {}
    for name in ("type", "status"):
        value = request.args.get(name)
        if value is not None:
            if not value:
                raise invalid(f"{name} must not be empty")
            filters[name] = value
    return filters
