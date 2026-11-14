"""Config flow and options flow for 10s, both over the kit's account and metered-devices steps.

``TensHubCore`` supplies the root menu and Finish. First setup stays in the flow after the login and the
entry is created by *Check and save*. 10s shows the client secret once and answers a second login from the
same installation with ``409``, so redeeming the token parks the account in a ``Store`` of its own until then.
"""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from homeassistant import config_entries
from homeassistant.core import callback
from homeassistant.data_entry_flow import FlowResult
from homeassistant.helpers.storage import Store

from . import environment
from ._tenskit.contract import (
    CONF_METERED_DEVICES,
    CONF_TENS_ACCOUNT,
    MeteredDevice,
    PartnerAccount,
    as_config_list,
    metered_devices,
)
from ._tenskit.outbound.partners import PartnerClient, TensEnvironment
from ._tenskit.setup_flow import BACK_NAMES, SECTION_TENS, TensSteps, VerifySteps
from .const import DOMAIN, STORAGE_VERSION

__all__ = ["TensConfigFlow", "TensOptionsFlow"]

_LOG_IN_TO_SAVE = "Log in to 10s to save this configuration."
_PENDING_ACCOUNT_KEY = f"{DOMAIN}_pending_account"


def _staged_onto(
    opened: Sequence[MeteredDevice], staged: Sequence[MeteredDevice], current: Sequence[MeteredDevice],
) -> tuple[list[MeteredDevice], list[MeteredDevice]]:
    """Apply the flow's own device changes (its delta against ``opened``) onto the list the entry holds now.

    A device removed elsewhere stays gone even if the flow edited it; one the flow never touched keeps its
    current row.

    Returns:
        ``(devices to write, devices the change removes or edits)``; the second as they stood in ``opened``.
    """
    opened_by_id = {device.id: device for device in opened}
    staged_by_id = {device.id: device for device in staged}
    removed = opened_by_id.keys() - staged_by_id.keys()
    edited = {
        device_id: device for device_id, device in staged_by_id.items()
        if device_id in opened_by_id and opened_by_id[device_id] != device
    }
    added = [device for device in staged if device.id not in opened_by_id]
    written = [edited.get(device.id, device) for device in current if device.id not in removed] + added
    touched = [device for device in opened if device.id in removed or device.id in edited]
    return written, touched


class TensHubCore(VerifySteps):
    """The tens host's flow state, root menu and Finish on top of the shared steps."""

    _HUB_STEP_ID: str
    _HUB_MENU_OPTIONS: list[str]
    # The two host roots join the shared menus so both flows resolve their back rows.
    _BACK_NAMES = {**BACK_NAMES, "hub": "10s", "init": "10s"}

    def _excluded_platforms(self) -> frozenset[str]:
        """Never offer the tens host's own readings as a meter for a device."""
        from ._tenskit.inbound import EXCLUDED_PLATFORMS

        return EXCLUDED_PLATFORMS | {DOMAIN}

    def _login_environment(self) -> TensEnvironment:
        """Return the 10s environment a new login redeems in, as the configuration file names it."""
        return environment.for_login(self.hass)  # type: ignore[attr-defined]

    def _tens_client(self, tens_environment: TensEnvironment, /) -> PartnerClient:
        """Return the 10s client a login or reauth redeems through, built as the entry's coordinator builds its own."""
        return environment.make_client(self.hass, tens_environment)  # type: ignore[attr-defined]

    def _tens_complete(self) -> bool:
        """Return True when a 10s account is stored."""
        return PartnerAccount.from_config(self._data.get(CONF_TENS_ACCOUNT)) is not None

    def _hub_status(self) -> str:
        """Return the root's status: the account and the linked devices."""
        account = PartnerAccount.from_config(self._data.get(CONF_TENS_ACCOUNT))
        if account is None:
            account_line = "- ○ **10s account** — not logged in"
        else:
            account_line = f"- ✓ **10s account** — logged in as {account.label}"
        devices = self._devices()
        devices_line = (
            f"- ✓ **Metered devices** — {len(devices)} linked"
            if devices
            else "- ○ **Metered devices** — none linked yet"
        )
        return f"{account_line}\n{devices_line}"

    async def _async_show_hub(self, error: str = "") -> FlowResult:
        """Show the root menu, rebuilt from the current state; ``error`` is why Finish was refused, if it was."""
        status = self._hub_status()
        if error:
            status = f"⚠ **{error}**\n\n{status}"
        return self.async_show_menu(  # type: ignore[attr-defined]
            step_id=self._HUB_STEP_ID,
            menu_options=self._HUB_MENU_OPTIONS,
            description_placeholders={"status": status},
        )

    async def async_step_verify(self, user_input: dict | None = None) -> FlowResult:
        """Show the read-only check of the setup; confirming it saves (Finish)."""
        if not self._tens_complete():
            return await self._async_show_hub(_LOG_IN_TO_SAVE)
        if user_input is not None:
            followed = await self._async_follow(user_input)
            return followed if followed is not None else await self._async_finish()
        return self._async_verify_form(step_id="verify", back_to=self._HUB_STEP_ID)


