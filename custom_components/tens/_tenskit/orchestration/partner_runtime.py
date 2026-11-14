"""Run one installation's partner: its account, device registrations and the actions on them.

With no account stored the runtime is inactive; every read goes through :meth:`_config`. The registration mirror records
what the partner last reported, nothing here invents a status. Each reconcile (:meth:`PartnerRuntime.async_sync`) also
refreshes the categories the account may register, and registering a device outside them is refused before the partner
is called. The last step of a reconcile uploads what each device is owed, moving its watermark only on the partner's
``202``; a refused push is held back for :data:`REJECTED_RETRY`. A withdrawn device still sends the slots up to its
``effectiveTo`` that it owes: more than the effective window is acceptable, a gap is not. The history dump is owed once
per registration until the partner accepts it, and given up after ``MAX_BACKFILL_REFUSALS`` refusals.
"""
from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import Enum
from typing import Any

from ..contract import (
    CONF_METERED_DEVICES,
    CONF_TENS_ACCOUNT,
    TENS_STATUS_REGISTERED,
    TENS_STATUS_WITHDRAWING,
    TENS_STATUS_WITHDRAWN,
    DeviceSlot,
    MeteredDevice,
    PartnerAccount,
    RegistrationState,
    as_config_list,
    is_held,
    metered_devices,
    normalize_color,
)
from ..inbound import DumpSeries, RecorderReadError, RegistrationDump, async_build_dump
from ..outbound.partners import (
    MAX_BLOCK_VALUES,
    METRIC_CONSUMPTION,
    METRIC_PRODUCTION,
    SLOT_GRANULARITY,
    MeasurementBlock,
    PartnerClient,
    PartnerConnectionError,
    PartnerRejectedError,
    encode_device,
    series_blocks,
)
from .ha_store import Store
from .registrations import (
    MAX_BACKFILL_REFUSALS,
    Registrations,
    current_status,
    ensure_removable,
    forget,
    is_backfill_given_up,
    is_eligible,
    note_registration,
    owe_backfill,
    owes_backfill,
    prune,
    record,
    record_eligible,
    refuse_backfill,
    serialize_devices,
    set_color,
    settle_backfill,
)
from .slot_collector import SlotCollector
from .slot_log import SlotLog, read_device, stalled_devices

_LOGGER = logging.getLogger(__name__)

# That 10s takes data while ``withdrawing`` and none before the device is effective are assumptions the spec does not
# settle. A ``registering`` device's readings wait in the archive and go with its first upload.
UPLOAD_STATUSES = frozenset({TENS_STATUS_REGISTERED, TENS_STATUS_WITHDRAWING})
# A refusal is an encoder bug or a device 10s no longer holds, so resending at once would only repeat it. The hold is on
# the partner id that was refused: a new id lifts it at once.
REJECTED_RETRY = timedelta(hours=1)
SLOT_ACCOUNTED = "accounted"
SLOT_OWED = "owed"
SLOT_OUTSIDE = "outside"


class _Push(Enum):
    """What one push to the partner carries; each is held back on its own after a refusal.

    Separate so a history dump 10s keeps refusing never stops the live readings, and a refused reading never delays the
    dump.
    """

    READINGS = "readings"
    HISTORY = "history"


@dataclass(frozen=True)
class PartnerSnapshot:
    """What the panel and the dashboard sensor need about the partner.

    Attributes:
        account: The logged-in account, or None.
        devices: One dict per linked device, the fields the panel reads (``registrations.serialize_devices``).
        eligible: Category to the partner's label as last cached; None until asked.
    """

    account: PartnerAccount | None = None
    devices: tuple[dict, ...] = ()
    eligible: Mapping[str, str] | None = None


