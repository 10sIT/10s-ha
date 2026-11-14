"""HTTP view serving the 72-hour device archive to the 10s panel as per-slot energy.

Energies come from the kit's ``slot_energies``, the function the upload to 10s uses, so the chart shows what
10s receives. The slot in progress is not drawn. Beside each energy rides how 10s accounts the slot, from the
kit's ``slot_accounting``. A view rather than a state attribute: the payload (~288 slots per device) would
otherwise be rewritten into the recorder on every coordinator cycle.
"""
from __future__ import annotations

from collections.abc import Iterable
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from aiohttp import web
from homeassistant.components.http import HomeAssistantView

from ._tenskit.orchestration import PartnerRuntime
from ._tenskit.outbound.partners import SlotEnergy, slot_energies
from .const import DOMAIN

#: One slot on the wire: ``[start_ms, consumed_kwh, returned_kwh, accounting]``; an energy is null where it
#: cannot be measured; ``accounting`` is ``"accounted"``, ``"owed"`` or ``"outside"``.
EnergyRow = list[int | float | str | None]


class TensDeviceSlotsView(HomeAssistantView):
    """Auth-gated HTTP endpoint serving the loaded entry's 72-hour device archive."""

    url = "/api/tens/device_slots"
    name = "api:tens:device_slots"
    requires_auth = True

    async def get(self, request: web.Request) -> web.Response:
        """Return ``{"generated_at", "devices"}``; ``devices`` is empty while no entry is loaded."""
        coordinator = request.app["hass"].data.get(DOMAIN)
        now = datetime.now(UTC)
        return self.json({
            "generated_at": now.isoformat(timespec="seconds"),
            "devices": {} if coordinator is None else _device_energies(coordinator, now),
        })


def _device_energies(coordinator: Any, now: datetime) -> dict[str, list[EnergyRow]]:
    """Return each linked device's id mapped to its slots over the 72 hours before ``now``.

    Every linked device is keyed even with an empty list, so the panel can tell "no slot closed yet" from
    "not a device of this installation".
    """
    partner: PartnerRuntime = coordinator.partner
    return {
        device.id: _energy_rows(partner, device.id, slot_energies(partner.device_archive(device.id, now)))
        for device in partner.devices()
    }


def _energy_rows(partner: PartnerRuntime, device_id: str, energies: Iterable[SlotEnergy]) -> list[EnergyRow]:
    """Return one device's slot energies as ``[start_ms, consumed, returned, accounting]`` rows.

    Arrays rather than objects: up to 288 rows per device, and the keys would be three quarters of the payload.
    """
    return [
        [
            int(energy.start.timestamp() * 1000),
            _as_json_kwh(energy.consumed_kwh),
            _as_json_kwh(energy.returned_kwh),
            partner.slot_accounting(device_id, energy.start),
        ]
        for energy in energies
    ]


def _as_json_kwh(energy_kwh: Decimal | None) -> float | None:
    """Return a 3-decimal energy as a JSON number (its float prints as the same decimal), or None."""
    return None if energy_kwh is None else float(energy_kwh)
