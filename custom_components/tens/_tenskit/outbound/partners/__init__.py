"""Provide the partner client protocol and its errors, the 10s client and the measurement-block encoder.

``base`` is the protocol, ``measurements`` the pure encoder to 10s blocks, ``tens`` the real 10s client and its
environments.
"""

from __future__ import annotations

from .base import PartnerAuthError, PartnerClient, PartnerConnectionError, PartnerRejectedError
from .measurements import (
    MAX_BLOCK_VALUES,
    METRIC_CONSUMPTION,
    METRIC_PRODUCTION,
    SLOT_GRANULARITY,
    MeasurementBlock,
    SlotEnergy,
    encode_device,
    series_blocks,
    slot_energies,
)
from .tens import TENS_DEV, TENS_ENVIRONMENTS, TENS_PROD, TensClient, TensEnvironment

__all__ = [
    "MAX_BLOCK_VALUES",
    "METRIC_CONSUMPTION",
    "METRIC_PRODUCTION",
    "SLOT_GRANULARITY",
    "TENS_DEV",
    "TENS_ENVIRONMENTS",
    "TENS_PROD",
    "MeasurementBlock",
    "PartnerAuthError",
    "PartnerClient",
    "PartnerConnectionError",
    "PartnerRejectedError",
    "SlotEnergy",
    "TensClient",
    "TensEnvironment",
    "encode_device",
    "series_blocks",
    "slot_energies",
]
