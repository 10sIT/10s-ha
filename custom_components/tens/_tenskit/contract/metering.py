"""Metered devices: the loads whose energy is collected for 10s, and the one definition of a stored row.

Each device kind's :class:`MeterProfile` translates its counters into one :class:`DeviceEnergy`, so nothing downstream
learns how a heat pump or a charger is metered. Devices belong to the installation, not to a partner.
"""
from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, replace
from typing import Any, Literal

from .const import CONF_METERED_DEVICES, TENS_DEVICE_CATEGORIES

# The one source of category labels, so the setup flow's picker and the panel word a category the same way.
CATEGORY_LABELS: Mapping[str, str] = {
    "HEAT_PUMP": "Heat pump",
    "EV_CHARGER": "EV charger",
    "BATTERY": "Battery",
    "BOILER": "Boiler",
}


ROLE_CONSUMED = "consumed"
ROLE_RETURNED = "returned"

Output = Literal["consumed", "returned"]


@dataclass(frozen=True)
class MeterBinding:
    """One energy counter a device reads: a role of the kind's :class:`MeterProfile` and the cumulative sensor."""

    role: str
    entity_id: str


@dataclass(frozen=True)
class MeterSlot:
    """One role a device kind reads its counters into.

    Several counters may be bound to a role; they are summed (a heat pump's heating and hot-water counters).

    Attributes:
        role: The role's id, also the form field ``meter_<role>``.
        output: The :class:`DeviceEnergy` field the role's counters add to.
        required: Whether the kind needs at least one counter here; a stored row without one is not a device.
    """

    role: str
    output: Output
    required: bool


@dataclass(frozen=True)
class DeviceEnergy:
    """A device's counters translated into the one shape every kind shares.

    Attributes:
        consumed_kwh: Energy into the device; None when a required counter has no reading.
        returned_kwh: Energy out of the device (V2G or battery discharge); None when the kind has none or a counter
            has no reading.
    """

    consumed_kwh: float | None
    returned_kwh: float | None


@dataclass(frozen=True)
class MeterProfile:
    """How one kind of device is metered: its roles and how they translate.

    Attributes:
        slots: The kind's roles in form order; the first feeds ``consumed``.
    """

    slots: tuple[MeterSlot, ...]

    def slot(self, role: str) -> MeterSlot | None:
        """Return the slot for ``role``, or None when the kind has no such role."""
        return next((slot for slot in self.slots if slot.role == role), None)

    def translate(self, meters: Iterable[MeterBinding], kwh: Mapping[str, float | None]) -> DeviceEnergy:
        """Translate bound counters' readings into the common :class:`DeviceEnergy`.

        A role's counters are summed only when every one has a reading: a sum missing one would under-report silently.

        Args:
            meters: The device's bindings; roles outside this profile are ignored.
            kwh: Each counter's reading in kWh, None when unusable.
        """
        bound = [(m.entity_id, slot) for m in meters if (slot := self.slot(m.role)) is not None]

        def total(output: Output) -> float | None:
            """Return the sum of one output's counters, or None when any is unread."""
            readings = [kwh.get(entity) for entity, slot in bound if slot.output == output]
            if not readings or any(reading is None for reading in readings):
                return None
            return sum(readings)  # type: ignore[arg-type]

        return DeviceEnergy(total("consumed"), total("returned"))


_CONSUMER = MeterProfile((MeterSlot(ROLE_CONSUMED, "consumed", required=True),))
_BIDIRECTIONAL = MeterProfile((
    MeterSlot(ROLE_CONSUMED, "consumed", required=True),
    MeterSlot(ROLE_RETURNED, "returned", required=False),
))
_STORAGE = MeterProfile((
    MeterSlot(ROLE_CONSUMED, "consumed", required=True),
    MeterSlot(ROLE_RETURNED, "returned", required=True),
))

# A charger may discharge a car back into the house (V2G), so its discharge is optional; a battery always has a
# discharge side, even one that reads zero, so it needs both.
METER_PROFILES: Mapping[str, MeterProfile] = {
    "HEAT_PUMP": _CONSUMER,
    "BOILER": _CONSUMER,
    "EV_CHARGER": _BIDIRECTIONAL,
    "BATTERY": _STORAGE,
}


def profile_for(category: str) -> MeterProfile:
    """Return a device kind's meter profile.

    Raises:
        KeyError: ``category`` is not one of ``TENS_DEVICE_CATEGORIES``.
    """
    return METER_PROFILES[category]


