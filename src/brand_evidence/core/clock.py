"""UTC timestamps as ISO-8601 text, the only time format stored in the record."""

from __future__ import annotations

from datetime import UTC, datetime


def now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="microseconds")


def to_iso(value: datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(UTC).isoformat(timespec="microseconds")


def parse_iso(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)
