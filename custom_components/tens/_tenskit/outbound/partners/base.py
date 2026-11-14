"""Define what the integration needs from a partner's server: the client protocol and its errors.

A partner is an outside service metered devices are registered with; today only 10s. Nothing below imports a concrete
partner.
"""
from __future__ import annotations

from collections.abc import Collection, Mapping, Sequence
from typing import TYPE_CHECKING, Protocol, runtime_checkable

from ...contract import MeteredDevice, PartnerAccount, RegistrationState

if TYPE_CHECKING:
    from .measurements import MeasurementBlock


class PartnerAuthError(Exception):
    """The partner's server rejected the credentials."""


class PartnerConnectionError(Exception):
    """The partner's server could not be reached."""


class PartnerRejectedError(Exception):
    """The partner's server answered and refused the request.

    Neither an auth failure nor an unreachable server: the request itself was wrong, e.g. a duplicate, an unknown device
    or a category it does not take.

    Attributes:
        status: The HTTP status the server answered with.
    """

    def __init__(self, status: int, message: str = "") -> None:
        """Record the status the server refused with, and what it said if anything."""
        super().__init__(message or f"partner refused the request (HTTP {status})")
        self.status = status


@runtime_checkable
class PartnerClient(Protocol):
    """How the integration talks to a partner's server."""

    async def async_redeem(self, token: str, app_instance_id: str) -> PartnerAccount:
        """Redeem a registration token for this installation's credentials.

        The token alone names the contract. The client id and secret come back once: the caller must persist the
        returned account first, since a second redemption for the same contract and ``app_instance_id`` is refused.

        Raises:
            PartnerAuthError: The token is expired, already redeemed, or unknown.
            PartnerRejectedError: ``409`` when the token's contract is not active or an app is already registered for
                this contract and ``app_instance_id``; the status does not tell the two apart.
            PartnerConnectionError: The server could not be reached.
        """
        ...

    async def async_register(
        self, account: PartnerAccount, device: MeteredDevice,
    ) -> RegistrationState:
        """Register a metered device with the partner.

        The device carries no history; that is delivered as consumption data. Its uuid is sent as the external id and
        the partner's own id comes back as ``remote_id``.

        Returns:
            The state derived from the returned effective window: ``registering`` until ``effectiveFrom``, else
                ``registered``.

        Raises:
            PartnerRejectedError: The server refused, among others a category it does not take (``400``).
            PartnerConnectionError: The server could not be reached.
        """
        ...

    async def async_withdraw(
        self, account: PartnerAccount, device_id: str,
    ) -> RegistrationState:
        """Ask the partner to withdraw a registered device.

        The partner sets ``effectiveTo`` two days out. A device it has already deregistered (``409``) is a success: the
        current state is returned.

        Returns:
            The state derived from the effective window, normally ``withdrawing`` with ``pending_until`` =
                ``effectiveTo``.

        Raises:
            ValueError: The device is not in a state it can be withdrawn from.
            PartnerRejectedError: The server refused the withdrawal.
            PartnerConnectionError: The server could not be reached.
        """
        ...

    async def async_statuses(
        self, account: PartnerAccount, device_ids: Collection[str],
    ) -> Mapping[str, RegistrationState]:
        """Return the partner's current state for each device it knows, by device id.

        This is the authority: it overwrites the local mirror, including a transition whose window edge has passed since
        the last read. Ids the partner does not know are left out.

        Raises:
            PartnerConnectionError: The server could not be reached.
        """
        ...

    async def async_eligible_categories(self, account: PartnerAccount) -> Mapping[str, str]:
        """Return the device categories the partner lets this account register.

        The partner decides per app registration, so this is asked, not configured. Categories without a counterpart on
        either side are left out.

        Returns:
            Our category key to the partner's label for it; possibly empty.

        Raises:
            PartnerAuthError: The credentials no longer work.
            PartnerRejectedError: The partner refused the request.
            PartnerConnectionError: The partner could not be reached or answered with something other than a list.
        """
        ...

    async def async_push_measurements(
        self, account: PartnerAccount, remote_id: int, blocks: Sequence[MeasurementBlock],
    ) -> int | None:
        """Deliver measurement blocks for one registered device, in one request.

        Acceptance is all or nothing: the partner validates every block before storing any, so a refusal means none
        landed and the caller must not move its watermark.

        Args:
            remote_id: The partner's id for the device (``RegistrationState.remote_id``).

        Returns:
            How many values the partner says it wrote (``datapointsWritten``), None when it did not say.

        Raises:
            PartnerAuthError: The credentials no longer work.
            PartnerRejectedError: ``400`` a malformed block (an encoder bug), ``404`` / ``403`` a device it does not
                hold under this account.
            PartnerConnectionError: The partner could not be reached.
        """
        ...
