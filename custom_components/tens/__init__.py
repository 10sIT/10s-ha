"""10s – metered-device reporting for Home Assistant."""
from __future__ import annotations

import hashlib
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

import voluptuous as vol
from homeassistant.components.frontend import async_remove_panel
from homeassistant.components.http import StaticPathConfig
from homeassistant.components.panel_custom import async_register_panel
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, ServiceCall, callback
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.service import async_register_admin_service
from homeassistant.helpers.typing import ConfigType

from . import environment
from ._tenskit.contract import TENS_ENVIRONMENT_NAMES
from ._tenskit.outbound.partners import PartnerAuthError, PartnerConnectionError, PartnerRejectedError
from .const import DOMAIN
from .coordinator import RegistrationStore, SlotStore, TensCoordinator
from .device_slots_view import TensDeviceSlotsView

_STATIC_REGISTERED_KEY = f"{DOMAIN}_static_registered"
_VIEWS_REGISTERED_KEY = f"{DOMAIN}_views_registered"
_PANEL_MODULE_KEY = f"{DOMAIN}_panel_module"
# Must not equal the sidebar URL: a static route at "/tens" answers a full page load of the panel with 403.
_STATIC_PATH = "/tens_static"
_PANEL_URL_DEFAULT = "tens"
_PANEL_TITLE_DEFAULT = "10s"
_WEBCOMPONENT = "tens-panel"
_PANEL_ICON = "mdi:meter-electric"

PLATFORMS = ["sensor"]

SERVICE_WITHDRAW_DEVICE = "withdraw_device"
SERVICE_RE_REGISTER_DEVICE = "re_register_device"
SERVICE_REMOVE_DEVICE = "remove_device"
SERVICE_SET_DEVICE_COLOR = "set_device_color"

# The 10s device id, not a Home Assistant device-registry id.
ATTR_DEVICE_ID = "metered_device_id"
ATTR_COLOR = "color"

# The integration is set up in the UI. ``tens:`` in the configuration file is optional and holds one setting: the 10s
# environment a new login redeems in. A bare ``tens:`` reads as None. Anything else under it fails the set-up.
CONFIG_SCHEMA = vol.Schema(
    {
        vol.Optional(DOMAIN): vol.Any(
            None, vol.Schema({vol.Optional(environment.CONF_ENVIRONMENT): vol.In(TENS_ENVIRONMENT_NAMES)}),
        ),
    },
    extra=vol.ALLOW_EXTRA,
)

_DEVICE_SERVICE_SCHEMA = vol.Schema({vol.Required(ATTR_DEVICE_ID): cv.string})
# "" clears the colour back to the palette's; anything else must be ``#rrggbb``, which the runtime re-checks.
_COLOR_SERVICE_SCHEMA = _DEVICE_SERVICE_SCHEMA.extend({vol.Required(ATTR_COLOR): vol.Any("", cv.string)})


def _js_hash(filename: str) -> str:
    """Return the first 8 hex characters of the MD5 hash of a www/ file for cache-busting, or "0" if unreadable."""
    path = Path(__file__).parent / "www" / filename
    try:
        return hashlib.md5(path.read_bytes()).hexdigest()[:8]
    except OSError:
        return "0"


async def _async_register_panel(hass: HomeAssistant) -> None:
    """Register the 10s sidebar panel at ``/tens``, unless it already stands with the current JS.

    The panel outlives a reload: a browser sitting on a panel that is taken away is sent to the default
    dashboard, and every options write reloads the entry.
    """
    # A blocking read, which Home Assistant forbids on the event loop.
    js_hash = await hass.async_add_executor_job(_js_hash, "tens-panel.js")
    module_url = f"{_STATIC_PATH}/tens-panel.js?v={js_hash}"
    if hass.data.get(_PANEL_MODULE_KEY) == module_url:
        return
    _async_remove_panel(hass)
    await async_register_panel(
        hass,
        frontend_url_path=_PANEL_URL_DEFAULT,
        webcomponent_name=_WEBCOMPONENT,
        sidebar_title=_PANEL_TITLE_DEFAULT,
        sidebar_icon=_PANEL_ICON,
        module_url=module_url,
        require_admin=False,
    )
    hass.data[_PANEL_MODULE_KEY] = module_url