class TensSetupSteps(TensHubCore, TensSteps):
    """The tens host's steps: the 10s account and the metered-devices menus."""


# --- Config flow ---

class TensConfigFlow(TensSetupSteps, config_entries.ConfigFlow, domain=DOMAIN):  # type: ignore[call-arg]
    """First-time setup: the root menu, opened on an empty installation."""

    VERSION = 1
    _HUB_STEP_ID = "hub"
    _HUB_MENU_OPTIONS = [SECTION_TENS, "verify"]

    def __init__(self) -> None:
        """Initialise an empty accumulator."""
        self._data: dict[str, Any] = {}

    def _pending_store(self) -> Store:
        """Return the file that parks a redeemed account until the entry exists."""
        return Store(self.hass, STORAGE_VERSION, _PENDING_ACCOUNT_KEY)  # type: ignore[attr-defined]

    async def async_step_user(self, user_input: dict | None = None) -> FlowResult:
        """Open the flow on the root menu, logged in again if an earlier setup was closed unsaved."""
        parked = await self._pending_store().async_load()
        if PartnerAccount.from_config(parked) is not None:
            self._data[CONF_TENS_ACCOUNT] = parked
        return await self._async_show_hub()

    async def async_step_hub(self, user_input: dict | None = None) -> FlowResult:
        """Show the root menu (also the target of every ``← Back`` row)."""
        return await self._async_show_hub()

    async def _async_persist_account(self, account: PartnerAccount) -> FlowResult:
        """Park a redeemed account on disk and return to the root, now logged in.

        There is no entry yet to hold the once-shown client secret, and 10s refuses a second login from this
        installation, so the secret must outlive a closed window.
        """
        await self._pending_store().async_save(account.as_config())
        return await self._async_show_hub()

    async def _async_logout_partner(self) -> None:
        """Forget the parked account, so the next setup starts logged out."""
        await self._pending_store().async_remove()

    async def async_step_reauth(self, entry_data: Mapping[str, Any]) -> FlowResult:
        """Start re-authentication once 10s stopped accepting the stored login; the account lives in the options."""
        entry = self._get_reauth_entry()
        self._data = dict(entry.options)
        return await self.async_step_reauth_confirm()

    async def _async_finish_reauth(self, account: PartnerAccount) -> FlowResult:
        """Write the new credentials into the entry's options and reload it; devices and the mirror are kept."""
        entry = self._get_reauth_entry()
        return self.async_update_reload_and_abort(
            entry, options={**entry.options, CONF_TENS_ACCOUNT: account.as_config()},
        )

    async def _async_finish(self) -> FlowResult:
        """Create the config entry with the accumulated config as its options and no data.

        A copy of the account left in ``data`` would keep a secret 10s cannot revoke on disk after a logout.
        """
        await self._pending_store().async_remove()
        return self.async_create_entry(title="10s", data={}, options=self._data)

    @staticmethod
    @callback
    def async_get_options_flow(
        config_entry: config_entries.ConfigEntry,
    ) -> TensOptionsFlow:
        """Return the options flow handler for this config entry."""
        return TensOptionsFlow(config_entry)


