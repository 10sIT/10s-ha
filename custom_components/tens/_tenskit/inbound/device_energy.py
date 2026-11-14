"""Read a metered device's counters through the energy reader and translate them with its kind's profile."""
from __future__ import annotations

from typing import Any

from ..contract import DeviceEnergy, MeteredDevice
from .energy import read_energy_kwh


def read_device_energy(hass: Any, device: MeteredDevice) -> DeviceEnergy:
    """Return a device's current energy; a role with no readable counter translates to None."""
    kwh = {entity: read_energy_kwh(hass, entity) for entity in device.meter_entities}
    return device.profile.translate(device.meters, kwh)

