"""Hold the 72-hour archive: the readings taken and, per device, the watermark up to which 10s has confirmed them.

One record per ``(device_id, quarter-hour mark)`` plus one watermark per device, the baseline mark of the first owed
slot. A confirmation moves it to the mark of the last reading the confirmed batch used; a new registration sets it to
the mark before the slot in progress at the request. Acknowledgement is per batch, so the watermark is all the
confirmation state there is. The total at the watermark closed the last confirmed slot, so an owed read starts at it.
Readings older than the retention are dropped by contract, a watermark reading included; nothing before the first
stored reading is sent, not even as ``null``. :func:`prune` runs at the end of a cycle so the cycle saw the whole
window. Pure Python, unit-testable without Home Assistant.
"""
from __future__ import annotations

from collections.abc import Collection, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from ..contract import (
    DEVICE_SLOT_RETENTION_HOURS,
    SILENT_METER_HOURS,
    SLOT_MINUTES,
    STALLED_UPLOAD_HOURS,
    DeviceSlot,
    parse_instant,
    slot_start,
)


@dataclass
class SlotLog:
    """The persisted archive: the readings and the per-device watermarks.

    Mutated in place by the helpers below, which return whether anything changed so a quiescent installation never
    writes.

    Attributes:
        slots: Every held reading, keyed by ``(device_id, mark)``.
        watermarks: Per device, the baseline mark of the first owed slot: the last mark a confirmed batch covered, or
            the mark a new registration set.
    """

    slots: dict[tuple[str, datetime], DeviceSlot] = field(default_factory=dict)
    watermarks: dict[str, datetime] = field(default_factory=dict)


def retention_floor(now: datetime) -> datetime:
    """Return the oldest mark the archive keeps at ``now``."""
    return slot_start(now) - timedelta(hours=DEVICE_SLOT_RETENTION_HOURS)


def record(log: SlotLog, slot: DeviceSlot) -> bool:
    """Store one device's reading under its slot, replacing the slot's earlier reading.

    Returns:
        True when this changed the archive.
    """
    if log.slots.get(slot.key) == slot:
        return False
    log.slots[slot.key] = slot
    return True


def read_device(
    log: SlotLog, device_id: str, now: datetime, *, unconfirmed_only: bool = False,
) -> list[DeviceSlot]:
    """Return one device's archive, earliest first, without the slot in progress.

    The slot in progress is provisional, so it is never offered: whoever differences readings sees only readings that
    can no longer change.

    Args:
        now: The current instant, which fixes the retention floor.
        unconfirmed_only: Start at the device's watermark reading, the baseline for the first owed slot, instead of at
            the floor. A watermark older than the floor is no baseline: the read starts at the first reading inside the
            window. A device with no watermark yields the same either way.
    """
    floor, current = retention_floor(now), slot_start(now)
    mark = log.watermarks.get(device_id) if unconfirmed_only else None
    start = floor if mark is None else max(floor, mark)
    found = [
        slot for (owner, at), slot in log.slots.items()
        if owner == device_id and start <= at < current
    ]
    return sorted(found, key=lambda slot: slot.start)


def silent_devices(
    log: SlotLog, held_since: Mapping[str, datetime], now: datetime,
    *, after: timedelta = timedelta(hours=SILENT_METER_HOURS),
) -> dict[str, datetime]:
    """Return the held devices that have gone quiet, device id to the instant the silence began.

    Silence runs from the newest reading but never from before ``held_since``, so a device only just watched gets the
    full ``after`` to produce a first reading. A withdrawn device is not in ``held_since`` and so cannot be silent.

    Args:
        held_since: Per held device, when this process first saw it held.
        after: How long without a reading counts as silent; strictly longer is silent.
    """
    silent: dict[str, datetime] = {}
    for device_id, watched_from in held_since.items():
        newest = max(
            (at for (owner, at) in log.slots if owner == device_id), default=watched_from,
        )
        since = max(newest, watched_from)
        if now - since > after:
            silent[device_id] = since
    return silent


