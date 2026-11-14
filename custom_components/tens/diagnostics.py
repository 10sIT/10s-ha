"""Diagnostics download for a 10s config entry, with every credential redacted."""
from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from .const import DOMAIN
from .coordinator import TensCoordinator

REDACTED = "**REDACTED**"

# ``client_id`` and ``app_instance_id`` are redacted because 10s can tie them to the account;
# ``contract_id`` / ``contract_ref`` stay as the support handle.
_SECRET_KEYS = frozenset({"client_secret", "client_id", "app_instance_id", "registration_token", "access_token"})


def _redact(value: Any) -> Any:
    """Return a copy of a JSON-like structure with every secret key's value masked, at any depth."""
    if isinstance(value, Mapping):
        return {k: REDACTED if k in _SECRET_KEYS else _redact(v) for k, v in value.items()}
    if isinstance(value, list | tuple):
        return [_redact(v) for v in value]
    return value


async def async_get_config_entry_diagnostics(hass: HomeAssistant, entry: ConfigEntry) -> dict[str, Any]:
    """Return the entry's stored config and the runtime's view, secrets redacted.

    Returns:
        A JSON-safe dict; only ``entry`` is present while no coordinator is loaded.
    """
    coordinator: TensCoordinator | None = hass.data.get(DOMAIN)
    diagnostics: dict[str, Any] = {
        "entry": {"data": _redact(dict(entry.data)), "options": _redact(dict(entry.options))},
    }
    if coordinator is None:
        return diagnostics
    snapshot = coordinator.partner.snapshot()
    account = snapshot.account
    diagnostics["partner"] = {
        "environment": {"name": coordinator.environment.name},
        "account": _redact(account.as_config()) if account else None,
        "eligible": dict(snapshot.eligible) if snapshot.eligible is not None else None,
    }
    stored = {device.id: device.as_config() for device in coordinator.partner.devices()}
    diagnostics["devices"] = _redact([{**row, **stored.get(row["id"], {})} for row in snapshot.devices])
    diagnostics["coordinator"] = {
        "last_update_success": coordinator.last_update_success,
        "last_exception": repr(coordinator.last_exception) if coordinator.last_exception else None,
    }
    return diagnostics
