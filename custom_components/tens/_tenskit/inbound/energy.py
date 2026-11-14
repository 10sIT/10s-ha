"""Read energy counters in kWh, strictly.

A counter comes from any integration, so a missing or unrecognised unit is never read as kWh (joules would be off by
3.6 million).
"""
from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from homeassistant.core import State

# Every unit Home Assistant's ``EnergyConverter`` accepts, as kWh per unit.
_STRICT_ENERGY_TO_KWH: dict[str, float] = {
    "J": 1.0 / 3_600_000.0,
    "kJ": 1.0 / 3_600.0,
    "MJ": 1.0 / 3.6,
    "GJ": 1000.0 / 3.6,
    "mWh": 1.0 / 1_000_000.0,
    "Wh": 1.0 / 1000.0,
    "kWh": 1.0,
    "MWh": 1000.0,
    "GWh": 1_000_000.0,
    "TWh": 1_000_000_000.0,
    "cal": 4.184 / 3_600_000.0,
    "kcal": 4.184 / 3_600.0,
    "Mcal": 4.184 / 3.6,
    "Gcal": 4184.0 / 3.6,
}

_UNUSABLE_STATES = ("unavailable", "unknown", "")


def _available_state(hass: Any, entity_id: str) -> State | None:
    """Return the entity's ``State`` when it carries a usable value, else None."""
    if not entity_id:
        return None
    state = hass.states.get(entity_id)
    if state is None or state.state in _UNUSABLE_STATES:
        return None
    return state


def energy_unit_to_kwh(unit: str | None) -> float | None:
    """Return the kWh-per-unit factor of an energy unit, or None when it is not one.

    Args:
        unit: The ``unit_of_measurement`` attribute, e.g. "Wh".
    """
    return _STRICT_ENERGY_TO_KWH.get((unit or "").strip())


def read_energy_kwh(hass: Any, entity_id: str) -> float | None:
    """Return an energy counter's reading in kWh, or None when it has no usable one.

    None covers an unmapped, unavailable or non-numeric state and a unit that is not energy.
    """
    state = _available_state(hass, entity_id)
    if state is None:
        return None
    factor = energy_unit_to_kwh(state.attributes.get("unit_of_measurement"))
    if factor is None:
        return None
    try:
        return float(state.state) * factor
    except (TypeError, ValueError):
        return None