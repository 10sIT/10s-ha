"""Metered-device forms: pick the device and its kind, then check its meters.

The kind decides the meter fields, one per role of its :class:`MeterProfile` (``meter_<role>``), so every kind uses
the same form code. These are only the forms; the model is in ``contract`` and the steps in ``device_steps``.
"""
from __future__ import annotations

from collections.abc import Collection
from typing import Any

import voluptuous as vol
from homeassistant.helpers.selector import (
    DeviceSelector,
    DeviceSelectorConfig,
    EntitySelector,
    EntitySelectorConfig,
    SelectSelector,
    SelectSelectorConfig,
    SelectSelectorMode,
)

from ..contract import CATEGORY_LABELS, TENS_DEVICE_CATEGORIES, MeterBinding, MeteredDevice, MeterProfile
from .fields import list_selector, missing_required, suggest

DEVICE_NAME = "device_name"
DEVICE_CATEGORY = "device_category"
DEVICE_SOURCE = "ha_device"
REMOVE_DEVICE_IDS = "remove_device_ids"
EDIT_DEVICE_ID = "edit_device_id"

# The field needs a default so "✕ discard" passes.
DEFAULT_CATEGORY = TENS_DEVICE_CATEGORIES[0]

# The selector can only filter by device class; the strict meter test runs in the step once a device is picked. Power
# sensors are listed too, so a device with only power can be picked and told how to get an energy counter.
_METERED_DEVICE = DeviceSelector(DeviceSelectorConfig(entity=[
    {"domain": "sensor", "device_class": "energy"},
    {"domain": "sensor", "device_class": "power"},
]))


def meter_key(role: str) -> str:
    """Return the form field holding a meter role's counters."""
    return f"meter_{role}"


def selected_meters(user_input: dict, role: str) -> list[str]:
    """Return the counters submitted for one role as a list whatever the widget, blanks dropped."""
    value = user_input.get(meter_key(role))
    values = value if isinstance(value, list) else [value]
    return [entity for entity in values if entity]


def meter_bindings(user_input: dict, profile: MeterProfile) -> tuple[MeterBinding, ...]:
    """Return a submitted form's counters bound to the kind's roles, in form order."""
    return tuple(
        MeterBinding(slot.role, entity) for slot in profile.slots for entity in selected_meters(user_input, slot.role)
    )


def _category_selector(default: str) -> dict:
    """Return the "What is it?" field, pre-selected with ``default``."""
    return {
        vol.Required(DEVICE_CATEGORY, default=default): SelectSelector(
            SelectSelectorConfig(
                options=[{"value": c, "label": CATEGORY_LABELS[c]} for c in TENS_DEVICE_CATEGORIES],
                mode=SelectSelectorMode.DROPDOWN,
            )
        ),
    }


def _meter_fields(d: dict, profile: MeterProfile, meters: Collection[str]) -> dict:
    """Return one entity picker per role of ``profile``, limited to ``meters``.

    Every role takes several counters, summed.

    Args:
        d: Values to pre-fill, ``meter_<role>``: an entity id or a list.
        meters: The device's qualifying counters.
    """
    fields: dict = {}
    for slot in profile.slots:
        key = meter_key(slot.role)
        selector = EntitySelector(
            EntitySelectorConfig(domain="sensor", include_entities=list(meters), multiple=True),
        )
        fields[vol.Optional(key, description=suggest(d.get(key)))] = selector
    return fields


def device_source_schema(d: dict) -> vol.Schema:
    """Build a device's first form: the Home Assistant device that measures it, and its kind.

    The kind comes first because it decides which meter fields the details form shows.

    Args:
        d: The previous submission or an edited device's :func:`device_form_values`.
    """
    # Optional so the frontend lets "✕ discard" through; the step checks it.
    fields: dict = {vol.Optional(DEVICE_SOURCE, description=suggest(d.get(DEVICE_SOURCE))): _METERED_DEVICE}
    fields |= _category_selector(d.get(DEVICE_CATEGORY) or DEFAULT_CATEGORY)
    return vol.Schema(fields)


def validate_device_source(user_input: dict) -> dict[str, str]:
    """Return ``{"ha_device": "required"}`` when no device was picked, else an empty dict."""
    return missing_required(user_input, [DEVICE_SOURCE])


def device_details_schema(d: dict, profile: MeterProfile, meters: Collection[str]) -> vol.Schema:
    """Build a device's details form, pre-filled from ``d``: the name and one meter field per role.

    Args:
        d: The previous submission, or the draft's pre-fill values.
        meters: The picked device's qualifying meters.
    """
    fields: dict = {vol.Optional(DEVICE_NAME, description=suggest(d.get(DEVICE_NAME))): str}
    fields |= _meter_fields(d, profile, meters)
    return vol.Schema(fields)


def device_form_values(dev: MeteredDevice) -> dict:
    """Return a linked device as form values keyed by the forms' field keys, to pre-fill its edit forms."""
    values: dict[str, Any] = {
        DEVICE_CATEGORY: dev.category,
        DEVICE_NAME: dev.name,
        DEVICE_SOURCE: dev.ha_device_id or None,
    }
    for slot in dev.profile.slots:
        entities = list(dev.entities_for(slot.role))
        values[meter_key(slot.role)] = entities
    return values


def validate_device(
    user_input: dict,
    devices: list[MeteredDevice],
    profile: MeterProfile,
) -> dict[str, str]:
    """Return field-keyed errors for a metered device being added or edited; empty when it can be saved.

    Args:
        devices: The other linked devices; an edited device excludes itself.
    """
    errors: dict[str, str] = {}
    name = str(user_input.get(DEVICE_NAME, "")).strip()
    if not name:
        errors[DEVICE_NAME] = "device_name_required"
    elif any(dev.name.casefold() == name.casefold() for dev in devices):
        errors[DEVICE_NAME] = "device_name_duplicate"
    linked = {meter for dev in devices for meter in dev.meter_entities}
    used: set[str] = set()
    for slot in profile.slots:
        key = meter_key(slot.role)
        chosen = selected_meters(user_input, slot.role)
        if slot.required and not chosen:
            errors[key] = "required"
        elif any(entity in linked for entity in chosen):
            errors[key] = "device_entity_linked"
        elif used & set(chosen) or len(set(chosen)) != len(chosen):
            errors[key] = "device_same_meter"
        used |= set(chosen)
    return errors


def remove_devices_schema(devices: list[MeteredDevice]) -> vol.Schema:
    """Build the unlink-devices form: a multi-select of device ids, one tick box per linked device."""
    options = [{"value": dev.id, "label": dev.name} for dev in devices]
    return vol.Schema({vol.Optional(REMOVE_DEVICE_IDS, default=[]): list_selector(options, multiple=True)})


def edit_device_pick_schema(devices: list[MeteredDevice]) -> vol.Schema:
    """Build the pick-a-device-to-edit form: a single-select of device ids, the first pre-selected.

    Args:
        devices: The devices that may be edited, those 10s no longer holds.
    """
    options = [{"value": dev.id, "label": dev.name} for dev in devices]
    if not options:
        return vol.Schema({vol.Optional(EDIT_DEVICE_ID): list_selector(options)})
    return vol.Schema({vol.Optional(EDIT_DEVICE_ID, default=options[0]["value"]): list_selector(options)})
