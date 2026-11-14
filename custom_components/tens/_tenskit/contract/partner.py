"""Partner account and device registration value types; the partner today is 10s.

The short-lived access token is never part of the account: it lives in memory and is re-obtained from
``client_id`` / ``client_secret``.
"""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from .const import (
    TENS_ENVIRONMENT_NAMES,
    TENS_STATUS_REGISTERED,
    TENS_STATUS_REGISTERING,
    TENS_STATUS_WITHDRAWING,
    TENS_STATUS_WITHDRAWN,
)

# A device with no known status was never registered, so it reads as not held: registration is a manual, panel-driven
# action and nothing is held until it is issued.
DEFAULT_STATUS = TENS_STATUS_WITHDRAWN

# The stored account keys without which there are no usable credentials.
_REQUIRED_KEYS = ("account_id", "client_id", "environment")


def is_held(status: str) -> bool:
    """Return True when the partner still holds a device in this status, i.e. anything but ``withdrawn``.

    Args:
        status: One of ``TENS_DEVICE_STATUSES``, or anything at all.
    """
    return status != TENS_STATUS_WITHDRAWN


@dataclass(frozen=True)
class PartnerAccount:
    """A logged-in partner account, as stored in the config entry.

    Attributes:
        account_id: 10s's app-registration ``id``, as a string.
        client_id: OAuth client id issued when the registration token was redeemed.
        client_secret: OAuth client secret, shown once by the partner; excluded from ``repr`` so it stays out of logs.
        contract_id: The redemption's ``contractId`` as a string, "" when 10s sent none.
        contract_ref: The contract's ``externalRef``, which 10s returns as the deprecated ``clientRef``; "" when absent.
        app_instance_id: Stable id of this Home Assistant installation as sent to the partner.
        logged_in_at: ISO-8601 UTC timestamp of the redemption.
        environment: The name of the 10s environment that issued the credentials, the only one they work in.
    """

    account_id: str
    client_id: str
    client_secret: str = field(repr=False)
    contract_id: str
    contract_ref: str
    app_instance_id: str
    logged_in_at: str
    environment: str

    @property
    def label(self) -> str:
        """Return how the setup flow, the panel and the reauth form name the account.

        The contract reference, else the contract id, else the app registration id: ``clientRef`` is deprecated, so the
        label degrades instead of breaking when it goes.
        """
        if self.contract_ref:
            return self.contract_ref
        if self.contract_id:
            return f"contract {self.contract_id}"
        return f"app registration {self.account_id}"

    def as_config(self) -> dict[str, str]:
        """Return the account as the JSON-safe dict stored in the config entry."""
        return {
            "account_id": self.account_id,
            "client_id": self.client_id,
            "client_secret": self.client_secret,
            "contract_id": self.contract_id,
            "contract_ref": self.contract_ref,
            "app_instance_id": self.app_instance_id,
            "logged_in_at": self.logged_in_at,
            "environment": self.environment,
        }

    @classmethod
    def from_config(cls, data: Any) -> PartnerAccount | None:
        """Rebuild an account from its stored dict, or None when nothing usable is stored.

        Args:
            data: The value stored under ``CONF_TENS_ACCOUNT``, possibly absent. A dict without ``account_id`` or
                ``client_id``, or whose ``environment`` is not one of ``TENS_ENVIRONMENT_NAMES``, reads as not logged
                in: credentials of an unknown environment cannot be used anywhere.
        """
        if not isinstance(data, Mapping) or not all(data.get(key) for key in _REQUIRED_KEYS):
            return None
        if data["environment"] not in TENS_ENVIRONMENT_NAMES:
            return None
        return cls(
            account_id=str(data["account_id"]),
            client_id=str(data["client_id"]),
            client_secret=str(data.get("client_secret", "")),
            contract_id=str(data.get("contract_id", "")),
            contract_ref=str(data.get("contract_ref", "")),
            app_instance_id=str(data.get("app_instance_id", "")),
            logged_in_at=str(data.get("logged_in_at", "")),
            environment=str(data["environment"]),
        )


@dataclass(frozen=True)
class RegistrationState:
    """One device's registration with a partner, as last seen here (a local mirror).

    The partner reports the effective window and the status is derived from it by :func:`status_from_window`.

    Attributes:
        status: One of ``TENS_DEVICE_STATUSES``.
        pending_until: When a pending status (``registering`` / ``withdrawing``) is expected to land, None once
            terminal.
        remote_id: The partner's id for the device, returned by the register call; None for a device never registered.
            Our device uuid travels the other way, as 10s's ``externalId``.
        effective_to: The partner's ``effectiveTo``, kept once ``withdrawn`` so the slots still owed at the flip are
            sent; None while not deregistered or not known.
        effective_from: The partner's ``effectiveFrom``, which tells whether a slot lies inside the window the partner
            accounts; None while not known, filled by the next listing.
    """

    status: str = DEFAULT_STATUS
    pending_until: datetime | None = None
    remote_id: int | None = None
    effective_to: datetime | None = None
    effective_from: datetime | None = None


def status_from_window(
    effective_from: datetime | None,
    effective_to: datetime | None,
    now: datetime,
    remote_id: int | None = None,
) -> RegistrationState:
    """Derive a device's registration state from the partner's effective window.

    10s has no pending status and no callbacks: deregistering sets ``effectiveTo`` two days out. ``effective_to`` is
    checked first, so a device deregistered before it became effective is on its way out, not in.

    - ``withdrawn``: ``effective_to`` has passed.
    - ``withdrawing`` until ``effective_to``: it is still ahead.
    - ``registering`` until ``effective_from``: no ``effective_to``, ``effective_from`` still ahead.
    - ``registered``: no ``effective_to``, ``effective_from`` passed or absent.

    Args:
        now: The instant to evaluate the window at.
        remote_id: The partner's id for the device, carried onto the result.

    Returns:
        The state at ``now``, carrying both window edges as given.
    """
    if effective_to is not None:
        if effective_to <= now:
            return RegistrationState(
                status=TENS_STATUS_WITHDRAWN, remote_id=remote_id, effective_to=effective_to,
                effective_from=effective_from,
            )
        return RegistrationState(
            status=TENS_STATUS_WITHDRAWING, pending_until=effective_to, remote_id=remote_id, effective_to=effective_to,
            effective_from=effective_from,
        )
    if effective_from is not None and effective_from > now:
        return RegistrationState(
            status=TENS_STATUS_REGISTERING, pending_until=effective_from, remote_id=remote_id,
            effective_from=effective_from,
        )
    return RegistrationState(status=TENS_STATUS_REGISTERED, remote_id=remote_id, effective_from=effective_from)
