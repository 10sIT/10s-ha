"""Read ISO-8601 instants from storage and from 10s, a naive one meaning UTC."""
from __future__ import annotations

from datetime import UTC, datetime
from typing import Any


def parse_instant(raw: Any) -> datetime | None:
    """Parse an ISO-8601 instant (``Z`` included), treating a naive one as UTC.

    Args:
        raw: The stored or received value; anything but a string reads as absent.

    Returns:
        A timezone-aware datetime, or None when ``raw`` is not a readable instant.
    """
    if not isinstance(raw, str):
        return None
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)


__all__ = ["parse_instant"]
