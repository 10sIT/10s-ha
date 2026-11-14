"""Metered-devices section steps: the devices menu and the add and edit forms.

A device the partner still holds is neither edited nor removed (``FlowBase._held_device_ids``), so a registration is
never silently orphaned.
"""
from __future__ import annotations

import uuid

from homeassistant.data_entry_flow import FlowResult

from ..contract import DeviceEnergy, MeteredDevice, profile_for
from ..inbound import MeterScan, device_meters, pre_pick, read_device_energy, read_energy_kwh
from .core import FlowBase
from .devices import (
    DEFAULT_CATEGORY,
    DEVICE_CATEGORY,
    DEVICE_NAME,
    DEVICE_SOURCE,
    EDIT_DEVICE_ID,
    REMOVE_DEVICE_IDS,
    device_details_schema,
    device_form_values,
    device_source_schema,
    edit_device_pick_schema,
    meter_bindings,
    meter_key,
    remove_devices_schema,
    validate_device,
    validate_device_source,
)
from .sections import SECTION_DEVICES

# "" reads as the device's own meter.
_ROLE_LABELS = {"consumed": "", "returned": "returned"}


class DeviceSteps(FlowBase):
    """The metered-devices section: its menu and the forms behind it."""

    _editing_device_id: str | None = None
    # What a device's first form resolved, carried to its details form: ``ha_device_id``, ``category``, ``meters`` (the
    # qualifying counters), ``source`` (display name), ``values`` (the details pre-fill) and ``warning`` ("" or a note).
    _device_draft: dict | None = None

    def _device_meters(self, ha_device_id: str, exclude: set[str]) -> MeterScan:
        """Return a Home Assistant device's candidate meters, or why it has none; overridable in tests.

        Args:
            ha_device_id: Device-registry id chosen on the picker.
            exclude: Meters already linked to another device.
        """
        return device_meters(
            self.hass,  # type: ignore[attr-defined]
            ha_device_id,
            exclude,
            excluded_platforms=self._excluded_platforms(),
        )

    def _meter_kwh(self, entity_id: str) -> float | None:
        """Return one counter's reading in kWh, or None; overridable in tests."""
        return read_energy_kwh(self.hass, entity_id)  # type: ignore[attr-defined]

    def _device_energy(self, device: MeteredDevice) -> DeviceEnergy:
        """Return a linked device's translated energy; overridable in tests."""
        return read_device_energy(self.hass, device)  # type: ignore[attr-defined]

    def _linked_meters(self, skip_id: str | None = None) -> set[str]:
        """Return the meters linked to every device except ``skip_id``."""
        return {meter for dev in self._devices() if dev.id != skip_id for meter in dev.meter_entities}

    def _meters_line(self, dev: MeteredDevice) -> str:
        """Render one device's counters with their translated readings, for the devices menu."""
        energy = self._device_energy(dev)
        parts = []
        for slot in dev.profile.slots:
            entities = dev.entities_for(slot.role)
            if not entities:
                continue
            value = energy.consumed_kwh if slot.output == "consumed" else energy.returned_kwh
            reading = f"{value:.2f} kWh" if value is not None else "no reading yet"
            label = _ROLE_LABELS[slot.role]
            names = " + ".join(f"`{entity}`" for entity in entities)
            parts.append(f"{label} {names}: {reading}".strip())
        return ", ".join(parts) or "no meter"

    def _device_lines(self) -> str:
        """Render the linked devices, each with its meters' current readings."""
        lines = []
        for dev in self._devices():
            lines.append(f"- **{dev.name}** ({dev.category_label}) — {self._meters_line(dev)}")
        return "\n".join(lines) or "- No devices linked yet."

    def _held_note(self) -> str:
        """Name the devices the partner still holds, which the edit and remove forms leave out."""
        held = self._held_device_ids()
        names = [d.name for d in self._devices() if d.id in held]
        if not names:
            return ""
        return (
            "\n\nStill registered with 10s, so not listed: "
            + ", ".join(f"**{name}**" for name in names)
            + ". Withdraw them in the 10s panel first."
        )

    def _editable_device(self) -> MeteredDevice | None:
        """Return the device the edit forms are open on, or None once the partner holds it.

        Re-checked on every edit form: the panel can re-register the device while a form is open.
        """
        device = next((d for d in self._devices() if d.id == self._editing_device_id), None)
        if device is None or device.id in self._held_device_ids():
            return None
        return device

    def _save_device(self, device: MeteredDevice | None, fields: dict) -> None:
        """Append a new device, or change ``device`` in place keeping its id.

        Args:
            device: The edited device, or None to add one.
            fields: The device's form-derived fields.
        """
        devices = self._devices()
        if device is None:
            devices.append(MeteredDevice(id=uuid.uuid4().hex, **fields))
        else:
            devices = [device.with_fields(**fields) if d.id == device.id else d for d in devices]
        self._store_devices(devices)
        self._device_draft = None

    def _no_reading_warning(self, entities: list[str]) -> str:
        """Return a note naming pre-filled counters with no reading yet, or "".

        Not an error: a fresh *Integral* helper is unknown until its power sensor first changes.
        """
        silent = [entity for entity in entities if self._meter_kwh(entity) is None]
        if not silent:
            return ""
        names = ", ".join(f"`{entity}`" for entity in silent)
        return (
            f"\n\n⚠ No reading yet from {names}. It can still be linked; 10s reads it once "
            "Home Assistant publishes a value in an energy unit."
        )

    def _draft_for(
        self, ha_device_id: str, category: str, device: MeteredDevice | None,
    ) -> tuple[dict | None, MeterScan]:
        """Resolve a picked Home Assistant device and kind into the details form's draft.

        Args:
            device: The edited device, or None when adding one.

        Returns:
            ``(draft, scan)``; the draft is None when the picked device has no qualifying meter, and the scan says why.
        """
        values = device_form_values(device) if device else {}
        scan = self._device_meters(ha_device_id, self._linked_meters(device.id if device else None))
        if not scan.candidates:
            return None, scan
        if device is None:
            values[DEVICE_NAME] = self._ha_device_name(ha_device_id)
        prefilled: list[str] = []
        for slot in profile_for(category).slots:
            key = meter_key(slot.role)
            stored = values.get(key)
            stored = [e for e in (stored if isinstance(stored, list) else [stored]) if e in scan.candidates]
            # A newly picked device, a changed kind or a stale stored meter re-picks.
            chosen = stored or pre_pick(scan, slot)
            values[key] = chosen
            prefilled += chosen
        draft = {"ha_device_id": ha_device_id, "category": category,
                 "meters": list(scan.candidates), "source": self._ha_device_name(ha_device_id) or "the picked device",
                 "values": values, "warning": self._no_reading_warning(prefilled)}
        return draft, scan

    async def _async_device_source(
        self, step_id: str, details_step: str, user_input: dict | None, device: MeteredDevice | None,
    ) -> FlowResult:
        """Show or handle a device's first form: the Home Assistant device that measures it, and its kind.

        Args:
            step_id: This form's step id.
            details_step: The step id of the details form that follows.
            user_input: Submitted values, or None to show the form.
            device: The edited device, or None when adding one.

        Returns:
            The details form once a device with a meter is picked, else this form.
        """
        errors: dict[str, str] = {}
        sensor = ""
        if user_input is None:
            self._device_draft = None  # a fresh start; never reuse a discarded draft
        else:
            errors = validate_device_source(user_input)
            if not errors:
                category = user_input.get(DEVICE_CATEGORY) or DEFAULT_CATEGORY
                draft, scan = self._draft_for(user_input[DEVICE_SOURCE], category, device)
                if draft is None:
                    errors = {DEVICE_SOURCE: scan.refusal or "device_no_meter"}
                    sensor = scan.refusal_sensor
                else:
                    self._device_draft = draft
                    return await getattr(self, f"async_step_{details_step}")()

        return self._show_form(
            step_id=step_id,
            back_to=SECTION_DEVICES,
            save_label="✓ continue to the device's details",
            errors=errors,
            data_schema=device_source_schema(user_input or (device_form_values(device) if device else {})),
            description_placeholders={"name": device.name if device else "", "sensor": sensor},
        )

    async def _async_device_details(
        self, step_id: str, user_input: dict | None, device: MeteredDevice | None,
    ) -> FlowResult:
        """Show or save a device's details form, its meters resolved on the first form.

        Args:
            user_input: Submitted values, or None to show the form.
            device: The edited device, or None when adding one.

        Returns:
            The devices menu once saved or when there is no draft, else this form.
        """
        draft = self._device_draft
        if draft is None:
            return await self.async_step_devices()
        profile = profile_for(draft["category"])
        errors: dict[str, str] = {}
        if user_input is not None:
            others = [d for d in self._devices() if device is None or d.id != device.id]
            errors = validate_device(user_input, others, profile)
            if not errors:
                self._save_device(device, {
                    "name": str(user_input[DEVICE_NAME]).strip(),
                    "category": draft["category"],
                    "meters": meter_bindings(user_input, profile),
                    "ha_device_id": draft["ha_device_id"],
                })
                return await self.async_step_devices()

        return self._show_form(
            step_id=step_id,
            back_to=SECTION_DEVICES,
            errors=errors,
            data_schema=device_details_schema(user_input or draft["values"], profile, draft["meters"]),
            description_placeholders={
                "name": device.name if device else "", "source": draft["source"], "warning": draft["warning"],
            },
        )

    async def async_step_devices(self, user_input: dict | None = None) -> FlowResult:
        """Show the metered-devices menu: add, edit, remove, back."""
        devices = self._devices()
        options = ["add_another_device" if devices else "add_device"]
        if devices:
            options += ["edit_device", "remove_devices"]
        options.append(self._HUB_STEP_ID)
        return self.async_show_menu(  # type: ignore[attr-defined]
            step_id=SECTION_DEVICES,
            menu_options=options,
            description_placeholders={"devices": self._device_lines(), "held": self._held_note()},
        )

    async def async_step_add_device(self, user_input: dict | None = None) -> FlowResult:
        """Pick the Home Assistant device that measures a new device, then open its details."""
        if (moved := await self._async_follow(user_input)) is not None:
            return moved
        return await self._async_device_source("add_device", "add_device_details", user_input, None)

    async def async_step_add_another_device(self, user_input: dict | None = None) -> FlowResult:
        """Link another metered device with the same forms as the first."""
        return await self.async_step_add_device()

    async def async_step_add_device_details(self, user_input: dict | None = None) -> FlowResult:
        """Check a new device's details, its meter already picked, then return to the menu."""
        if (moved := await self._async_follow(user_input)) is not None:
            return moved
        return await self._async_device_details("add_device_details", user_input, None)

    async def async_step_remove_devices(self, user_input: dict | None = None) -> FlowResult:
        """Unlink the ticked devices, then return to the devices menu.

        Devices 10s still holds are left off the list and ignored if submitted anyway; they are withdrawn from the panel
        first.
        """
        if (moved := await self._async_follow(user_input)) is not None:
            return moved
        held = self._held_device_ids()
        if user_input is not None:
            drop = set(user_input.get(REMOVE_DEVICE_IDS) or []) - held
            self._store_devices([d for d in self._devices() if d.id not in drop])
            return await self.async_step_devices()

        return self._show_form(
            step_id="remove_devices",
            back_to=SECTION_DEVICES,
            data_schema=remove_devices_schema([d for d in self._devices() if d.id not in held]),
            description_placeholders={"held": self._held_note()},
        )

    async def async_step_edit_device(self, user_input: dict | None = None) -> FlowResult:
        """Pick the device to edit, then open its forms.

        Only devices 10s no longer holds are listed, and a held id submitted anyway is ignored: a registered device's
        meter is what 10s is being sent.
        """
        if (moved := await self._async_follow(user_input)) is not None:
            return moved
        held = self._held_device_ids()
        editable = [d for d in self._devices() if d.id not in held]
        if user_input is not None:
            chosen = next((d for d in editable if d.id == user_input.get(EDIT_DEVICE_ID)), None)
            if chosen is None:
                return await self.async_step_devices()
            self._editing_device_id = chosen.id
            return await self.async_step_edit_device_source()

        return self._show_form(
            step_id="edit_device",
            back_to=SECTION_DEVICES,
            save_label="✓ edit the chosen device" if editable else None,
            data_schema=edit_device_pick_schema(editable),
            description_placeholders={"held": self._held_note()},
        )

    async def async_step_edit_device_source(self, user_input: dict | None = None) -> FlowResult:
        """Change which Home Assistant device a withdrawn device reads from."""
        if (moved := await self._async_follow(user_input)) is not None:
            return moved
        if (device := self._editable_device()) is None:
            return await self.async_step_devices()
        return await self._async_device_source("edit_device_source", "edit_device_details", user_input, device)

    async def async_step_edit_device_details(self, user_input: dict | None = None) -> FlowResult:
        """Change one withdrawn device in place, then return to the menu.

        Keeping the id keeps the device's registration row and chart colour, which remove and add again would reset.
        """
        if (moved := await self._async_follow(user_input)) is not None:
            return moved
        if (device := self._editable_device()) is None:
            return await self.async_step_devices()
        return await self._async_device_details("edit_device_details", user_input, device)
