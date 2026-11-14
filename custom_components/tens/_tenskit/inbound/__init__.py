"""Provide the Home Assistant edge of the kit: strict energy-counter reads and meter detection."""

from __future__ import annotations

from .device_energy import read_device_energy
from .energy import read_energy_kwh
from .metered_device_resolver import EXCLUDED_PLATFORMS, MeterScan, device_meters, device_name, pre_pick
from .registration_dump import DumpSeries, RegistrationDump, async_build_dump
from .statistics import RecorderReadError

__all__ = [
    "EXCLUDED_PLATFORMS",
    "DumpSeries",
    "MeterScan",
    "RecorderReadError",
    "RegistrationDump",
    "async_build_dump",
    "device_meters",
    "device_name",
    "pre_pick",
    "read_device_energy",
    "read_energy_kwh",
]
