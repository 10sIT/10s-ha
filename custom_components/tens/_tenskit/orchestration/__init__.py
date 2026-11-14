"""Provide the partner runtime, the registration mirror, the slot archive and the ``Store`` seam.

``registrations`` and ``slot_log`` are pure rules and codecs, ``partner_runtime`` drives them against the account and
the partner client, and a host's persistence plugs in through the structural ``Store`` protocol.
"""

from __future__ import annotations

from .ha_store import Store
from .partner_runtime import PartnerRuntime, PartnerSnapshot
from .registrations import Registrations, deserialize_registrations, serialize_registrations
from .slot_log import SlotLog, deserialize_slot_log, serialize_slot_log

__all__ = [
    "PartnerRuntime",
    "PartnerSnapshot",
    "Registrations",
    "SlotLog",
    "Store",
    "deserialize_registrations",
    "deserialize_slot_log",
    "serialize_registrations",
    "serialize_slot_log",
]
