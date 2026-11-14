"""The verify screen: a read-only look at what 10s will receive, shown before the setup is saved.

It writes nothing; the host decides where it sits in its menu and what confirming does.
"""
from __future__ import annotations

from collections.abc import Mapping

import voluptuous as vol
from homeassistant.data_entry_flow import FlowResult

from ..contract import CONF_TENS_ACCOUNT, MeteredDevice, PartnerAccount
from .device_steps import DeviceSteps

_RECEIVING = "●"
_WAITING = "◐"
_REFUSED = "⚠"


class VerifySteps(DeviceSteps):
    """Builds the verify screen's text and form on top of the device readings."""

    def _eligible_categories(self) -> Mapping[str, str] | None:
        """Return the categories 10s accepts for this account, or None when not known; a hook the host overrides.

        A new install has not asked 10s yet, so the default is unknown.
        """
        return None

    def _verify_device_line(self, device: MeteredDevice, eligible: Mapping[str, str] | None) -> str:
        """Render one device's verdict, refused by 10s, receiving data, or still waiting, as a markdown list item.

        Args:
            eligible: What 10s accepts, category to label, or None when unknown.
        """
        energy = self._device_energy(device)
        readings = [value for value in (energy.consumed_kwh, energy.returned_kwh) if value is not None]
        if eligible is not None and device.category not in eligible:
            note = f"{_REFUSED} 10s does not accept this kind of device, so it cannot be registered"
        elif readings:
            note = f"{_RECEIVING} receiving data"
        else:
            note = f"{_WAITING} no data yet — check the meter"
        return f"- **{device.name}** ({device.category_label}) — {note}"

    def _verify_text(self) -> str:
        """Return the screen's body: the account, then one line per linked device."""
        account = PartnerAccount.from_config(self._data.get(CONF_TENS_ACCOUNT))
        who = account.label if account else "not logged in"
        eligible = self._eligible_categories()
        devices = self._devices()
        lines = "\n".join(self._verify_device_line(dev, eligible) for dev in devices) or "- No devices linked yet."
        return (
            f"**Account:** {who}\n\n{lines}\n\n"
            "A registered device takes effect at 10s at midnight, Brussels time, two days after the day it is "
            "registered. The readings recorded meanwhile go out at the first contact with 10s after that: "
            "Home Assistant contacts 10s when it starts and then every 8 hours."
        )

    def _async_verify_form(self, *, step_id: str, back_to: str) -> FlowResult:
        """Show the read-only screen; its only control is the Confirm dropdown.

        Args:
            step_id: The step this form belongs to.
            back_to: The root menu the discard option returns to.
        """
        return self._show_form(
            back_to=back_to,
            save_label="✓ save the configuration",
            step_id=step_id,
            data_schema=vol.Schema({}),
            description_placeholders={"summary": self._verify_text()},
        )
