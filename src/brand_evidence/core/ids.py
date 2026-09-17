"""UUIDv7 generator (RFC 9562). Python 3.12 has no uuid.uuid7, so this is a local shim."""

from __future__ import annotations

import os
import time
import uuid


def uuid7() -> str:
    ms = time.time_ns() // 1_000_000
    rand = int.from_bytes(os.urandom(10), "big")
    rand_a = (rand >> 62) & 0x0FFF
    rand_b = rand & ((1 << 62) - 1)
    value = (ms << 80) | (0x7 << 76) | (rand_a << 64) | (0b10 << 62) | rand_b
    return str(uuid.UUID(int=value))
