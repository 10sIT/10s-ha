"""Find the energy meters a Home Assistant device offers, rank them and pre-pick them for a device kind.

A sensor qualifies as the Energy dashboard would accept it: ``sensor`` domain, ``device_class: energy`` with
``state_class`` ``total`` or ``total_increasing``, enabled, not hidden, not a ``utility_meter`` helper and not one of
the host's own sensors. Power sensors never qualify. Candidates are ranked by name hints (:func:`meter_rank`), never
filtered. The selection logic is pure; ``device_meters`` and :func:`device_name` are the Home Assistant edge.
"""
from __future__ import annotations

import re
from collections.abc import Callable, Collection, Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er

from ..contract import MeterSlot

METER_STATE_CLASSES = frozenset({"total", "total_increasing"})
# A ``utility_meter`` helper is a resetting copy of a meter already listed; a host's own sensors would make the
# pipeline read its own output, so a host adds its domain through ``excluded_platforms``.
EXCLUDED_PLATFORMS = frozenset({"utility_meter"})
# Name fragments of a counter for energy leaving the site (Shelly "returned energy", Solis "fed into grid", ZHA
# "summation received"). Matched against the id and names only: a unique id can hold a MAC whose hex spells "fed".
_EXPORT_HINTS = ("export", "return", "feed", "fed", "inject", "produc", "generat", "yield", "discharg", "received")

TIER_WHOLE = 0
TIER_NEUTRAL = 1
TIER_SPLIT = 2
TIER_TIME_SCOPED = 3
TIER_NOT_ELECTRIC = 4
TIER_EXPORT = 5

# Matched against the "_"-joined tokens with a leading and trailing "_": "_word_" is a whole token, "_word" a prefix.
_NOT_ELECTRIC_HINTS = ("_thermal", "_environment", "_earned", "_fossil", "_gas_", "_heat_generated", "_heat_meter")
_TIME_HINTS = (
    "_today_", "_daily_", "_day_", "_yesterday_", "_week_", "_weekly_", "_month_", "_monthly_", "_year_",
    "_yearly_", "_session_", "_hour_", "_hourly_",
)
# Split counters add up to the device's whole consumption only within one axis: L1 + L2 + L3 is the total, L1 + T1
# double-counts.
_SPLIT_AXES: Mapping[str, tuple[str, ...]] = {
    "circuit": ("_heating", "_cooling", "_dhw_", "_hot_water_"),
    "tariff": ("_tariff", "_t1_", "_t2_", "_t3_", "_t4_"),
    "phase": ("_phase", "_l1_", "_l2_", "_l3_"),
    "source": ("_green_", "_grid_energy_"),
}
_WHOLE_HINTS = ("_total", "_lifetime", "_consum", "_import", "_electric")

Attributes = Callable[[str], Mapping[str, Any]]


@dataclass(frozen=True)
class MeterRank:
    """Where a counter sorts among a device's meters.

    Attributes:
        tier: One of the ``TIER_*`` constants; lower is likelier the device's consumption.
        axis: The split axis (``circuit``, ``tariff``, ``phase``, ``source``) of a ``TIER_SPLIT`` counter, "" otherwise.
    """

    tier: int
    axis: str = ""


@dataclass(frozen=True)
class MeterScan:
    """What a Home Assistant device offers as meters.

    Attributes:
        candidates: Qualifying, unlinked counters, best rank first.
        ranks: Each candidate's :class:`MeterRank`.
        refusal: "" when there are candidates, otherwise the error code saying why not (``device_meters_linked``,
            ``device_meter_not_counter``, ``device_meter_disabled``, ``device_power_only``, ``device_no_meter``).
        refusal_sensor: The sensor the refusal is about, "" if none.
    """

    candidates: tuple[str, ...] = ()
    ranks: Mapping[str, MeterRank] = field(default_factory=dict)
    refusal: str = ""
    refusal_sensor: str = ""


def _text(value: Any) -> str:
    """Return a registry value as a plain string, "" for None."""
    return str(value) if value is not None else ""


