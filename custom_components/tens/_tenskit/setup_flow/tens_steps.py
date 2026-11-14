"""10s section steps: the account login, the section's menu and Log out.

Log out is the only way out of the section: it wipes the account, every linked device and all registrations, and is
refused while 10s still holds a device. The login asks for the registration token alone. Every login sends Home
Assistant's installation id as ``appInstanceId`` and a reauth the one the stored account was issued under; a ``409``
shows as ``already_registered`` and is left for 10s to resolve, with no fallback to another id. A login redeems in the
environment the host names, 10s production unless configured otherwise; a reauth in the environment the stored account
names, as a token works only where it was issued. Both forms carry the environment tried as the ``environment``
placeholder, so a refusal names it.
"""
from __future__ import annotations

import logging

from homeassistant.data_entry_flow import FlowResult
from homeassistant.helpers import instance_id

from ..contract import CONF_METERED_DEVICES, CONF_TENS_ACCOUNT, PartnerAccount
from ..outbound.partners import (
    TENS_ENVIRONMENTS,
    TENS_PROD,
    PartnerAuthError,
    PartnerConnectionError,
    PartnerRejectedError,
    TensEnvironment,
)
from .core import FlowBase
from .sections import PAGE_TENS_LOGIN, SECTION_DEVICES
from .tens import TENS_REGISTRATION_TOKEN, tens_login_schema, validate_login

_LOGGER = logging.getLogger(__name__)


