"""Sensor entities for 10s."""
from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from homeassistant.components.sensor import SensorDeviceClass, SensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import UnitOfEnergy
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from ._tenskit.contract import DeviceSlot, MeteredDevice
from ._tenskit.orchestration import PartnerSnapshot
from ._tenskit.outbound.partners import SLOT_GRANULARITY, SlotEnergy, slot_energies
from .const import DOMAIN
from .coordinator import TensCoordinator


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Create the dashboard sensor and one 15-minute energy sensor per linked device.

    The sensors deliberately register no Home Assistant device: a device in the registry makes the frontend
    end the setup flow with its "Name and assign" (area) page.
    """
    coordinator: TensCoordinator = hass.data[DOMAIN]
    devices = coordinator.partner.devices()
    _remove_stale_entities(
        hass,
        entry,
        {_dashboard_unique_id(entry), *(_energy_unique_id(entry, device.id) for device in devices)},
    )
    async_add_entities([
        DashboardSensor(coordinator, entry),
        *(DeviceEnergySensor(coordinator, entry, device) for device in devices),
    ])


def _dashboard_unique_id(entry: ConfigEntry) -> str:
    """Return the unique_id of the entry's dashboard sensor."""
    return f"{entry.entry_id}_dashboard"


def _energy_unique_id(entry: ConfigEntry, device_id: str) -> str:
    """Return the unique_id of one metered device's energy sensor, scoped by entry (the bare device id is 10s's)."""
    return f"{entry.entry_id}_{device_id}_energy_15min"


def _remove_stale_entities(hass: HomeAssistant, entry: ConfigEntry, live: set[str]) -> None:
    """Drop this entry's registry entities whose unique_id is not in ``live``, leaving no orphaned sensor."""
    registry = er.async_get(hass)
    for registered in er.async_entries_for_config_entry(registry, entry.entry_id):
        if registered.unique_id not in live:
            registry.async_remove(registered.entity_id)


def _partner_attributes(snapshot: PartnerSnapshot) -> dict[str, Any]:
    """Return the panel's keys naming the 10s account and the kinds it may register.

    ``partner_eligible`` is None until 10s has been asked. Never carries the client secret.
    """
    account = snapshot.account
    eligible = snapshot.eligible
    return {
        "partner_linked": account is not None,
        "partner_eligible": None if eligible is None else list(eligible.values()),
        "partner_account": {"label": account.label} if account is not None else None,
    }


def _latest_slot(archive: list[DeviceSlot]) -> SlotEnergy | None:
    """Return the latest slot of a device's archive, computed as the upload does, or None before any slot closed."""
    energies = slot_energies(archive)
    return energies[-1] if energies else None


class DashboardSensor(CoordinatorEntity, SensorEntity):
    """The single panel source of the account and every linked device."""

    _attr_name = "10s Dashboard"
    _attr_icon = "mdi:meter-electric"
    # The panel reads live state, never history; recording these would write a large new state row each refresh.
    _unrecorded_attributes = frozenset({"metered_devices", "generated_at"})

    def __init__(self, coordinator: TensCoordinator, entry: ConfigEntry) -> None:
        """Initialise the dashboard sensor, its unique_id scoped by the entry."""
        super().__init__(coordinator)
        self._attr_unique_id = _dashboard_unique_id(entry)

    @property
    def available(self) -> bool:
        """Return True: ``CoordinatorEntity`` would follow the last refresh and blank the panel on a refused login."""
        return True

    @property
    def native_value(self) -> str:
        """Return "auth_failed" while 10s refuses the login, else "ok"."""
        return "auth_failed" if self.coordinator.auth_failed else "ok"

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return the panel's payload: which 10s, the account, whether 10s refuses the login, and the linked devices."""
        now = datetime.now(UTC)
        partner = self.coordinator.partner.snapshot(now)
        return {
            # Never the same twice, so every push is a state change and the panel redraws its chart.
            "generated_at": now.isoformat(),
            "partner_environment": self.coordinator.environment.name,
            **_partner_attributes(partner),
            "auth_failed": self.coordinator.auth_failed,
            "metered_devices": list(partner.devices),
        }


class DeviceEnergySensor(CoordinatorEntity, SensorEntity):
    """The energy one metered device used in its latest complete 15-minute interval.

    The value is the latest slot of the kit's ``slot_energies``, as the upload computes it. A slot after a
    missed mark or across a counter reset has no honest value, so the state is unknown rather than a guess.
    """

    # No state class: HA accepts only ``total``/``total_increasing`` for device class energy
    # (``DEVICE_CLASS_STATE_CLASSES`` in ``homeassistant/components/sensor/const.py``) and warns on ``measurement``.
    _attr_device_class = SensorDeviceClass.ENERGY
    _attr_native_unit_of_measurement = UnitOfEnergy.KILO_WATT_HOUR
    _attr_suggested_display_precision = 3

    def __init__(self, coordinator: TensCoordinator, entry: ConfigEntry, device: MeteredDevice) -> None:
        """Initialise the sensor for one device, naming it after the device (no Home Assistant device prefixes it)."""
        super().__init__(coordinator)
        self._device_id = device.id
        self._attr_unique_id = _energy_unique_id(entry, device.id)
        self._attr_name = f"{device.name} Energy (15 min)"

    @property
    def available(self) -> bool:
        """Return True: the value comes from the local archive, which keeps filling while 10s refuses the login."""
        return True

    @property
    def native_value(self) -> float | None:
        """Return the kWh consumed over the latest interval, or None when unknown."""
        slot = _latest_slot(self.coordinator.partner.device_archive(self._device_id))
        if slot is None or slot.consumed_kwh is None:
            return None
        return float(slot.consumed_kwh)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return the registration state, the interval's bounds and the latest counters.

        ``last_confirmed_at`` is the mark the live upload is owed from: the last mark 10s confirmed, or the one
        a new registration set before its first accepted upload.
        """
        partner = self.coordinator.partner
        row = partner.device(self._device_id) or {}
        archive = partner.device_archive(self._device_id)
        confirmed = partner.last_confirmed(self._device_id)
        attrs: dict[str, Any] = {
            "status": row.get("status"),
            "last_reading_at": archive[-1].start.isoformat() if archive else None,
            "last_reading_consumed_kwh": archive[-1].consumed_kwh if archive else None,
            "last_reading_returned_kwh": archive[-1].returned_kwh if archive else None,
            "last_confirmed_at": confirmed.isoformat() if confirmed is not None else None,
        }
        slot = _latest_slot(archive)
        if slot is not None and slot.consumed_kwh is not None:
            attrs["interval_start"] = slot.start.isoformat()
            attrs["interval_end"] = (slot.start + SLOT_GRANULARITY).isoformat()
            if archive[-2].returned_kwh is not None and archive[-1].returned_kwh is not None:
                attrs["returned_kwh"] = None if slot.returned_kwh is None else float(slot.returned_kwh)
        return attrs
