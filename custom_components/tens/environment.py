"""Say which 10s environment a login and a config entry talk to, and build the client for it.

An account works only in the 10s environment that issued it, so the entry's environment is the one its stored account
names: 10s production while there is none. A new login redeems in the environment named by ``environment:`` under
``tens:`` in Home Assistant's configuration file, read once at start-up, and in 10s production when that is not set.
The coordinator and the config flow both build their client here, on Home Assistant's shared session, which verifies
certificates.
"""
from __future__ import annotations

from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from ._tenskit.contract import PartnerAccount
from ._tenskit.outbound.partners import TENS_ENVIRONMENTS, TENS_PROD, PartnerClient, TensClient, TensEnvironment
from .const import DOMAIN

__all__ = ["CONF_ENVIRONMENT", "configure_login", "for_login", "make_client", "of_account"]

CONF_ENVIRONMENT = "environment"

_LOGIN_ENVIRONMENT_KEY = f"{DOMAIN}_login_environment"


def of_account(account: PartnerAccount | None) -> TensEnvironment:
    """Return the 10s environment an account was issued in; 10s production when there is no account."""
    if account is None:
        return TENS_PROD
    return TENS_ENVIRONMENTS[account.environment]


def configure_login(hass: HomeAssistant, environment_name: str | None) -> None:
    """Record the 10s environment the configuration file names for new logins; None leaves them on 10s production.

    Called once, at the integration's start-up, with a name the configuration schema has already checked.
    """
    if environment_name is not None:
        hass.data[_LOGIN_ENVIRONMENT_KEY] = TENS_ENVIRONMENTS[environment_name]


def for_login(hass: HomeAssistant) -> TensEnvironment:
    """Return the 10s environment a new login redeems in: the configured one, 10s production when none is."""
    return hass.data.get(_LOGIN_ENVIRONMENT_KEY, TENS_PROD)


def make_client(hass: HomeAssistant, environment: TensEnvironment) -> PartnerClient:
    """Return the 10s client for an environment, on Home Assistant's shared, certificate-verifying session."""
    session = async_get_clientsession(hass)
    return TensClient(session, environment)