class TensSteps(FlowBase):
    """The 10s section: the login form and the section's menu."""

    # One-shot warning line shown above the 10s menu, set by a refused action.
    _tens_notice: str = ""

    async def _async_installation_id(self) -> str:
        """Return this Home Assistant installation's id, sent to 10s as ``appInstanceId``; overridable in tests."""
        return await instance_id.async_get(self.hass)  # type: ignore[attr-defined]

    def _refuse_while_held(self, action: str) -> bool:
        """Return True, arming the menu's warning line, when 10s still holds a device.

        Logging out would orphan the registration on 10s's side. A device whose status the loaded entry does not know is
        treated as held, so the refusal fails closed.

        Args:
            action: What was refused, named in the warning line.
        """
        names = [d.name for d in self._devices() if d.id in self._held_device_ids()]
        if not names:
            self._tens_notice = ""
            return False
        self._tens_notice = (
            f"⚠ **Cannot {action} yet** — 10s still holds "
            + ", ".join(f"**{name}**" for name in names)
            + ". Withdraw them in the 10s panel first."
        )
        return True

    async def async_step_tens(self, user_input: dict | None = None) -> FlowResult:
        """Open the 10s section: the login form, or the account menu once logged in."""
        if PartnerAccount.from_config(self._data.get(CONF_TENS_ACCOUNT)) is None:
            return await self.async_step_tens_login()
        return await self.async_step_tens_menu()

    async def async_step_tens_menu(self, user_input: dict | None = None) -> FlowResult:
        """Show the logged-in menu: the metered devices, Log out, Back."""
        account = PartnerAccount.from_config(self._data.get(CONF_TENS_ACCOUNT))
        notice, self._tens_notice = self._tens_notice, ""
        devices = self._devices()
        summary = f"{len(devices)} metered device(s) reported to 10s." if devices else (
            "No metered devices yet — link them under **Metered devices**."
        )
        return self.async_show_menu(  # type: ignore[attr-defined]
            step_id="tens_menu",
            menu_options=[SECTION_DEVICES, "tens_logout", self._HUB_STEP_ID],
            description_placeholders={
                "account": account.label if account else "",
                "status": f"{notice}\n\n{summary}" if notice else summary,
            },
        )

    async def async_step_tens_login(self, user_input: dict | None = None) -> FlowResult:
        """Redeem a 10s registration token; on success persist the account at once.

        The client secret comes back exactly once, so the account goes to :meth:`FlowBase._async_persist_account` in
        this same
        step: a flow cancelled afterwards must not lose it. Reached with an account stored, the section just re-opens.
        """
        if (moved := await self._async_follow(user_input)) is not None:
            return moved
        if PartnerAccount.from_config(self._data.get(CONF_TENS_ACCOUNT)) is not None:
            return await self.async_step_tens_menu()
        errors: dict[str, str] = {}
        environment = self._login_environment()
        if user_input is not None:
            account, errors = await self._async_redeem(user_input, await self._async_installation_id(), environment)
            if account is not None:
                self._data[CONF_TENS_ACCOUNT] = account.as_config()
                if (done := await self._async_persist_account(account)) is not None:
                    return done
                return await self.async_step_tens_menu()

        return self._show_form(
            step_id=PAGE_TENS_LOGIN,
            back_to=self._HUB_STEP_ID,
            errors=errors,
            data_schema=tens_login_schema(),
            description_placeholders={"environment": environment.name},
        )

    async def _async_redeem(
        self, user_input: dict, app_instance_id: str, environment: TensEnvironment,
    ) -> tuple[PartnerAccount | None, dict[str, str]]:
        """Validate the login form and redeem its registration token.

        A refusal is only shown as the form error it maps to; nothing is retried under another id or environment.

        Args:
            app_instance_id: The installation's id at 10s, sent with the token.
            environment: The 10s environment the token is redeemed in.

        Returns:
            The redeemed account and no errors, or None and the form's errors.
        """
        errors = validate_login(user_input)
        if errors:
            return None, errors
        try:
            account = await self._tens_client(environment).async_redeem(
                str(user_input[TENS_REGISTRATION_TOKEN]).strip(), app_instance_id,
            )
        except PartnerAuthError:
            return None, {"base": "invalid_token"}
        except PartnerRejectedError as err:
            return None, {"base": "already_registered" if err.status == 409 else "redeem_rejected"}
        except PartnerConnectionError:
            return None, {"base": "cannot_connect"}
        return account, {}

    async def async_step_reauth_confirm(self, user_input: dict | None = None) -> FlowResult:
        """Ask for a fresh registration token once 10s stops accepting the stored login.

        The token is redeemed under the ``appInstanceId`` the stored account was issued for (the installation id when it
        has none), so the devices it registered stay listed, and in the environment the stored account names, with no
        choice offered: another environment means logging out and in. Devices and the registration mirror are kept; the
        host replaces the credentials. A redemption returning a different app registration ``id`` is warned about at
        once: devices hang on that id at 10s, so the old ones will no longer be listed.
        """
        old = PartnerAccount.from_config(self._data.get(CONF_TENS_ACCOUNT))
        environment = TENS_ENVIRONMENTS[old.environment] if old is not None else TENS_PROD
        errors: dict[str, str] = {}
        if user_input is not None:
            stored_id = old.app_instance_id if old is not None else ""
            account, errors = await self._async_redeem(
                user_input, stored_id or await self._async_installation_id(), environment,
            )
            if account is not None:
                if old is not None and account.account_id != old.account_id:
                    _LOGGER.warning(
                        "10s issued app registration %s for this reauth, not the stored %s; devices registered "
                        "under the old one are not listed under the new credentials",
                        account.account_id, old.account_id,
                    )
                return await self._async_finish_reauth(account)
        return self.async_show_form(  # type: ignore[attr-defined]
            step_id="reauth_confirm",
            data_schema=tens_login_schema(),
            description_placeholders={"account": old.label if old else "", "environment": environment.name},
            errors=errors,
        )

    async def async_step_tens_logout(self, user_input: dict | None = None) -> FlowResult:
        """Log out of 10s: wipe the account, the devices and every registration.

        The wipe is immediate and also goes through the running entry's runtime, so it survives a cancelled flow.
        Refused while 10s still holds a device, as the registration would be orphaned. A refusal by the host shows as
        the menu's warning line and nothing is cleared.
        """
        if self._refuse_while_held("log out of 10s"):
            return await self.async_step_tens_menu()
        try:
            await self._async_logout_partner()
        except ValueError as err:
            self._tens_notice = f"⚠ **Cannot log out of 10s yet** — {err}."
            return await self.async_step_tens_menu()
        self._data[CONF_TENS_ACCOUNT] = ""
        self._data[CONF_METERED_DEVICES] = []
        return await self._async_show_hub()
