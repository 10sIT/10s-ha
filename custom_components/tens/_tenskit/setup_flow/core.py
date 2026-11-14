"""Flow state and navigation the 10s steps build on.

A host's flows accumulate into ``self._data``; the options flow seeds it with the entry's merged data and options, so it
doubles as the form defaults. The host's flow core subclasses :class:`FlowBase` and supplies its root menu, its
device-hold answers, the 10s client and the 10s environment a new login redeems in (10s production unless it names
another).
"""
from __future__ import annotations

from typing import TYPE_CHECKING, Any

from homeassistant.data_entry_flow import FlowResult

from ..contract import CONF_METERED_DEVICES, MeteredDevice, PartnerAccount, as_config_list, metered_devices
from ..inbound import device_name
from ..outbound.partners import TENS_PROD
from .fields import GO_TO, SAVE, next_options, with_next

if TYPE_CHECKING:
    from ..outbound.partners import PartnerClient, TensEnvironment


class FlowBase:
    """The metered-device and 10s flow surface a host flow core extends.

    Attributes:
        _HUB_STEP_ID: The host's root-menu step id.
        _data: The accumulated config and options values.
        _BACK_NAMES: Step id to the parent name shown in "discard and go back"; the host extends it with its root ids.
    """

    _HUB_STEP_ID: str
    _data: dict[str, Any]
    _BACK_NAMES: dict[str, str] = {}

    async def _async_finish(self) -> FlowResult:
        """Persist ``self._data``: a new entry, or the entry's options."""
        raise NotImplementedError

    def _devices(self) -> list[MeteredDevice]:
        """Return the metered devices linked so far."""
        return list(metered_devices(self._data))

    def _store_devices(self, devices: list[MeteredDevice]) -> None:
        """Write the device list back to the accumulator as stored rows, order preserved."""
        self._data[CONF_METERED_DEVICES] = as_config_list(devices)

    def _ha_device_name(self, ha_device_id: str) -> str:
        """Return a Home Assistant device's display name, or ""; overridable in tests."""
        return device_name(self.hass, ha_device_id)  # type: ignore[attr-defined]

    def _held_device_ids(self) -> set[str]:
        """Return ids of linked devices the partner still holds; overridable by the host.

        The config flow runs before any device reaches the partner, so nothing is held there.
        """
        return set()

    def _login_environment(self) -> TensEnvironment:
        """Return the 10s environment a new login redeems in; overridable by the host, 10s production by default.

        A reauth does not ask: it redeems in the environment the stored account names.
        """
        return TENS_PROD

    def _tens_client(self, environment: TensEnvironment, /) -> PartnerClient:
        """Return the 10s client a login or reauth redeems through, bound to ``environment``; implemented by the host.

        The step picks the environment; the host builds the client on its HTTP session.
        """
        raise NotImplementedError

    async def _async_persist_account(self, account: PartnerAccount) -> FlowResult | None:
        """Persist a freshly redeemed account before the flow goes on; implemented by the host.

        The client secret is shown once, so the account must reach storage in the step that redeemed it, not at Finish.
        A host with a running entry writes it there and returns None; a new install has no entry yet, so the host parks
        the account where Finish will find it and returns the root menu, still logged in.

        Args:
            account: The account the redemption returned, already in ``self._data``.

        Returns:
            A flow result to show instead of the section's menu, or None to continue to it.
        """
        raise NotImplementedError

    async def _async_finish_reauth(self, account: PartnerAccount) -> FlowResult:
        """Replace the stored account with a re-redeemed one and end the reauth flow; implemented by the host.

        Devices, registrations and readings are left alone; only the credentials change.

        Returns:
            The flow result that ends the reauth, the entry reloaded.
        """
        raise NotImplementedError

    async def _async_logout_partner(self) -> None:
        """Tell the running entry's partner to log out; implemented by the host.

        A flow with no live entry has nothing to log out of. The options flow logs out at once, so the wipe takes effect
        even if the flow is later cancelled.

        Raises:
            ValueError: The partner still holds a device or there is no running entry; the message is shown to the user
                and nothing is cleared.
        """
        raise NotImplementedError

    def _excluded_platforms(self) -> frozenset[str]:
        """Return the platforms whose sensors are never offered as meters; implemented by the host.

        The host extends ``EXCLUDED_PLATFORMS`` with its own domain so it never proposes its own sensors.
        """
        raise NotImplementedError

    async def _async_show_hub(self, error: str = "") -> FlowResult:
        """Show the host's root menu; implemented by the host flow core.

        Args:
            error: Why an action was refused, shown above the status.
        """
        raise NotImplementedError

    async def _async_follow(self, user_input: dict | None) -> FlowResult | None:
        """Return where a form's Confirm dropdown pointed, or None when it saves.

        The reserved SAVE value means save this page; any other value is the parent window's step id and navigates there
        without saving. Popping the key keeps it out of ``self._data``.

        Returns:
            The parent window's flow result, or None to save the page.
        """
        if user_input is None:
            return None
        target = user_input.pop(GO_TO, SAVE)
        if target == SAVE or target not in self._BACK_NAMES:
            return None
        return await getattr(self, f"async_step_{target}")(None)

    def _show_form(self, *, back_to: str, save_label: str | None = None, **kwargs: Any) -> FlowResult:
        """Show a page form with the Confirm dropdown as its last field.

        Every other kwarg is passed to :meth:`async_show_form`.

        Args:
            back_to: Step id of the parent window the discard option returns to.
            save_label: Replaces the default option's text when Confirm does not save and go back.
        """
        kwargs["data_schema"] = with_next(
            kwargs["data_schema"], next_options(back_to, self._BACK_NAMES[back_to], save_label)
        )
        return self.async_show_form(**kwargs)  # type: ignore[attr-defined]