class PartnerRuntime:
    """One installation's partner: its account, devices and registrations."""

    def __init__(
        self,
        *,
        read_config: Callable[[], Mapping[str, Any]],
        store: Store[Registrations],
        slot_store: Store[SlotLog],
        client: PartnerClient,
        request_refresh: Callable[[], Awaitable[None]],
        update_options: Callable[[dict], Awaitable[None]],
        hass: Any,
        max_request_values: int = MAX_BLOCK_VALUES,
    ) -> None:
        """Bind the runtime to its entry, its two stores and the partner client.

        Args:
            read_config: Returns the entry's merged data and options.
            store: The registration mirror's store; a None ``value`` reads as an empty mirror.
            slot_store: The 15-minute archive's store; likewise.
            client: The partner's client, the authority on every status.
            request_refresh: Tells the host an action changed the state.
            update_options: Rewrites the entry's options, so a removal survives a reload.
            hass: Home Assistant instance, through which the collector reads the meters and the dump the recorder.
            max_request_values: The most values one upload request carries, both metric types together; a test seam.
        """
        self._read_config = read_config
        self._hass = hass
        self._store = store
        self._client = client
        self._request_refresh = request_refresh
        self._update_options = update_options
        self._max_request_values = max_request_values
        # Every change awaits the partner between reading the mirror and saving it, so without this a register or a
        # logout landing inside a reconcile is overwritten by the reconcile's older answer. Released before the refresh
        # request: a host whose refresh reconciles would otherwise wait on itself (asyncio.Lock is not re-entrant).
        self._lock = asyncio.Lock()
        # In memory: a restart simply tries once more.
        self._rejected_until: dict[tuple[str, _Push], tuple[datetime, int]] = {}
        # In memory, like the collector's hold watch: a restart grants every device a fresh grace period before its
        # uploads count as stalled.
        self._upload_expected_since: dict[str, datetime] = {}
        self._collector = SlotCollector(
            hass=hass,
            store=slot_store,
            read_devices=self.devices,
            read_held=self.held_device_ids,
            read_active=lambda: self.active,
        )

    def _config(self) -> Mapping[str, Any] | None:
        """Return the merged entry config, or None when no 10s account is stored."""
        merged = self._read_config()
        return merged if PartnerAccount.from_config(merged.get(CONF_TENS_ACCOUNT)) is not None else None

    @property
    def active(self) -> bool:
        """Return True when the partner is part of this installation."""
        return self._config() is not None

    @property
    def account(self) -> PartnerAccount | None:
        """Return the logged-in account, or None when absent or not installed."""
        merged = self._config()
        if merged is None:
            return None
        return PartnerAccount.from_config(merged.get(CONF_TENS_ACCOUNT))

    def devices(self) -> list[MeteredDevice]:
        """Return the installation's linked metered devices."""
        return list(metered_devices(self._read_config()))

    def _registrations(self) -> Registrations:
        """Return the registration mirror, a fresh empty one before the first sync."""
        return self._store.value or Registrations()

    def snapshot(self, now: datetime | None = None) -> PartnerSnapshot:
        """Return the panel's view: the account and one row per linked device with its registration.

        Args:
            now: The instant to evaluate pending transitions against.
        """
        account = self.account
        moment = now if now is not None else datetime.now(UTC)
        return PartnerSnapshot(
            account=account,
            devices=tuple(serialize_devices(self.devices(), self._registrations(), moment)),
            eligible=self._registrations().eligible if account is not None else None,
        )

    def device(self, device_id: str) -> dict | None:
        """Return one device's panel view, or None when it is not linked."""
        for device in self.snapshot().devices:
            if device.get("id") == device_id:
                return device
        return None

    def held_device_ids(self) -> frozenset[str]:
        """Return the devices the partner still holds, which may not be changed or dropped; none without a partner."""
        if not self.active:
            return frozenset()
        registrations = self._registrations()
        return frozenset(
            device.id for device in self.devices() if is_held(current_status(registrations, device.id))
        )

    @property
    def slot_log(self) -> SlotLog:
        """Return the 72-hour archive, empty until its store is loaded."""
        return self._collector.log

    def device_archive(
        self, device_id: str, now: datetime | None = None, *, unconfirmed_only: bool = False,
    ) -> list[DeviceSlot]:
        """Return one device's archived readings, earliest first.

        Args:
            now: The instant the 72-hour retention window is measured from.
            unconfirmed_only: True for what the partner is still owed, from the watermark reading on; False for the
                whole window.
        """
        moment = now if now is not None else datetime.now(UTC)
        return read_device(self.slot_log, device_id, moment, unconfirmed_only=unconfirmed_only)

    def last_confirmed(self, device_id: str) -> datetime | None:
        """Return the mark the live upload is owed from for a device, or None when nothing has set one.

        That is the mark of the last reading the partner confirmed, or between a new registration and its first accepted
        upload the mark before the slot in progress at the request.
        """
        return self.slot_log.watermarks.get(device_id)

    def slot_accounting(self, device_id: str, start: datetime) -> str:
        """Return how the partner accounts a device's slot starting at ``start``; see :func:`slot_accounting`."""
        return slot_accounting(self._registrations().rows.get(device_id), self.last_confirmed(device_id), start)

    def silent_devices(self, now: datetime | None = None) -> dict[str, datetime]:
        """Return the held devices that have stopped reporting, device id to the instant the silence began.

        Args:
            now: The instant to measure silence against.

        Returns:
            Empty without a partner, since nothing is expected then.
        """
        if not self.active:
            return {}
        return self._collector.silent_devices(now if now is not None else datetime.now(UTC))

    def stalled_devices(self, now: datetime | None = None) -> dict[str, datetime]:
        """Return the uploadable devices whose owed readings are not going out.

        Only devices the partner takes data for right now count: a withdrawn device is not watched here, a refusal of
        its slots is logged and the archive drops them once they age out.

        Args:
            now: The instant to measure the wait against.

        Returns:
            Device id to the instant its oldest owed reading began to wait; empty without a partner.
        """
        if not self.active:
            return {}
        uploadable = self._uploadable_ids()
        expected = {
            device_id: since for device_id, since in self._upload_expected_since.items() if device_id in uploadable
        }
        return stalled_devices(self.slot_log, expected, now if now is not None else datetime.now(UTC))

    def _uploadable_ids(self) -> set[str]:
        """Return the linked devices the partner takes data for, under a known id."""
        rows = self._registrations().rows
        return {
            device.id for device in self.devices()
            if (state := rows.get(device.id)) is not None
            and state.status in UPLOAD_STATUSES and state.remote_id is not None
        }

    async def async_sync(self, now: datetime) -> bool:
        """Reconcile the mirror with the partner, persisting only on a change, then upload what is owed.

        The meters are read first (:meth:`async_collect`), before anything waits on the lock or the partner. Under the
        lock it drops rows for devices that are gone, takes the partner's status for the rest, leaves the ones it never
        saw free (``withdrawn``) until the panel registers them, and refreshes the eligible categories. Whatever the
        partner reports wins, including a transition it landed on its own. A partner that cannot be reached leaves the
        mirror as it is.

        Args:
            now: The current UTC instant, taken by the caller with no await between taking it and this call.

        Returns:
            True when the mirror changed and was saved.

        Raises:
            PartnerAuthError: The partner no longer accepts the credentials.
        """
        # Before the lock and any partner call: a wait there can cross a quarter-hour mark, and a reading taken after it
        # would be filed under a slot that has already ended, over-stating it. The archive needs no lock because the
        # collector has no await between reading its log and recording into it.
        await self.async_collect(now)
        async with self._lock:
            return await self._async_reconcile(now)

    async def _async_reconcile(self, now: datetime) -> bool:
        """Run one reconcile; the caller has collected and holds the lock."""
        if not self.active:
            self._collector.forget_held()
            self._upload_expected_since = {}
            return False
        devices = self.devices()
        registrations = self._registrations()
        saved = False
        changed = prune(registrations, [device.id for device in devices])
        account, client = self.account, self._client
        if account is not None:
            changed = await self._ask_partner(account, client, devices, registrations, now) or changed
            if changed:
                # Before the second call: an auth error from it must not cost the listing's change.
                await self._save(registrations)
                changed, saved = False, True
            changed = await self._ask_eligible(account, client, registrations)
        if changed:
            await self._save(registrations)
            saved = True
        # Last, against the statuses just recorded: a device this listing shows effective is uploaded in this contact,
        # not the next.
        await self._async_upload(now)
        return saved

    async def async_collect(self, now: datetime) -> bool:
        """Read the meters into the current quarter-hour's slot, independent of the partner.

        Called without the lock by the host on each mark and between marks, and first thing by :meth:`async_sync`. The
        slot ends up holding the last reading taken in it.

        Returns:
            True when the archive changed and was saved.
        """
        return await self._collector.async_collect(now)

    async def _ask_partner(
        self,
        account: PartnerAccount,
        client: PartnerClient,
        devices: list[MeteredDevice],
        registrations: Registrations,
        now: datetime,
    ) -> bool:
        """Record the partner's statuses, recording a device it does not list as ``withdrawn``.

        A partner that cannot be reached, or refuses the listing, leaves the mirror as it is, so the cycle still
        collects and uploads.

        Args:
            registrations: The mirror to update in place.

        Returns:
            True when the mirror changed.

        Raises:
            PartnerAuthError: The partner no longer accepts the credentials.
        """
        try:
            reported = await client.async_statuses(account, [device.id for device in devices])
        except PartnerConnectionError:
            _LOGGER.debug("10s unreachable; keeping the last known registrations")
            return False
        except PartnerRejectedError as err:
            _LOGGER.warning(
                "10s refused to list the devices (HTTP %s); keeping the last known registrations", err.status,
            )
            return False
        changed = False
        for device_id, state in reported.items():
            changed = await self._async_record_reported(registrations, device_id, state, now) or changed
        for device in devices:
            if device.id not in reported:
                self._warn_if_registration_lost(registrations, device)
                state = RegistrationState(status=TENS_STATUS_WITHDRAWN, pending_until=None)
                changed = record(registrations, device.id, state) or changed
        return changed

    @staticmethod
    def _warn_if_registration_lost(registrations: Registrations, device: MeteredDevice) -> None:
        """Warn when a device 10s had registered is no longer in its listing.

        The row is about to be recorded free with no partner id, so the next reconcile finds nothing to warn about. The
        cause is a conflict on 10s's side, such as another app registration or a row deleted there, resolved with 10s.
        """
        previous = registrations.rows.get(device.id)
        if previous is None or previous.remote_id is None:
            return
        _LOGGER.warning(
            "10s no longer lists %s (10s device %s) under this app registration; it now reads as not registered",
            device.name, previous.remote_id,
        )

    async def _async_record_reported(
        self, registrations: Registrations, device_id: str, state: RegistrationState, now: datetime,
    ) -> bool:
        """Record a partner report on a device, starting a new registration where its partner id is new.

        A new ``remote_id`` is a new registration wherever it is first seen: the register answer, the status listing
        that adopts a register whose answer was lost, or a withdrawal that meets it. Its history dump is then owed, the
        moment is noted, and the device's watermark is set to the mark before the slot in progress, so the dump and the
        live upload meet and nothing from before the request is owed live. The caller holds the lock and saves.

        Args:
            registrations: The mirror, mutated in place.

        Returns:
            True when the mirror changed.
        """
        previous = registrations.rows.get(device_id)
        changed = record(registrations, device_id, state)
        remote_id = state.remote_id
        if remote_id is not None and (previous is None or previous.remote_id != remote_id):
            await self._collector.async_start_registration(device_id, now)
            owe_backfill(registrations, device_id, remote_id)
            note_registration(registrations, device_id, now)
        return changed

    async def _ask_eligible(
        self, account: PartnerAccount, client: PartnerClient, registrations: Registrations,
    ) -> bool:
        """Refresh the cached list of registrable categories.

        A partner that cannot be reached or refuses the question leaves the cached list as it is.

        Returns:
            True when the cached list changed.

        Raises:
            PartnerAuthError: The partner no longer accepts the credentials.
        """
        try:
            eligible = await client.async_eligible_categories(account)
        except PartnerConnectionError:
            _LOGGER.debug("10s unreachable; keeping the last eligible categories")
            return False
        except PartnerRejectedError as err:
            _LOGGER.warning(
                "10s refused to list the eligible categories (HTTP %s); keeping the last ones", err.status,
            )
            return False
        return record_eligible(registrations, eligible)

    async def _async_upload(self, now: datetime) -> int:
        """Push every uploadable device's owed readings to the partner; the caller holds the lock.

        A device is uploaded while it is ``registered`` or ``withdrawing`` with a known ``remote_id``: first its history
        dump if still owed, then its live readings. Once ``withdrawn``, only the readings up to its ``effectiveTo`` go.
        An unreachable partner ends the pass, since every other device would fail the same way. A refusal holds back
        only what was refused, for :data:`REJECTED_RETRY` or until the partner reports the device under another id.

        Returns:
            How many requests the partner accepted.

        Raises:
            PartnerAuthError: The credentials no longer work (reauth).
        """
        account, client = self.account, self._client
        if account is None:
            return 0
        self._upload_expected_since = {
            device_id: self._upload_expected_since.get(device_id, now) for device_id in self._uploadable_ids()
        }
        rows = self._registrations().rows
        accepted = 0
        for device in self.devices():
            state = rows.get(device.id)
            if state is None or state.remote_id is None or not is_uploaded(state):
                continue
            remote_id = state.remote_id
            until = upload_end(state)
            try:
                if until is None:
                    accepted += await self._async_push_unless_held(
                        _Push.HISTORY, device, remote_id, now,
                        lambda: self._async_backfill_device(account, client, device, remote_id, now),
                    )
                accepted += await self._async_push_unless_held(
                    _Push.READINGS, device, remote_id, now,
                    lambda: self._async_upload_device(account, client, device.id, remote_id, now, until),
                )
            except PartnerConnectionError:
                _LOGGER.debug("10s unreachable; the owed readings go at the next contact")
                break
        return accepted

    async def _async_push_unless_held(
        self,
        push: _Push,
        device: MeteredDevice,
        remote_id: int,
        now: datetime,
        send: Callable[[], Awaitable[int]],
    ) -> int:
        """Run one kind of push for a device unless a refusal of it is still being held back.

        Args:
            push: What ``send`` carries, the key its refusal is held under.
            remote_id: The partner's id for the device; a hold on another id is lifted.
            send: Pushes it, returning how many requests were accepted.

        Returns:
            How many requests the partner accepted; 0 while held or when refused.

        Raises:
            PartnerConnectionError: The partner could not be reached.
        """
        key = (device.id, push)
        held_until, refused_id = self._rejected_until.get(key, (now, remote_id))
        if now < held_until and refused_id == remote_id:
            return 0
        try:
            accepted = await send()
        except PartnerRejectedError as err:
            self._rejected_until[key] = (now + REJECTED_RETRY, remote_id)
            _LOGGER.warning(
                "10s refused the %s of %s (HTTP %s)%s; holding it back for %s",
                push.value, device.name, err.status, f": {err}" if str(err) else "", REJECTED_RETRY,
            )
            return 0
        self._rejected_until.pop(key, None)
        return accepted

    async def _async_backfill_device(
        self, account: PartnerAccount, client: PartnerClient, device: MeteredDevice, remote_id: int, now: datetime,
    ) -> int:
        """Deliver a device's recorded history if the registration is still owed it.

        The recorder is read only while the dump is owed. A recorder holding nothing is a delivered dump of nothing; one
        that could not be read is not, so the dump stays owed.

        Args:
            now: The current UTC instant; the dump is as of it only when the registration's moment was not recorded.

        Returns:
            How many requests were accepted.

        Raises:
            PartnerRejectedError: The partner refused a request; the refusal is counted and the dump offered again after
                the hold unless that refusal gave it up.
            PartnerConnectionError: The partner could not be reached.
        """
        registrations = self._registrations()
        if not owes_backfill(registrations, device.id, remote_id):
            return 0
        # As of the registration, not of this call: a dump retried later must not run over slots the live upload sent.
        try:
            dump = await async_build_dump(self._hass, device, registrations.registered_at.get(device.id, now))
        except RecorderReadError as err:
            _LOGGER.warning("Could not read the recorded history of %s; it stays owed: %s", device.name, err)
            return 0
        accepted = 0
        for blocks in dump_requests(dump, self._max_request_values):
            try:
                await client.async_push_measurements(account, remote_id, blocks)
            except PartnerRejectedError:
                _LOGGER.warning("Refused history blocks for %s: %s", device.id, _describe(blocks))
                await self._async_count_refused_history(registrations, device, remote_id)
                raise
            accepted += 1
        if settle_backfill(registrations, device.id, remote_id):
            await self._save(registrations)
        return accepted

    async def _async_count_refused_history(
        self, registrations: Registrations, device: MeteredDevice, remote_id: int,
    ) -> None:
        """Count a refusal of the device's owed history, giving it up with a warning at the limit.

        Args:
            registrations: The mirror, mutated in place and saved.
            remote_id: The partner id it was refused under.
        """
        given_up = refuse_backfill(registrations, device.id, remote_id)
        await self._save(registrations)
        if given_up:
            _LOGGER.warning(
                "10s refused the recorded history of %s %s times; giving up on it. Its live readings still go "
                "out. To send the history again, fix the cause, withdraw the device and register it again once it "
                "reads withdrawn: a new registration offers its history afresh",
                device.name, MAX_BACKFILL_REFUSALS,
            )

    def given_up_histories(self) -> frozenset[str]:
        """Return the held devices whose recorded history the partner refused too often to be offered again.

        A device drops out once it is withdrawn or removed; a new registration offers its history afresh.
        """
        if not self.active:
            return frozenset()
        registrations = self._registrations()
        return frozenset(
            device.id for device in self.devices()
            if is_backfill_given_up(registrations, device.id) and is_held(current_status(registrations, device.id))
        )

    async def _async_upload_device(
        self,
        account: PartnerAccount,
        client: PartnerClient,
        device_id: str,
        remote_id: int,
        now: datetime,
        until: datetime | None = None,
    ) -> int:
        """Push one device's owed readings, one request per window, confirming each.

        Both metric types of a window go in one request, so one watermark covers both.

        Args:
            until: For a withdrawn device its ``effectiveTo``: only the slots that start before it are owed, though the
                archive keeps sampling past it. None for no end.

        Returns:
            How many requests were accepted.

        Raises:
            PartnerRejectedError: The partner refused a request; the blocks are logged and the watermark stays before
                them.
            PartnerConnectionError: The partner could not be reached.
        """
        readings = self.device_archive(device_id, now, unconfirmed_only=True)
        if until is not None:
            readings = [reading for reading in readings if reading.start < until]
        per_side = max(1, self._max_request_values // 2)
        accepted = 0
        for window in upload_windows(readings, per_side):
            blocks = encode_device(window, max_values=per_side)
            if not blocks:
                continue
            try:
                written = await client.async_push_measurements(account, remote_id, blocks)
            except PartnerRejectedError:
                _LOGGER.warning("Refused blocks for %s: %s", device_id, _describe(blocks))
                raise
            sent = sum(value is not None for block in blocks for value in block.values)
            if written is not None and written != sent:
                _LOGGER.warning(
                    "10s wrote %s of %s values for %s: %s", written, sent, device_id, _describe(blocks),
                )
            await self._collector.async_confirm(device_id, window[-1].start)
            accepted += 1
        return accepted

    async def _save(self, registrations: Registrations) -> None:
        """Persist the registration mirror."""
        await self._store.save(registrations)

    def _account_and_client(self) -> tuple[PartnerAccount, PartnerClient]:
        """Return the logged-in account and the partner client an action needs.

        Raises:
            ValueError: Not logged in, so there is nothing to ask.
        """
        account = self.account
        if account is None:
            raise ValueError("not logged in to 10s")
        return account, self._client

    async def async_withdraw(self, device_id: str, now: datetime | None = None) -> None:
        """Ask the partner to withdraw a registered device.

        Args:
            device_id: The registered device uuid.
            now: The current UTC instant, the wall clock when omitted; it matters only when the withdrawal meets a
                registration whose answer was lost, which is noted as first seen now.

        Raises:
            ValueError: The device is not one the partner can withdraw.
        """
        account, client = self._account_and_client()
        async with self._lock:
            registrations = self._registrations()
            reported = await client.async_withdraw(account, device_id)
            await self._async_record_reported(registrations, device_id, reported, now or datetime.now(UTC))
            await self._save(registrations)
        await self._request_refresh()

    async def async_re_register(self, device_id: str, now: datetime) -> None:
        """Enter a withdrawn device into the registration cycle again.

        Only a device whose category the partner lists as eligible is sent; with no list cached the partner is asked
        first. Live readings are owed from the request on, never from an earlier registration's watermark.

        Args:
            device_id: The withdrawn device uuid.
            now: The current UTC instant; the history dump ends at its slot.

        Raises:
            ValueError: The device is not linked, is not withdrawn, or its category is not one the partner lets this
                account register.
            PartnerConnectionError: No list is cached and the partner could not be reached to give one.
        """
        account, client = self._account_and_client()
        device = next((d for d in self.devices() if d.id == device_id), None)
        if device is None:
            raise ValueError(f"no metered device with id {device_id!r}")
        async with self._lock:
            await self._async_register(account, client, device, now)
        await self._request_refresh()

    async def _async_register(
        self, account: PartnerAccount, client: PartnerClient, device: MeteredDevice, now: datetime,
    ) -> None:
        """Register a withdrawn device; the caller holds the lock.

        Only the registration happens here; the history goes out in the upload pass.

        Raises:
            ValueError: The device is not withdrawn, or not eligible.
            PartnerConnectionError: No list is cached and the partner could not be reached to give one.
        """
        device_id = device.id
        registrations = self._registrations()
        status = current_status(registrations, device_id)
        if is_held(status):
            raise ValueError(f"device {device_id!r} is {status!r}, expected withdrawn")
        if registrations.eligible is None and record_eligible(
            registrations, await client.async_eligible_categories(account),
        ):
            await self._save(registrations)
        if not is_eligible(registrations, device):
            raise ValueError(
                f"10s does not accept {device.category_label.lower()} devices for registration "
                f"under this login, so {device.name!r} cannot be registered"
            )
        reported = await client.async_register(account, device)
        await self._async_record_reported(registrations, device_id, reported, now)
        await self._save(registrations)

    async def async_set_device_color(self, device_id: str, color: str) -> None:
        """Set the colour a device is drawn in, or clear it back to the palette with "".

        Display only: no partner call, allowed in every status, and kept in the registration store so a recolour does
        not reload the entry.

        Args:
            color: A ``#rrggbb`` colour, or "".

        Raises:
            ValueError: The device is not linked, or ``color`` is neither empty nor a hex colour.
        """
        if not any(device.id == device_id for device in self.devices()):
            raise ValueError(f"no metered device with id {device_id!r}")
        wanted = normalize_color(color)
        if color and not wanted:
            raise ValueError(f"{color!r} is not a hex colour (#rrggbb)")
        async with self._lock:
            registrations = self._registrations()
            set_color(registrations, device_id, wanted)
            await self._save(registrations)
        await self._request_refresh()

    async def async_remove(self, device_id: str) -> None:
        """Drop a withdrawn device from the installation.

        The entry's options are rewritten before the mirror row is dropped: were the rewrite to fail afterwards, the
        device would still be configured with no row, free again, which nobody asked for. A row that briefly outlives
        its device is pruned by the next sync.

        Raises:
            ValueError: The device is not linked, or is not withdrawn yet.
        """
        async with self._lock:
            devices = self.devices()
            kept = [device for device in devices if device.id != device_id]
            if len(kept) == len(devices):
                raise ValueError(f"no metered device with id {device_id!r}")
            registrations = self._registrations()
            # Raises unless the device is withdrawn, before anything is written.
            ensure_removable(registrations, device_id)
            await self._update_options({CONF_METERED_DEVICES: as_config_list(kept)})
            forget(registrations, device_id)
            await self._save(registrations)

    async def async_logout(self) -> None:
        """Log out of the partner and wipe every trace of it from the installation, locally only.

        The account, every linked device, the registration mirror and the slot archive are dropped. 10s has no endpoint
        to revoke an app registration, so it keeps this installation's registration; the next login sends the same
        installation id as ``appInstanceId``. Refused while the partner holds a device, as removal is. The entry is
        rewritten before the mirror is cleared, so a failed entry write leaves the installation as it was instead of
        configured but unremembered.

        Raises:
            ValueError: The partner still holds a linked device.
        """
        async with self._lock:
            held = self.held_device_ids()
            if held:
                raise ValueError(
                    f"{len(held)} device(s) the partner still holds; withdraw them before logging out"
                )
            await self._update_options({
                CONF_TENS_ACCOUNT: "",
                CONF_METERED_DEVICES: [],
            })
            await self._save(Registrations())
            self._upload_expected_since = {}
            await self._collector.async_reset()
        await self._request_refresh()


def is_uploaded(state: RegistrationState) -> bool:
    """Return whether a device's readings go to the partner at a contact.

    True while the partner takes data for the device, and once it reads ``withdrawn`` with ``effectiveTo`` known: the
    slots owed up to that edge go at the next contact, so a withdrawal leaves no gap.
    """
    return state.status in UPLOAD_STATUSES or (
        state.status == TENS_STATUS_WITHDRAWN and state.effective_to is not None
    )


def upload_end(state: RegistrationState) -> datetime | None:
    """Return the instant a device's owed slots end, or None while the partner still takes data for it."""
    return None if state.status in UPLOAD_STATUSES else state.effective_to


def slot_accounting(state: RegistrationState | None, confirmed_through: datetime | None, start: datetime) -> str:
    """Return how the partner accounts the slot starting at ``start``.

    The partner accounts its effective window ``[effectiveFrom, effectiveTo)``. Sent is not accounted, since the live
    upload begins at the registration request, up to two days before ``effectiveFrom``, so the window is checked first
    and the watermark second.

    Args:
        state: The device's mirror row, or None for a device never recorded.
        confirmed_through: The device's watermark, or None while nothing has set one.
        start: The slot's opening mark.

    Returns:
        ``SLOT_OUTSIDE`` for a device the partner is not sent, a slot before a known ``effectiveFrom`` or from
            ``effectiveTo`` on; ``SLOT_ACCOUNTED`` for a slot inside the window that the partner confirmed;
            ``SLOT_OWED`` for one inside the window still to be confirmed. An edge the mirror does not know yet does not
            cut.
    """
    if state is None or not is_uploaded(state):
        return SLOT_OUTSIDE
    if state.effective_from is not None and start < state.effective_from:
        return SLOT_OUTSIDE
    if state.effective_to is not None and start >= state.effective_to:
        return SLOT_OUTSIDE
    if confirmed_through is not None and start < confirmed_through:
        return SLOT_ACCOUNTED
    return SLOT_OWED


def upload_windows(readings: Sequence[DeviceSlot], max_slots: int) -> list[list[DeviceSlot]]:
    """Cut a device's owed readings into windows of at most ``max_slots`` slots each.

    Consecutive windows share their boundary reading, so no slot is lost or sent twice. Cutting by time bounds a window
    across a gap; only a shrunken ``max_slots`` is exceeded by one, and the encoder then splits it into blocks.

    Args:
        readings: The readings, earliest first, starting at the watermark reading.
        max_slots: The most 15-minute slots one window may span.

    Returns:
        The windows, each of at least two readings; empty when fewer than two readings exist.
    """
    windows: list[list[DeviceSlot]] = []
    first = 0
    while first < len(readings) - 1:
        last = first + 1
        limit = readings[first].start + SLOT_GRANULARITY * max_slots
        while last + 1 < len(readings) and readings[last + 1].start <= limit:
            last += 1
        windows.append(list(readings[first:last + 1]))
        first = last
    return windows


def dump_requests(dump: RegistrationDump, max_request_values: int) -> list[list[MeasurementBlock]]:
    """Encode a history dump as upload requests of at most ``max_request_values`` values.

    Both resolutions go out, 5-minute and hourly, each as ``CONSUMPTION`` and, where the device measures one,
    ``PRODUCTION``. The windows overlap on purpose.

    Returns:
        The requests, each a list of blocks; empty for a dump with no readings.
    """
    per_block = max(1, max_request_values // 2)
    blocks: list[MeasurementBlock] = []
    for series in (dump.fine, dump.coarse):
        blocks.extend(_dump_series_blocks(series, per_block))
    requests: list[list[MeasurementBlock]] = []
    size = 0
    for block in blocks:
        if not requests or size + len(block.values) > max_request_values:
            requests.append([])
            size = 0
        requests[-1].append(block)
        size += len(block.values)
    return requests


def _dump_series_blocks(series: DumpSeries, max_values: int) -> list[MeasurementBlock]:
    """Return the blocks of one dump series, consumption then production."""
    return [
        *series_blocks(METRIC_CONSUMPTION, series.consumed, series.step, max_values=max_values),
        *series_blocks(METRIC_PRODUCTION, series.returned, series.step, max_values=max_values),
    ]


def _describe(blocks: Sequence[MeasurementBlock]) -> str:
    """Return a one-line summary of blocks for the log: window, metric, count and total."""
    return "; ".join(
        f"{block.metric_type} {block.start.isoformat()}..{block.end.isoformat()} "
        f"({len(block.values)} values, total {block.total})"
        for block in blocks
    )