def stalled_devices(
    log: SlotLog, expected_since: Mapping[str, datetime], now: datetime,
    *, after: timedelta = timedelta(hours=STALLED_UPLOAD_HOURS),
) -> dict[str, datetime]:
    """Return the devices whose owed readings have not gone out, device id to the instant the wait began.

    A device is owed from its oldest reading past the baseline: the watermark, or with none the earliest reading. The
    wait never counts from before ``expected_since``, so a device the partner only just started taking data for gets the
    full ``after`` for a first upload. A device with nothing owed is not stalled; a meter that stopped reporting is
    :func:`silent_devices`' business.

    Args:
        expected_since: Per uploadable device, when this process first saw the partner take data for it.
        after: How long an owed reading may wait; strictly longer is stalled.
    """
    stalled: dict[str, datetime] = {}
    for device_id, watched_from in expected_since.items():
        marks = sorted(at for (owner, at) in log.slots if owner == device_id)
        if not marks:
            continue
        baseline = log.watermarks.get(device_id, marks[0])
        owed = next((at for at in marks if at > baseline), None)
        if owed is None:
            continue
        since = max(owed, watched_from)
        if now - since > after:
            stalled[device_id] = since
    return stalled


def confirm(log: SlotLog, device_id: str, through: datetime) -> bool:
    """Mark everything up to ``through`` as received by the partner; never moves backwards.

    A late acknowledgement of an older batch must not re-open readings a newer one already covered.

    Args:
        through: The mark of the last reading the confirmed batch used, its ``end``; an instant inside a quarter-hour
            counts as that mark.

    Returns:
        True when the watermark moved.
    """
    through = slot_start(through)
    current = log.watermarks.get(device_id)
    if current is not None and current >= through:
        return False
    log.watermarks[device_id] = through
    return True


def start_registration(log: SlotLog, device_id: str, requested_at: datetime) -> bool:
    """Set the device's watermark for a new registration requested at ``requested_at``, keeping its readings.

    The watermark goes to the mark before the slot in progress at the request, whether or not the archive holds a
    reading there, so nothing from before the request is owed: a watermark kept from an earlier registration would owe
    every slot since, a month withdrawn being a month of ``null`` slots. With a reading at that mark the live push
    starts where the history dump ends; without it the first reading after the mark is the baseline.

    Returns:
        True when the watermark changed.
    """
    mark = slot_start(requested_at) - timedelta(minutes=SLOT_MINUTES)
    if log.watermarks.get(device_id) == mark:
        return False
    log.watermarks[device_id] = mark
    return True


def prune(log: SlotLog, now: datetime, device_ids: Collection[str]) -> bool:
    """Drop what nothing can consume: aged-out readings and unlinked devices' readings and watermarks.

    Args:
        device_ids: Ids of the devices that are still linked.

    Returns:
        True when the archive changed.
    """
    keep, floor = set(device_ids), retention_floor(now)
    stale = [key for key, slot in log.slots.items() if slot.device_id not in keep or slot.start < floor]
    for key in stale:
        del log.slots[key]
    gone = [device_id for device_id in log.watermarks if device_id not in keep]
    for device_id in gone:
        del log.watermarks[device_id]
    return bool(stale or gone)


def serialize_slot_log(value: SlotLog) -> dict[str, Any]:
    """Serialise the archive to a JSON-safe payload, its records in time order."""
    ordered = sorted(value.slots.values(), key=lambda slot: (slot.start, slot.device_id))
    return {
        "slots": [slot.as_payload() for slot in ordered],
        "watermarks": {
            device_id: mark.isoformat(timespec="seconds")
            for device_id, mark in sorted(value.watermarks.items())
        },
    }


def deserialize_slot_log(payload: dict[str, Any]) -> SlotLog:
    """Rebuild the archive from a stored payload, skipping corrupt records.

    Losing one mark is far cheaper than losing the window.
    """
    slots: dict[tuple[str, datetime], DeviceSlot] = {}
    for raw in payload.get("slots") or ():
        slot = DeviceSlot.from_payload(raw)
        if slot is not None:
            slots[slot.key] = slot
    watermarks: dict[str, datetime] = {}
    for device_id, raw_mark in (payload.get("watermarks") or {}).items():
        mark = parse_instant(raw_mark)
        if mark is not None:
            watermarks[str(device_id)] = slot_start(mark)
    return SlotLog(slots=slots, watermarks=watermarks)