def _classes(entry: Any, attributes: Mapping[str, Any]) -> tuple[str, str]:
    """Return an entry's ``(device_class, state_class)``, registry first, state attributes second."""
    device_class = _text(entry.device_class or entry.original_device_class or attributes.get("device_class"))
    state_class = _text((entry.capabilities or {}).get("state_class") or attributes.get("state_class"))
    return device_class, state_class


def _offerable(entry: Any, excluded_platforms: frozenset[str] = EXCLUDED_PLATFORMS) -> bool:
    """Return True for a sensor that is not a helper copy or one of the host's own."""
    return entry.domain == "sensor" and entry.platform not in excluded_platforms


def _enabled(entry: Any) -> bool:
    """Return True for an entry neither disabled nor hidden."""
    return entry.disabled_by is None and entry.hidden_by is None


def is_meter(
    entry: Any,
    attributes: Mapping[str, Any],
    excluded_platforms: frozenset[str] = EXCLUDED_PLATFORMS,
) -> bool:
    """Return True when a registry entry is an enabled, visible energy counter that is not an excluded platform's.

    Args:
        entry: Entity-registry entry.
        attributes: The entity's current state attributes, empty when it has no state yet; a fallback for classes the
            registry does not carry.
        excluded_platforms: Platforms whose sensors are never offered.
    """
    if not _offerable(entry, excluded_platforms) or not _enabled(entry):
        return False
    device_class, state_class = _classes(entry, attributes)
    return device_class == "energy" and state_class in METER_STATE_CLASSES


def looks_like_export(entry: Any) -> bool:
    """Return True when an entry's id or name suggests energy leaving the site."""
    text = f"{entry.entity_id} {entry.original_name or ''} {entry.name or ''}".casefold()
    return any(hint in text for hint in _EXPORT_HINTS)


def _tokens(entry: Any) -> str:
    """Return the entry's id, names and stable keys as "_"-joined lower-case tokens."""
    # ``translation_key`` and ``unique_id`` stay English on an install whose names are translated.
    parts = (
        entry.entity_id, entry.original_name, entry.name,
        getattr(entry, "translation_key", None), getattr(entry, "unique_id", None),
    )
    words = re.split(r"[^0-9a-z]+", " ".join(_text(part) for part in parts).casefold())
    return "_" + "_".join(word for word in words if word) + "_"


def meter_rank(entry: Any) -> MeterRank:
    """Return where a counter sorts among its device's meters.

    The first matching tier wins, worst first, so "consumed electrical energy heating" is a heating split though it says
    *consumed*.
    """
    if looks_like_export(entry):
        return MeterRank(TIER_EXPORT)
    tokens = _tokens(entry)
    if any(hint in tokens for hint in _NOT_ELECTRIC_HINTS):
        return MeterRank(TIER_NOT_ELECTRIC)
    if any(hint in tokens for hint in _TIME_HINTS):
        return MeterRank(TIER_TIME_SCOPED)
    for axis, hints in _SPLIT_AXES.items():
        if any(hint in tokens for hint in hints):
            return MeterRank(TIER_SPLIT, axis)
    if any(hint in tokens for hint in _WHOLE_HINTS):
        return MeterRank(TIER_WHOLE)
    return MeterRank(TIER_NEUTRAL)


def _ranked(
    entries: Iterable[Any],
    attributes: Attributes,
    exclude: Collection[str],
    excluded_platforms: frozenset[str] = EXCLUDED_PLATFORMS,
) -> dict[str, MeterRank]:
    """Return the qualifying, unlinked meters among ``entries`` with their ranks, best first."""
    found: dict[str, MeterRank] = {}
    for entry in entries:
        if entry.entity_id in found or entry.entity_id in exclude:
            continue
        if is_meter(entry, attributes(entry.entity_id), excluded_platforms):
            found[entry.entity_id] = meter_rank(entry)
    order = sorted(found, key=lambda entity_id: (found[entity_id].tier, entity_id))
    return {entity_id: found[entity_id] for entity_id in order}