@callback
def _async_remove_panel(hass: HomeAssistant) -> None:
    """Take the sidebar panel away, if this integration registered it and it still stands."""
    if hass.data.pop(_PANEL_MODULE_KEY, None) is not None:
        async_remove_panel(hass, _PANEL_URL_DEFAULT)


def _coordinator_for_device(hass: HomeAssistant, device_id: str) -> TensCoordinator:
    """Return the loaded coordinator, provided its entry links the 10s device id.

    Raises:
        ServiceValidationError: No entry is loaded or it does not link the device.
    """
    coordinator: TensCoordinator | None = hass.data.get(DOMAIN)
    if coordinator is None or coordinator.partner.device(device_id) is None:
        raise ServiceValidationError(f"No 10s device with id {device_id!r}")
    return coordinator


@contextmanager
def _service_errors() -> Iterator[None]:
    """Turn the runtime's and the partner's refusals into service-call errors.

    A refusal the caller can act on is shown as the reason without a traceback; 10s being unreachable or
    refusing the login is not the call's fault, so the action failed and may work later.

    Raises:
        ServiceValidationError: On a ``ValueError`` or a ``PartnerRejectedError``.
        HomeAssistantError: On a ``PartnerConnectionError`` or a ``PartnerAuthError``.
    """
    try:
        yield
    except ValueError as err:
        raise ServiceValidationError(str(err)) from err
    except PartnerRejectedError as err:
        raise ServiceValidationError(_reason(f"10s refused the request (HTTP {err.status})", err)) from err
    except PartnerConnectionError as err:
        raise HomeAssistantError(_reason("10s could not be reached; try again later", err)) from err
    except PartnerAuthError as err:
        raise HomeAssistantError(
            _reason("10s no longer accepts the login; Home Assistant will ask for a new registration token", err),
        ) from err


def _reason(summary: str, err: Exception) -> str:
    """Return ``summary`` followed by the error's text, unless that is empty or only repeats the HTTP status."""
    detail = str(err)
    if not detail or (isinstance(err, PartnerRejectedError) and detail.endswith(f"(HTTP {err.status})")):
        return summary
    return f"{summary}: {detail}"


@callback
def _async_register_services(hass: HomeAssistant) -> None:
    """Register the four admin-only services, once per Home Assistant run.

    Registered from :func:`async_setup` rather than per config entry, so a reload does not register them twice.
    """

    async def handle_withdraw_device(call: ServiceCall) -> None:
        """Ask 10s to withdraw a registered device."""
        device_id = call.data[ATTR_DEVICE_ID]
        coordinator = _coordinator_for_device(hass, device_id)
        with _service_errors():
            await coordinator.partner.async_withdraw(device_id)

    async def handle_re_register_device(call: ServiceCall) -> None:
        """Ask 10s to register a withdrawn device again."""
        device_id = call.data[ATTR_DEVICE_ID]
        coordinator = _coordinator_for_device(hass, device_id)
        with _service_errors():
            await coordinator.partner.async_re_register(device_id, datetime.now(UTC))

    async def handle_remove_device(call: ServiceCall) -> None:
        """Remove a 10s device from its entry and drop the lifecycle row."""
        device_id = call.data[ATTR_DEVICE_ID]
        coordinator = _coordinator_for_device(hass, device_id)
        with _service_errors():
            await coordinator.partner.async_remove(device_id)

    async def handle_set_device_color(call: ServiceCall) -> None:
        """Set a 10s device's chart colour, or clear it back to the palette's."""
        device_id = call.data[ATTR_DEVICE_ID]
        coordinator = _coordinator_for_device(hass, device_id)
        with _service_errors():
            await coordinator.partner.async_set_device_color(device_id, call.data[ATTR_COLOR])

    for service, handler, schema in (
        (SERVICE_WITHDRAW_DEVICE, handle_withdraw_device, _DEVICE_SERVICE_SCHEMA),
        (SERVICE_RE_REGISTER_DEVICE, handle_re_register_device, _DEVICE_SERVICE_SCHEMA),
        (SERVICE_REMOVE_DEVICE, handle_remove_device, _DEVICE_SERVICE_SCHEMA),
        (SERVICE_SET_DEVICE_COLOR, handle_set_device_color, _COLOR_SERVICE_SCHEMA),
    ):
        if not hass.services.has_service(DOMAIN, service):
            async_register_admin_service(hass, DOMAIN, service, handler, schema=schema)


