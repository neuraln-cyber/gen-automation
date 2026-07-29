import secrets
import time
from uuid import UUID


def uuid7() -> UUID:
    """Return an RFC 9562 UUIDv7 using the current Unix epoch milliseconds."""
    timestamp_ms = time.time_ns() // 1_000_000
    if timestamp_ms >= 1 << 48:
        raise OverflowError("UUIDv7 timestamp exceeds 48 bits")

    random_a = secrets.randbits(12)
    random_b = secrets.randbits(62)
    value = (timestamp_ms << 80) | (0x7 << 76) | (random_a << 64) | (0b10 << 62) | random_b
    return UUID(int=value)
