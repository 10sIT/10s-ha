"""Slot grid and one metered device's counter totals at the end of a quarter-hour slot.

A slot keeps the counter's latest total before it ended, not an energy: the energy is the difference of two totals,
computed by the outbound encoder (``slot_energies``).
"""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from .const import SLOT_MINUTES
from .instants import parse_instant


def slot_start(moment: datetime) -> datetime:
    """Return the UTC quarter-hour mark at or before ``moment``; a naive ``moment`` is read as UTC."""
    moment = moment if moment.tzinfo is not None else moment.replace(tzinfo=UTC)
    moment = moment.astimezone(UTC)
    return moment.replace(minute=moment.minute - moment.minute % SLOT_MINUTES, second=0, microsecond=0)


@dataclass(frozen=True)
class DeviceSlot:
    """One device's meter total for one quarter-hour slot.

    Attributes:
        device_id: The metered device's id.
        start: The UTC quarter-hour mark that opens the slot the total closes.
        consumed_kwh: The consumed-side counter's reading.
        returned_kwh: The returned-side counter's reading (V2G or battery discharge), None when the kind has none.
    """

    device_id: str
    start: datetime
    consumed_kwh: float
    returned_kwh: float | None = None

    @property
    def key(self) -> tuple[str, datetime]:
        """Return the ``(device_id, mark)`` pair a reading is stored under."""
        return self.device_id, self.start

    def as_payload(self) -> dict[str, Any]:
        """Return the reading as its JSON-safe stored record, ``"r"`` present only for a returned side."""
        payload: dict[str, Any] = {
            "d": self.device_id,
            "ts": self.start.isoformat(timespec="seconds"),
            "kwh": round(self.consumed_kwh, 6),
        }
        if self.returned_kwh is not None:
            payload["r"] = round(self.returned_kwh, 6)
        return payload

    @classmethod
    def from_payload(cls, raw: Any) -> DeviceSlot | None:
        """Rebuild a reading from its stored record, or None when it is unusable.

        A corrupt record is dropped, not repaired: a fabricated reading would look measured.
        """
        if not isinstance(raw, Mapping):
            return None
        device_id, start = str(raw.get("d") or ""), parse_instant(raw.get("ts"))
        consumed = _number(raw.get("kwh"))
        if not device_id or start is None or consumed is None:
            return None
        return cls(
            device_id=device_id,
            start=slot_start(start),
            consumed_kwh=consumed,
            returned_kwh=_number(raw.get("r")),
        )


def _number(value: Any) -> float | None:
    """Return a stored value as a float, or None when it is not one."""
    if value is None or isinstance(value, bool):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None