def _refusal(
    entries: list[Any],
    attributes: Attributes,
    exclude: Collection[str],
    excluded_platforms: frozenset[str] = EXCLUDED_PLATFORMS,
) -> tuple[str, str]:
    """Return why a device with no candidate meter has none, and the sensor concerned.

    Args:
        exclude: Meters already linked to another device.

    Returns:
        ``(error_code, entity_id)``, the most actionable reason first.
    """
    sensors = [e for e in entries if _offerable(e, excluded_platforms)]
    classes = {e.entity_id: _classes(e, attributes(e.entity_id)) for e in sensors}
    energy = [e for e in sensors if classes[e.entity_id][0] == "energy"]
    counters = [e for e in energy if classes[e.entity_id][1] in METER_STATE_CLASSES]
    linked = [e for e in counters if _enabled(e) and e.entity_id in exclude]
    if linked:
        return "device_meters_linked", linked[0].entity_id
    not_counting = [e for e in energy if _enabled(e) and e not in counters]
    if not_counting:
        return "device_meter_not_counter", not_counting[0].entity_id
    if counters:
        return "device_meter_disabled", counters[0].entity_id
    power = [e for e in sensors if _enabled(e) and classes[e.entity_id][0] == "power"]
    if power:
        return "device_power_only", power[0].entity_id
    return "device_no_meter", ""


def scan_meters(
    entries: Iterable[Any],
    attributes: Attributes,
    exclude: Collection[str],
    excluded_platforms: frozenset[str] = EXCLUDED_PLATFORMS,
) -> MeterScan:
    """Return a device's candidate meters, or the reason it has none.

    Args:
        entries: The device's entity-registry entries.
        attributes: Returns an entity's state attributes by entity id.
        exclude: Meters already linked to another device.
        excluded_platforms: Platforms whose sensors are never offered.
    """
    entries = list(entries)
    ranks = _ranked(entries, attributes, exclude, excluded_platforms)
    if ranks:
        return MeterScan(tuple(ranks), ranks)
    refusal, sensor = _refusal(entries, attributes, exclude, excluded_platforms)
    return MeterScan(refusal=refusal, refusal_sensor=sensor)


def pre_pick(scan: MeterScan, slot: MeterSlot) -> list[str]:
    """Return the counters to pre-select for one role of a device kind.

    Only a consumed-side role is guessed. When the best counter is one of several split along one axis and the role sums
    counters, the whole axis is picked. A device whose every counter looks like export gets nothing.
    """
    # A returned-side role opens empty, required or not: a wrong guess would silently send the wrong counter as export.
    if slot.output != "consumed" or not scan.candidates:
        return []
    top = scan.candidates[0]
    rank = scan.ranks[top]
    if rank.tier == TIER_EXPORT:
        return []
    if rank.tier == TIER_SPLIT:
        return [c for c in scan.candidates if scan.ranks[c] == rank]
    return [top]


def _attributes(hass: HomeAssistant) -> Attributes:
    """Return a lookup of an entity's current state attributes."""
    def lookup(entity_id: str) -> Mapping[str, Any]:
        state = hass.states.get(entity_id)
        return state.attributes if state is not None else {}
    return lookup


def device_meters(
    hass: HomeAssistant,
    device_id: str,
    exclude: Collection[str],
    excluded_platforms: frozenset[str] = EXCLUDED_PLATFORMS,
) -> MeterScan:
    """Return a Home Assistant device's candidate meters, or why it has none.

    Args:
        device_id: Device-registry id chosen on the picker.
        exclude: Entity ids already linked to another metered device.
        excluded_platforms: Platforms whose sensors are never offered.
    """
    return scan_meters(
        er.async_entries_for_device(er.async_get(hass), device_id),
        _attributes(hass),
        exclude,
        excluded_platforms,
    )


def device_name(hass: HomeAssistant, device_id: str) -> str:
    """Return a device's display name, the user's rename first, or ""."""
    device = dr.async_get(hass).async_get(device_id)
    if device is None:
        return ""
    return device.name_by_user or device.name or ""

