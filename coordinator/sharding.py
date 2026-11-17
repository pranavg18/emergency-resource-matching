"""Which shard owns a key.

MD5 of the full key (for example `request:<uuid>`) encoded as UTF-8, the 16-byte
digest read as an unsigned big-endian integer, modulo the shard count. Python's
built-in `hash()` is salted per process and must never be used here: two
coordinator restarts would route the same key to different shards.
"""

from __future__ import annotations

import hashlib
import re

KEY_PATTERN = re.compile(
    r"^(request|resource):[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"
)
SCAN_PREFIXES = ("request:", "resource:")


def shard_for_key(key: str, shard_count: int) -> int:
    digest = hashlib.md5(key.encode("utf-8"), usedforsecurity=False).digest()
    return int.from_bytes(digest, "big") % shard_count
