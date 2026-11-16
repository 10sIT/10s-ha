"""Home Assistant wiring of the kit's ``PartnerRuntime``: its two stores, the meter reads and the contact with 10s."""
from __future__ import annotations

import logging
from collections.abc import Callable, Coroutine, Mapping
from datetime import UTC, datetime, timedelta
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.event import async_track_time_interval, async_track_utc_time_change
from homeassistant.helpers.storage import Store
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator
from homeassistant.util import dt as dt_util

from . import environment
from ._tenskit.contract import CONF_TENS_ACCOUNT, CONTACT_INTERVAL_HOURS, SLOT_MINUTES, PartnerAccount
from ._tenskit.orchestration import (
    PartnerRuntime,
    Registrations,
    SlotLog,
    deserialize_registrations,
    deserialize_slot_log,
    serialize_registrations,
    serialize_slot_log,
)
from ._tenskit.outbound.partners import PartnerAuthError
from .const import DOMAIN, STORAGE_VERSION

_LOGGER = logging.getLogger(__name__)

# The coordinator's refresh is the contact with 10s. Home Assistant schedules the next one this long after the
# last attempt, whether it got through or not; each installation keeps the phase its start gave it.
_UPDATE_INTERVAL = timedelta(hours=CONTACT_INTERVAL_HOURS)
# Local meter reads between the quarter-hour marks; a slot keeps the last reading taken in it.
_SAMPLE_INTERVAL = timedelta(minutes=5)

# Repair-issue keys, also the ``translation_key``s under ``issues`` in translations/en.json.
_ISSUE_SILENT_METER = "silent_meter"
_ISSUE_UPLOAD_STALLED = "upload_stalled"
_ISSUE_HISTORY_GIVEN_UP = "history_given_up"


class RegistrationStore:
    """The registration mirror's HA-backed store; ``value`` is None until the first load or save."""

    def __init__(self, hass: HomeAssistant, entry_id: str) -> None:
        """Bind the store to its entry's namespaced HA store."""
        self._store = Store(hass, STORAGE_VERSION, f"{DOMAIN}_{entry_id}_device_lifecycle")
        self.value: Registrations | None = None

    async def load(self) -> Registrations | None:
        """Load the mirror from disk, skipping corrupt rows; None when the store is empty."""
        raw = await self._store.async_load()
        if raw is None:
            return None
        self.value = deserialize_registrations(raw)
        return self.value

    async def save(self, value: Registrations) -> None:
        """Persist the mirror as the store's new value."""
        self.value = value
        await self._store.async_save(serialize_registrations(value))

    async def async_remove(self) -> None:
        """Delete the mirror's file; one never written is not an error (HA's ``Store`` ignores a missing file)."""
        self.value = None
        await self._store.async_remove()


class SlotStore:
    """The slot archive's HA-backed store; ``value`` is None until the first load or save."""

    def __init__(self, hass: HomeAssistant, entry_id: str) -> None:
        """Bind the store to its entry's namespaced HA store."""
        self._store = Store(hass, STORAGE_VERSION, f"{DOMAIN}_{entry_id}_device_slots")
        self.value: SlotLog | None = None

    async def load(self) -> SlotLog | None:
        """Load the archive from disk, skipping corrupt records; None when the store is empty."""
        raw = await self._store.async_load()
        if raw is None:
            return None
        self.value = deserialize_slot_log(raw)
        return self.value

    async def save(self, value: SlotLog) -> None:
        """Persist the archive as the store's new value."""
        self.value = value
        await self._store.async_save(serialize_slot_log(value))

    async def async_remove(self) -> None:
        """Delete the archive's file; one never written is not an error (HA's ``Store`` ignores a missing file)."""
        self.value = None
        await self._store.async_remove()


