"""Keep one meter reading per device per quarter-hour slot, the last one taken in it: the only writer of the archive.

While an account is stored, every linked device in every state is read on each quarter-hour mark, between marks and at
each contact with the partner, whether or not the partner holds it; whether a reading is sent is the upload's business.
Only the silent-meter watch follows the devices the partner holds. A reading is stored under the slot it lands in and
is provisional until that slot ends, since each read overwrites the slot's earlier one. A counter with no usable
reading stores nothing, never a fabricated zero.
"""
from __future__ import annotations

import logging
from collections.abc import Callable, Sequence
from datetime import datetime
from typing import Any

from ..contract import DeviceEnergy, DeviceSlot, MeteredDevice, slot_start
from ..inbound import read_device_energy
from .ha_store import Store
from .slot_log import SlotLog, confirm, prune, record, silent_devices, start_registration

_LOGGER = logging.getLogger(__name__)

ReadEnergy = Callable[[Any, MeteredDevice], DeviceEnergy]


def _log_watermark(level: int, device_id: str, before: datetime | None, after: datetime | None, reason: str) -> None:
    """Log one watermark move of ``device_id``, an absent watermark reading as ``none``.

    Args:
        level: DEBUG for an advance, INFO for a set or a drop, so a normal cycle is silent at INFO.
        device_id: The device by id; an unlinked device has no name left.
        before: The watermark before the move, None when there was none.
        after: The watermark after the move, None when it was dropped.
        reason: Why it moved.
    """
    _LOGGER.log(
        level, "Watermark of %s: %s -> %s (%s)", device_id,
        "none" if before is None else before.isoformat(timespec="minutes"),
        "none" if after is None else after.isoformat(timespec="minutes"),
        reason,
    )


class SlotCollector:
    """Samples one installation's metered devices into its 72-hour archive."""

    def __init__(
        self,
        *,
        hass: Any,
        store: Store[SlotLog],
        read_devices: Callable[[], Sequence[MeteredDevice]],
        read_held: Callable[[], frozenset[str]],
        read_active: Callable[[], bool],
        read_energy: ReadEnergy | None = None,
    ) -> None:
        """Bind the sampler to its store, its devices and the meter read.

        Args:
            hass: Home Assistant instance; only the meter read touches it.
            store: The archive's store; a None ``value`` reads as an empty archive.
            read_devices: Returns the installation's linked metered devices.
            read_held: Returns the ids of the devices the partner holds, which the silent-meter watch follows.
            read_active: Returns True while an account is stored; only then is any device read.
            read_energy: Reads one device's counters through its kind's profile; injectable for tests.
        """
        self._hass = hass
        self._store = store
        self._read_devices = read_devices
        self._read_held = read_held
        self._read_active = read_active
        self._read_energy = read_energy or read_device_energy
        # In memory on purpose: a restart grants every device a fresh grace period, so a long outage does not raise a
        # silent-meter issue for each one before its sensors have had a chance to report.
        self._held_since: dict[str, datetime] = {}

    @property
    def log(self) -> SlotLog:
        """Return the archive, a fresh empty one before the first reading."""
        return self._store.value or SlotLog()

    async def async_collect(self, now: datetime) -> bool:
        """Take the readings of the slot ``now`` falls in, then trim what has aged out.

        Every linked device is read while an account is stored, whatever its status; none otherwise. The trim runs
        either way.

        Returns:
            True when the archive changed and was saved.
        """
        # No await between reading the log and the ``record`` calls: the host's quarter-hour timer and the runtime's
        # reconcile (before it takes its lock) both drive this without a lock, and only this keeps one from reading a
        # half-written archive. The first await is the save, with the log complete.
        log = self.log
        devices = list(self._read_devices())
        held = self._read_held()
        mark = slot_start(now)
        self._held_since = {
            device_id: self._held_since.get(device_id, now) for device_id in held
        }
        changed = False
        sampled = devices if self._read_active() else []
        for device in sampled:
            energy = self._read_energy(self._hass, device)
            if energy.consumed_kwh is None:
                _LOGGER.debug(
                    "No reading from %s in the slot of %s; keeping what the slot holds",
                    device.name, mark.isoformat(timespec="minutes"),
                )
                continue
            changed = record(log, DeviceSlot(
                device_id=device.id,
                start=mark,
                consumed_kwh=energy.consumed_kwh,
                returned_kwh=energy.returned_kwh,
            )) or changed
        # Last, so the readings above saw the full retention window.
        before = dict(log.watermarks)
        changed = prune(log, now, [device.id for device in devices]) or changed
        for device_id, mark in before.items():
            if device_id not in log.watermarks:
                _log_watermark(logging.INFO, device_id, mark, None, "device unlinked")
        if changed:
            await self._store.save(log)
        return changed

    def forget_held(self) -> None:
        """Forget when each device was first seen held, so a partner coming back later gets a fresh grace period."""
        self._held_since = {}

    def silent_devices(self, now: datetime) -> dict[str, datetime]:
        """Return the held devices that have stopped reporting, device id to the instant the silence began."""
        return silent_devices(self.log, self._held_since, now)

    async def async_confirm(self, device_id: str, through: datetime) -> bool:
        """Record what the partner has received, by the confirmed batch's ``end``.

        Here and not on the runtime because this object owns the archive and its store.

        Args:
            through: The mark of the last reading the confirmed batch used.

        Returns:
            True when the watermark moved and was saved.
        """
        log = self.log
        before = log.watermarks.get(device_id)
        if not confirm(log, device_id, through):
            return False
        _log_watermark(logging.DEBUG, device_id, before, log.watermarks[device_id], "confirmed")
        await self._store.save(log)
        return True

    async def async_start_registration(self, device_id: str, requested_at: datetime) -> None:
        """Set a newly registered device's watermark at the request, keeping its readings.

        Args:
            requested_at: The instant the new registration was first seen.
        """
        log = self.log
        before = log.watermarks.get(device_id)
        if not start_registration(log, device_id, requested_at):
            return
        await self._store.save(log)
        _log_watermark(logging.INFO, device_id, before, log.watermarks[device_id], "new registration")

    async def async_reset(self) -> None:
        """Drop the whole archive, every reading and every watermark, as logging out leaves no partner data behind."""
        before = dict(self.log.watermarks)
        self.forget_held()
        await self._store.save(SlotLog())
        for device_id, mark in before.items():
            _log_watermark(logging.INFO, device_id, mark, None, "logout")