def category_label(category: str) -> str:
    """Return a category's display label.

    Raises:
        KeyError: ``category`` is not a known kind.
    """
    return CATEGORY_LABELS[category]


def _text(value: Any) -> str:
    """Return a stored value as a plain string, "" for None."""
    return str(value) if value is not None else ""


def normalize_color(value: Any) -> str:
    """Return a stored colour as lowercase ``#rrggbb``, or "" when it is not one.

    Only ``#rrggbb`` is accepted because it is the only form written (the panel's ``<input type="color">`` emits it);
    anything else is dropped, and the deterministic palette still gives the device a colour.
    """
    text = _text(value).strip().lower()
    return text if re.fullmatch(r"#[0-9a-f]{6}", text) else ""


def _stored_meters(raw: Mapping[str, Any], profile: MeterProfile) -> tuple[MeterBinding, ...]:
    """Return a stored row's meter bindings in stored order, dropping those for a role the kind lacks."""
    stored = raw.get("meters")
    pairs = [
        (_text(item.get("role")), _text(item.get("entity_id")))
        for item in stored if isinstance(item, Mapping)
    ] if isinstance(stored, list) else []
    return tuple(
        MeterBinding(role, entity_id) for role, entity_id in pairs if entity_id and profile.slot(role) is not None
    )


@dataclass(frozen=True)
class MeteredDevice:
    """One energy-metered load linked to this installation.

    The chart colour is not part of the stored row; the partner runtime keeps it in its own store.

    Attributes:
        id: uuid4 hex, stable across renames, edits and re-registrations.
        name: The installer's name for it, unique within the installation.
        category: One of ``TENS_DEVICE_CATEGORIES``.
        meters: The counters the device reads, in form order.
        ha_device_id: The Home Assistant device the meters were picked from, "" when not recorded.
    """

    id: str
    name: str
    category: str
    meters: tuple[MeterBinding, ...] = ()
    ha_device_id: str = ""

    @property
    def category_label(self) -> str:
        """Return this device's display label."""
        return category_label(self.category)

    @property
    def profile(self) -> MeterProfile:
        """Return how this device's kind reads its meters."""
        return profile_for(self.category)

    @property
    def meter_entities(self) -> tuple[str, ...]:
        """Return every entity id this device reads, in binding order."""
        return tuple(binding.entity_id for binding in self.meters)

    def entities_for(self, role: str) -> tuple[str, ...]:
        """Return the entity ids bound to one role."""
        return tuple(binding.entity_id for binding in self.meters if binding.role == role)

    @classmethod
    def from_config(cls, raw: Any) -> MeteredDevice | None:
        """Rebuild a device from one stored row, or None when it is unusable.

        A row with no ``id``, an unknown kind, or no counter for a role its kind requires is dropped rather than
        repaired; the prunes then forget whatever else was kept for it.
        """
        if not isinstance(raw, Mapping) or not raw.get("id"):
            return None
        category = _text(raw.get("category"))
        if category not in TENS_DEVICE_CATEGORIES:
            return None
        profile = profile_for(category)
        meters = _stored_meters(raw, profile)
        bound_roles = {binding.role for binding in meters}
        if any(slot.required and slot.role not in bound_roles for slot in profile.slots):
            return None
        return cls(
            id=_text(raw["id"]),
            name=_text(raw.get("name")),
            category=category,
            meters=meters,
            ha_device_id=_text(raw.get("ha_device_id")),
        )

    def as_config(self) -> dict[str, Any]:
        """Return the device as the JSON-safe row stored in the config entry."""
        return {
            "id": self.id,
            "name": self.name,
            "category": self.category,
            "meters": [{"role": b.role, "entity_id": b.entity_id} for b in self.meters],
            "ha_device_id": self.ha_device_id,
        }

    def with_fields(self, **fields: Any) -> MeteredDevice:
        """Return a copy with ``fields`` replaced, keeping its id."""
        return replace(self, **fields)


def metered_devices(config: Mapping[str, Any]) -> tuple[MeteredDevice, ...]:
    """Return the devices stored under ``CONF_METERED_DEVICES``, dropping unusable rows.

    Args:
        config: Merged config-entry data and options, or a setup flow's accumulator.
    """
    stored = config.get(CONF_METERED_DEVICES)
    if not isinstance(stored, list):
        return ()
    return tuple(device for raw in stored if (device := MeteredDevice.from_config(raw)) is not None)


def as_config_list(devices: tuple[MeteredDevice, ...] | list[MeteredDevice]) -> list[dict[str, Any]]:
    """Return devices as the stored list written back to the config entry."""
    return [device.as_config() for device in devices]