class TensCoordinator(DataUpdateCoordinator):
    """One 10s config entry: its account, devices and registration mirror."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        """Build the runtime over this entry and start the reconcile cadence."""
        super().__init__(
            hass,
            _LOGGER,
            name=DOMAIN,
            update_interval=_UPDATE_INTERVAL,
            config_entry=entry,
        )
        self._entry = entry
        self._store = RegistrationStore(hass, entry.entry_id)
        self._slot_store = SlotStore(hass, entry.entry_id)
        # Per issue key, the devices with that issue open right now; the issues are not persisted either.
        self._raised: dict[str, set[str]] = {
            _ISSUE_SILENT_METER: set(), _ISSUE_UPLOAD_STALLED: set(), _ISSUE_HISTORY_GIVEN_UP: set(),
        }
        # In memory only: the reload a completed reauth triggers builds a new coordinator.
        self.auth_failed = False
        # Fixed for the coordinator's life: a new login or a reauth writes the options, which reloads the entry.
        self.environment = environment.of_account(PartnerAccount.from_config(entry.options.get(CONF_TENS_ACCOUNT)))
        self.partner = PartnerRuntime(
            read_config=self._read_options,
            store=self._store,
            slot_store=self._slot_store,
            client=environment.make_client(hass, self.environment),
            request_refresh=self._async_request_refresh,
            update_options=self._async_update_entry_options,
            start_job=self._start_job,
            hass=hass,
        )

    async def async_setup(self) -> None:
        """Load both stores into the runtime before the first sync."""
        await self._store.load()
        await self._slot_store.load()

    # --- The runtime's entry hooks ------------------------------------------

    def _read_options(self) -> Mapping[str, Any]:
        """Return the entry's options, where the account and the devices live (its data is empty)."""
        return self._entry.options

    def _start_job(self, job: Coroutine[Any, Any, None]) -> None:
        """Run a runtime job as the entry's background task, cancelled with the entry; drop it once unloaded.

        A job writes the registration mirror, so it may live no longer than this coordinator. A register that was
        waiting on 10s while the entry reloaded would otherwise add its job to the reloaded entry, beside the new
        coordinator writing the same store. Dropped, the history stays owed and the new coordinator's contact offers it.
        """
        if self.hass.data.get(DOMAIN) is not self:
            job.close()
            _LOGGER.debug("The entry was reloaded during a register; its history goes at the next contact")
            return
        self._entry.async_create_background_task(self.hass, job, f"{DOMAIN}: history push")

    async def _async_request_refresh(self) -> None:
        """Show an action's result now: re-check the repairs and update the entities, without contacting 10s."""
        self._sync_repair_issues(datetime.now(UTC))
        self.async_update_listeners()

    async def _async_update_entry_options(self, changes: dict) -> None:
        """Merge ``changes`` into the entry's options (the runtime's writer); other keys are kept."""
        # ``async_update_entry`` is a plain callback, not a coroutine: awaiting it raises.
        self.hass.config_entries.async_update_entry(
            self._entry, options={**self._entry.options, **changes},
        )

    # --- The meter reads and the contact with 10s ---------------------------

    def async_start_sampling(self) -> Callable[[], None]:
        """Read the meters on each quarter-hour mark and every 5 minutes, without contacting 10s.

        Returns:
            The unsubscribe callable for both timers, for ``entry.async_on_unload``.
        """
        unsubscribes = (
            async_track_utc_time_change(
                self.hass,
                self._async_on_sample,
                minute=tuple(range(0, 60, SLOT_MINUTES)),
                second=0,
            ),
            async_track_time_interval(self.hass, self._async_on_sample, _SAMPLE_INTERVAL),
        )

        def unsubscribe() -> None:
            """Stop both timers."""
            for unsub in unsubscribes:
                unsub()

        return unsubscribe

    async def _async_on_sample(self, now: datetime) -> None:
        """Read every linked device into its slot, re-check the repairs, update the entities."""
        await self.partner.async_collect(now)
        self._sync_repair_issues(now)
        # The entities otherwise follow the coordinator's refresh, which is not due for hours.
        self.async_update_listeners()

    async def _async_update_data(self) -> dict:
        """Contact 10s: read the meters, reconcile the mirror, upload what is owed; note a refused login."""
        # No await before ``async_sync``: the readings are stamped with ``now``, and an await could cross a
        # quarter-hour mark and file them under a slot that ended.
        now = datetime.now(UTC)
        try:
            await self.partner.async_sync(now)
        except PartnerAuthError as err:
            # Home Assistant opens the reauth flow and stops contacting 10s until it completes.
            self.auth_failed = True
            raise ConfigEntryAuthFailed("10s no longer accepts this login") from err
        finally:
            # Also on failure: a reconcile that keeps failing is what stalls the uploads.
            self._sync_repair_issues(now)
        self.auth_failed = False
        return {}

    # --- Repair issues -------------------------------------------------------

    def _issue_id(self, key: str, device_id: str) -> str:
        """Return the repair-issue id for one device's issue of one kind."""
        return f"{key}_{self._entry.entry_id}_{device_id}"

    def _sync_repair_issues(self, now: datetime) -> None:
        """Bring every per-device repair in line with the kit's predicates.

        Silent meter: a held device with no new reading. Stalled uploads: a device 10s takes data for whose
        owed readings are not going out. History given up: 10s refused the recorded history too often.
        """
        self._sync_issues(_ISSUE_SILENT_METER, self.partner.silent_devices(now))
        self._sync_issues(_ISSUE_UPLOAD_STALLED, self.partner.stalled_devices(now))
        self._sync_issues(_ISSUE_HISTORY_GIVEN_UP, dict.fromkeys(self.partner.given_up_histories()))

    def _sync_issues(self, key: str, found: Mapping[str, datetime | None]) -> None:
        """Open a repair for each device in ``found`` and close the ones that recovered.

        Args:
            key: The issue kind, also its ``translation_key``.
            found: Device id to the instant the condition began, or None when the issue's text shows none.
        """
        raised = self._raised[key]
        names = {device.id: device.name for device in self.partner.devices()}
        for device_id, since in found.items():
            if device_id in raised:
                continue
            placeholders = {"device": names.get(device_id, device_id)}
            if since is not None:
                placeholders["since"] = dt_util.as_local(since).strftime("%Y-%m-%d %H:%M")
            ir.async_create_issue(
                self.hass,
                DOMAIN,
                self._issue_id(key, device_id),
                is_fixable=False,
                severity=ir.IssueSeverity.WARNING,
                translation_key=key,
                translation_placeholders=placeholders,
            )
        for device_id in raised - found.keys():
            ir.async_delete_issue(self.hass, DOMAIN, self._issue_id(key, device_id))
        self._raised[key] = set(found)

    def async_clear_issues(self) -> None:
        """Close every repair this entry has open (the entry is unloading)."""
        for key, raised in self._raised.items():
            for device_id in raised:
                ir.async_delete_issue(self.hass, DOMAIN, self._issue_id(key, device_id))
            self._raised[key] = set()