@callback
def _async_register_views(hass: HomeAssistant) -> None:
    """Register the integration's HTTP views, once per Home Assistant run.

    Home Assistant has no public API to take an aiohttp route back out, so a reload must not add a second one.
    """
    if hass.data.get(_VIEWS_REGISTERED_KEY):
        return
    hass.http.register_view(TensDeviceSlotsView())
    hass.data[_VIEWS_REGISTERED_KEY] = True


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Set up what outlives any single config entry: the services, the views and the environment of new logins.

    Home Assistant calls this once per run, so a change to ``tens:`` in the configuration file needs a restart.
    """
    tens_config = config.get(DOMAIN) or {}
    environment.configure_login(hass, tens_config.get(environment.CONF_ENVIRONMENT))
    _async_register_services(hass)
    _async_register_views(hass)
    return True


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up 10s from a config entry without waiting on 10s.

    Everything but the first reconcile is up before 10s is asked anything, so a 10s that hangs, fails or
    refuses the login at start-up leaves the entry loaded and sampling.
    """
    coordinator = TensCoordinator(hass, entry)
    await coordinator.async_setup()

    hass.data[DOMAIN] = coordinator
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    entry.async_on_unload(entry.add_update_listener(async_handle_entry_update))
    entry.async_on_unload(coordinator.async_start_sampling())

    if not hass.data.get(_STATIC_REGISTERED_KEY):
        await hass.http.async_register_static_paths([
            StaticPathConfig(
                _STATIC_PATH,
                str(Path(__file__).parent / "www"),
                cache_headers=False,
            )
        ])
        hass.data[_STATIC_REGISTERED_KEY] = True

    await _async_register_panel(hass)
    # Last, so the entities already listen when the refresh ends. Home Assistant starts it eagerly: this
    # mark's reading is taken inside the set-up, which still never waits for 10s.
    entry.async_create_background_task(hass, coordinator.async_refresh(), f"{DOMAIN}: first reconcile")
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload the config entry: its coordinator and its repairs, and, when disabled, the sidebar panel.

    The services and the www/ static path stay registered: Home Assistant has no public API to remove an
    aiohttp route, and ``_STATIC_REGISTERED_KEY`` keeps a re-setup from adding it twice.

    Returns:
        True when all platforms unloaded successfully.
    """
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok:
        hass.data.pop(DOMAIN).async_clear_issues()
        if entry.disabled_by:
            _async_remove_panel(hass)
    return unload_ok


async def async_remove_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Delete the registration mirror, the slot archive and the sidebar panel once the entry is deleted.

    What 10s holds is not touched: it has no endpoint to revoke a registration, so a device still registered
    stays registered there.
    """
    _async_remove_panel(hass)
    await RegistrationStore(hass, entry.entry_id).async_remove()
    await SlotStore(hass, entry.entry_id).async_remove()


async def async_handle_entry_update(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Reload the entry on an options write; a device's colour lives in the runtime's store and never lands here."""
    await hass.config_entries.async_reload(entry.entry_id)