# --- Options flow ---

class TensOptionsFlow(TensSetupSteps, config_entries.OptionsFlow):
    """Reconfigure an entry: the setup menus, opened on the entry's current config."""

    _HUB_STEP_ID = "init"
    _HUB_MENU_OPTIONS = [SECTION_TENS, "verify"]

    def __init__(self, config_entry: config_entries.ConfigEntry) -> None:
        """Seed the accumulator from the entry."""
        self._entry = config_entry
        self._data: dict[str, Any] = dict(config_entry.options)
        self._opened_devices = metered_devices(self._data)

    async def async_step_init(self, user_input: dict | None = None) -> FlowResult:
        """Open the options flow on the root menu (also every ``← Back`` target)."""
        return await self._async_show_hub()

    def _held_device_ids(self) -> set[str]:
        """Return the linked devices the running entry's lifecycle has not withdrawn yet.

        Fails closed: while the entry is not loaded there is no lifecycle to consult, so every stored device
        counts as held. Devices linked earlier in this unsaved flow have never reached 10s and stay editable.
        """
        coordinator = self._coordinator()
        if coordinator is None:
            return {dev.id for dev in self._devices()}
        return set(coordinator.partner.held_device_ids())

    async def _async_persist_account(self, account: PartnerAccount) -> None:
        """Write a redeemed account into the entry's options at once; the once-shown secret must not wait for Finish."""
        self.hass.config_entries.async_update_entry(  # type: ignore[attr-defined]
            self._entry, options={**self._entry.options, CONF_TENS_ACCOUNT: account.as_config()},
        )

    def _eligible_categories(self):
        """Return the categories 10s last listed for the running entry, or None if not loaded or not yet asked."""
        coordinator = self._coordinator()
        return coordinator.partner.snapshot().eligible if coordinator is not None else None

    async def _async_logout_partner(self) -> None:
        """Log the running entry's partner runtime out; a wipe, not a staged edit.

        Raises:
            ValueError: The entry is not loaded, or the runtime refused because a device is held.
        """
        coordinator = self._coordinator()
        if coordinator is None:
            raise ValueError(
                "the 10s integration is not running, so nothing could be wiped; try again once it has loaded"
            )
        await coordinator.partner.async_logout()

    def _coordinator(self):
        """Return this entry's loaded coordinator, or None; tolerates a flow not yet bound to ``hass``."""
        data = getattr(self.hass, "data", None)
        if not isinstance(data, dict):
            return None
        return data.get(DOMAIN)

    async def _async_finish(self) -> FlowResult:
        """Write this flow's device changes onto the entry's current options, or refuse on the root.

        The opened snapshot is never written back: the panel or a reauth may have changed the entry since.
        Refused when the entry holds no account, or the change removes or edits a device 10s holds now.
        """
        current = self._entry.options
        if PartnerAccount.from_config(current.get(CONF_TENS_ACCOUNT)) is None:
            return await self._async_show_hub(_LOG_IN_TO_SAVE)
        stored = metered_devices(current)
        written, touched = _staged_onto(self._opened_devices, self._devices(), stored)
        coordinator = self._coordinator()
        held = coordinator.partner.held_device_ids() if coordinator is not None else {device.id for device in stored}
        refused = ", ".join(device.name for device in touched if device.id in held)
        if refused and coordinator is None:
            return await self._async_show_hub(
                f"Not saved: 10s is not running, so it cannot confirm that {refused} may be removed or edited. "
                "Try again once it has loaded, or close this window to discard the change."
            )
        if refused:
            return await self._async_show_hub(
                f"Not saved: 10s now holds {refused}, which this change removes or edits. "
                "Withdraw them in the 10s panel first, or close this window to discard the change."
            )
        return self.async_create_entry(
            title="", data={**self._entry.options, CONF_METERED_DEVICES: as_config_list(written)},
        )
