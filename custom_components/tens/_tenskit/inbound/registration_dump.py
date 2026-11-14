"""Build the one-shot history a device is offered to 10s with when it is registered.

It is read live from Home Assistant's statistics, never from the 72-hour archive: 5-minute buckets for what the
recorder holds and hourly buckets for the last year. A series is sparse: a bucket with no reading has no entry and goes
to 10s as ``null``, so an outage is never read as zero consumption. Only completed slots are sent, ending at the start
of the slot in progress when the registration was first seen.
"""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from ..contract import MeteredDevice, Output, slot_start
from .statistics import (
    PERIOD_FIVE_MINUTE,
    PERIOD_HOUR,
    StatBucket,
    async_fetch_statistics,
    period_minutes,
)

# 10s's backfill cap is one year ("may adjust later"). Hourly statistics are never purged, hence the cap; short-term
# ones live for the recorder's ``purge_keep_days``. The series are sparse, so a generous window costs only the rows
# that come back.
QUERY_DAYS = 365

CounterValues = Mapping[datetime, float]


@dataclass(frozen=True)
class DumpSeries:
    """One device's energy at one resolution, held sparsely.

    Attributes:
        period: The statistics period the buckets came from.
        consumed: Energy into the device per bucket start; a bucket with no reading has no entry.
        returned: Energy out of the device per bucket start, empty for a kind that measures none.
    """

    period: str
    consumed: CounterValues = field(default_factory=dict)
    returned: CounterValues = field(default_factory=dict)

    @property
    def step(self) -> timedelta:
        """Return one bucket's width."""
        return timedelta(minutes=period_minutes(self.period))


@dataclass(frozen=True)
class RegistrationDump:
    """Everything Home Assistant holds about one device's energy.

    Attributes:
        fine: The 5-minute series over the recorder's short-term window.
        coarse: The hourly series over the long-term depth.
    """

    fine: DumpSeries
    coarse: DumpSeries


def _entities_for(device: MeteredDevice, output: Output) -> list[str]:
    """Return the device's counters feeding one output, in binding order."""
    return [
        binding.entity_id for binding in device.meters
        if (slot := device.profile.slot(binding.role)) is not None and slot.output == output
    ]


def _side(entities: Sequence[str], values: Mapping[str, CounterValues], start: datetime) -> float | None:
    """Return one output side's energy over one bucket, or None.

    None unless every counter of the side has a value: a partial sum would under-report silently.
    """
    if not entities:
        return None
    found = [values.get(entity, {}).get(start) for entity in entities]
    if any(value is None for value in found):
        return None
    return sum(value for value in found if value is not None)


def device_bucket(
    device: MeteredDevice, values: Mapping[str, CounterValues], start: datetime,
) -> tuple[float | None, float | None]:
    """Return one device's ``(consumed_kwh, returned_kwh)`` over one bucket.

    Both are None when the consumed side has no value, since every kind requires that side.
    """
    consumed = _side(_entities_for(device, "consumed"), values, start)
    if consumed is None:
        return None, None
    return consumed, _side(_entities_for(device, "returned"), values, start)


def build_series(
    device: MeteredDevice,
    values: Mapping[str, CounterValues],
    period: str,
    end: datetime,
) -> DumpSeries:
    """Return one device's sparse series: the buckets with a reading before ``end`` (exclusive)."""
    starts = sorted({start for entity in device.meter_entities for start in values.get(entity, {})})
    consumed: dict[datetime, float] = {}
    returned: dict[datetime, float] = {}
    for start in starts:
        if start >= end:
            break
        consumed_kwh, returned_kwh = device_bucket(device, values, start)
        if consumed_kwh is None:
            continue
        consumed[start] = consumed_kwh
        if returned_kwh is not None:
            returned[start] = returned_kwh
    return DumpSeries(period=period, consumed=consumed, returned=returned)


def _as_values(buckets: Sequence[StatBucket]) -> dict[datetime, float]:
    """Return statistics buckets as values keyed by start at native resolution, leaving out empty ones."""
    return {
        bucket.start: bucket.change
        for bucket in buckets if bucket.change is not None
    }


async def async_build_dump(
    hass: Any,
    device: MeteredDevice,
    now: datetime,
) -> RegistrationDump:
    """Read one device's whole recorded history out of Home Assistant.

    Args:
        now: The instant the dump is as of; the runtime passes the instant the device was registered.

    Returns:
        The dump; a recorder holding nothing yields two empty series, not an error.

    Raises:
        RecorderReadError: The recorder is loaded but could not be read.
    """
    # Ends at the slot start, not ``now``: the live series is owed from that same slot on (the registration sets the
    # watermark to the mark before it), so the two meet and never overlap.
    end = slot_start(now)
    entities = device.meter_entities
    fine_raw: Mapping[str, Sequence[StatBucket]] = await async_fetch_statistics(
        hass, entities, end - timedelta(days=QUERY_DAYS), end, PERIOD_FIVE_MINUTE,
    )
    coarse_end = end.replace(minute=0)
    coarse_raw: Mapping[str, Sequence[StatBucket]] = await async_fetch_statistics(
        hass, entities, coarse_end - timedelta(days=QUERY_DAYS), coarse_end, PERIOD_HOUR,
    )
    return RegistrationDump(
        fine=build_series(
            device, {e: _as_values(rows) for e, rows in fine_raw.items()}, PERIOD_FIVE_MINUTE, end,
        ),
        coarse=build_series(
            device, {e: _as_values(rows) for e, rows in coarse_raw.items()}, PERIOD_HOUR, coarse_end,
        ),
    )
