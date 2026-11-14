"""The real 10s client: Keycloak token grant, app registration and the device endpoints.

API: https://api.dev.10senergy.com/v3/api-docs/external. Uses the host's shared ``aiohttp`` session and never opens or
closes one. Tokens come from the OAuth 2 client-credentials grant (RFC 6749 §4.4) and are cached in memory only.
Errors map by status alone, as 10s's error bodies carry no contract: unreachable, timeout, ``5xx``, ``429`` or a
non-JSON answer is :class:`PartnerConnectionError`; a refused grant or a ``401`` a fresh token does not cure is
:class:`PartnerAuthError`; any other ``4xx`` is :class:`PartnerRejectedError`. A client is bound to one
:class:`TensEnvironment` and stamps its name on every account it redeems, since a registration token and the credentials
it yields work only in the environment that issued them. The kit reads no environment variable; the host picks the
environment.
"""
from __future__ import annotations

import asyncio
import json as jsonlib
import logging
from collections.abc import Callable, Collection, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

import aiohttp

from ...contract import (
    TENS_DEVICE_CATEGORIES,
    TENS_ENVIRONMENT_DEV,
    TENS_ENVIRONMENT_PRODUCTION,
    MeteredDevice,
    PartnerAccount,
    RegistrationState,
    parse_instant,
    status_from_window,
)
from .base import PartnerAuthError, PartnerConnectionError, PartnerRejectedError

if TYPE_CHECKING:
    from aiohttp import ClientSession

    from .measurements import MeasurementBlock

_LOGGER = logging.getLogger(__name__)

API_PREFIX = "/api/v1"

# Whole request, connect to last byte; generous for an upload of 20 000 values, the most a request carries.
REQUEST_TIMEOUT = timedelta(seconds=30)
# Re-grant this long before the stated expiry, so a token never expires between the cache and the server.
TOKEN_EXPIRY_MARGIN = timedelta(seconds=30)
_TRANSIENT_STATUSES = frozenset({429})
# A deregister refused with one of these on a remembered id means the id is stale.
_STALE_ID_STATUSES = frozenset({403, 404})


@dataclass(frozen=True)
class TensEnvironment:
    """One 10s deployment: where its API and its Keycloak live.

    Attributes:
        name: What the environment is called in logs, diagnostics and the panel.
        api_url: Where the 10s API lives; ``/api/v1`` is appended.
        token_url: The Keycloak client-credentials token endpoint.
    """

    name: str
    api_url: str
    token_url: str


# 10s's dev environment: ``servers`` and ``securitySchemes.bearerAuth`` of the live spec
# (https://api.dev.10senergy.com/v3/api-docs/external, read 2026-10-20). Its certificates are issued by Let's Encrypt,
# publicly trusted, and verified like any other.
TENS_DEV = TensEnvironment(
    name=TENS_ENVIRONMENT_DEV,
    api_url="https://api.dev.10senergy.com",
    token_url="https://auth.dev.10senergy.com/realms/10senergy-apps/protocol/openid-connect/token",
)

# 10s's production environment: the same API and token paths as dev on the production hosts, as 10s gave them
# (2026-11-10). Its certificates are issued by Let's Encrypt and verified.
TENS_PROD = TensEnvironment(
    name=TENS_ENVIRONMENT_PRODUCTION,
    api_url="https://api.10senergy.com",
    token_url="https://auth.10senergy.com/realms/10senergy-apps/protocol/openid-connect/token",
)

# Every environment by its name, the name a redeemed account stores; production first, the default.
TENS_ENVIRONMENTS: Mapping[str, TensEnvironment] = {env.name: env for env in (TENS_PROD, TENS_DEV)}


def _utcnow() -> datetime:
    """Return the current UTC instant."""
    return datetime.now(UTC)


@dataclass(frozen=True)
class _Token:
    """A cached access token.

    Attributes:
        value: The bearer token.
        expires_at: When it should no longer be used: the server's expiry less the margin.
    """

    value: str
    expires_at: datetime


@dataclass(frozen=True)
class _DeviceRow:
    """One row of ``GET /devices`` (a ``DeviceResponse``), as far as the client reads it.

    Attributes:
        remote_id: 10s's id for the device.
        external_id: Our device uuid as sent at registration; None when absent.
        effective_from: When the registration takes effect, or None.
        effective_to: When the deregistration takes effect, or None.
    """

    remote_id: int
    external_id: str | None
    effective_from: datetime | None
    effective_to: datetime | None

    @classmethod
    def parse(cls, body: Any) -> _DeviceRow | None:
        """Return the row a ``DeviceResponse`` describes, or None when it has no integer ``id``."""
        if not isinstance(body, Mapping):
            return None
        remote_id = body.get("id")
        if not isinstance(remote_id, int) or isinstance(remote_id, bool):
            return None
        external_id = body.get("externalId")
        return cls(
            remote_id=remote_id,
            external_id=external_id if isinstance(external_id, str) else None,
            effective_from=parse_instant(body.get("effectiveFrom")),
            effective_to=parse_instant(body.get("effectiveTo")),
        )

    def state(self, now: datetime) -> RegistrationState:
        """Return the device's state at ``now``, read off the row's window."""
        return status_from_window(self.effective_from, self.effective_to, now, self.remote_id)


class TensClient:
    """The 10s client: redeems registration tokens and makes authenticated calls."""

    def __init__(
        self,
        session: ClientSession,
        environment: TensEnvironment,
        *,
        clock: Callable[[], datetime] = _utcnow,
    ) -> None:
        """Bind the client to the host's session and one 10s environment.

        Args:
            session: The host's shared ``aiohttp`` session.
            environment: The 10s deployment every request goes to; its name is stamped on redeemed accounts.
            clock: Returns the current UTC instant; tests step it.
        """
        self._session = session
        self._environment = environment
        self._api = environment.api_url.rstrip("/") + API_PREFIX
        self._token_url = environment.token_url
        self._timeout = aiohttp.ClientTimeout(total=REQUEST_TIMEOUT.total_seconds())
        self._clock = clock
        self._tokens: dict[str, _Token] = {}
        # In memory only: after a restart the first ``async_statuses`` learns them again from ``externalId``.
        self._remote_ids: dict[tuple[str, str], int] = {}
        # Concurrent calls on a cold cache share the first call's grant instead of each asking Keycloak.
        self._grant_lock = asyncio.Lock()

    async def _send(
        self, method: str, url: str, **kwargs: Any,
    ) -> tuple[int, Any]:
        """Send one request and read its JSON answer.

        Args:
            **kwargs: ``json`` / ``data`` / ``headers`` for the request.

        Returns:
            ``(status, decoded body)``; the body is None when empty or not JSON, such as an error page.

        Raises:
            PartnerConnectionError: The server could not be reached or timed out.
        """
        try:
            async with self._session.request(method, url, timeout=self._timeout, **kwargs) as response:
                text = await response.text()
                status = response.status
        except (aiohttp.ClientError, TimeoutError) as err:
            raise PartnerConnectionError(f"10s unreachable: {err or type(err).__name__}") from err
        return status, _decode(text)

    async def async_request(
        self,
        account: PartnerAccount,
        method: str,
        path: str,
        *,
        json: Any = None,
        accept: Collection[int] = (),
    ) -> tuple[int, Any]:
        """Make a bearer-authenticated call to the 10s API.

        A ``401`` is answered once by granting a fresh token and sending again.

        Args:
            path: The path under ``/api/v1``, starting with ``/``.
            accept: Non-2xx statuses the caller handles itself, such as a ``409`` carrying the existing row; returned,
                not raised.

        Returns:
            ``(status, decoded body)`` for a 2xx or an accepted status.

        Raises:
            PartnerAuthError: The credentials no longer grant a working token.
            PartnerRejectedError: Any other refusal (``4xx``).
            PartnerConnectionError: Unreachable, timed out, ``5xx`` or ``429``.
        """
        url = self._api + path
        for attempt in range(2):
            token = await self._async_token(account, fresh=attempt > 0)
            status, body = await self._send(
                method, url, json=json, headers={"Authorization": f"Bearer {token}"},
            )
            if status != 401:
                break
            self._tokens.pop(account.client_id, None)
        else:
            raise PartnerAuthError("10s refused a freshly granted token")
        if 200 <= status < 300 or status in accept:
            return status, body
        raise _refusal(status, body)

    async def _async_token(self, account: PartnerAccount, *, fresh: bool = False) -> str:
        """Return an access token for the account, granting one when none is usable.

        Args:
            fresh: Ignore a cached token because the server just refused it.

        Raises:
            PartnerAuthError: Keycloak refused the client credentials.
            PartnerConnectionError: Keycloak could not be reached or answered badly.
        """
        async with self._grant_lock:
            cached = self._tokens.get(account.client_id)
            if cached is not None and not fresh and cached.expires_at > self._clock():
                return cached.value
            token = await self._async_grant(account)
            self._tokens[account.client_id] = token
            return token.value

    async def _async_grant(self, account: PartnerAccount) -> _Token:
        """Ask Keycloak for a token by the client-credentials grant, with the credentials in the form body.

        Raises:
            PartnerAuthError: ``400`` / ``401``: unknown client, wrong secret, or the grant disabled for it.
            PartnerConnectionError: Unreachable, ``5xx``, or an answer without a token.
        """
        issued_at = self._clock()
        status, body = await self._send(
            "POST", self._token_url,
            data={
                "grant_type": "client_credentials",
                "client_id": account.client_id,
                "client_secret": account.client_secret,
            },
        )
        if status in (400, 401):
            raise PartnerAuthError(f"10s refused the client credentials (HTTP {status})")
        if status != 200:
            raise _refusal(status, body)
        if not isinstance(body, Mapping) or not isinstance(body.get("access_token"), str):
            raise PartnerConnectionError("10s token endpoint answered without an access token")
        lifetime = body.get("expires_in")
        lifetime = timedelta(seconds=lifetime) if isinstance(lifetime, int | float) else timedelta(0)
        # A token shorter-lived than the margin is still used once, for the call that asked for it.
        return _Token(body["access_token"], issued_at + max(lifetime - TOKEN_EXPIRY_MARGIN, timedelta(0)))

    async def async_redeem(self, token: str, app_instance_id: str) -> PartnerAccount:
        """Redeem a registration token: ``POST /app-registrations``, unauthenticated.

        The answer carries the Keycloak client secret, shown this once. 10s derives the contract from the token, so no
        ``clientRef`` is sent. The account names this client's environment, the only one its credentials work in. The
        answer's ``contractId`` and ``clientRef`` are kept when usable and read as "" when not: once the credentials
        have arrived, only a missing ``id`` fails the redemption, since the secret would be lost.

        Raises:
            PartnerAuthError: ``400``: the token is expired, already redeemed, or unknown.
            PartnerRejectedError: ``409`` (contract not active, or an app already registered for this contract and
                ``app_instance_id``) or any other refusal.
            PartnerConnectionError: Unreachable, ``5xx``, or a ``201`` missing the credentials.
        """
        status, body = await self._send(
            "POST", self._api + "/app-registrations",
            json={"registrationToken": token, "appInstanceId": app_instance_id},
        )
        if status == 400:
            raise PartnerAuthError("10s refused the registration token")
        if not 200 <= status < 300:
            raise _refusal(status, body)
        fields = body if isinstance(body, Mapping) else {}
        app_id, client_id, secret = (fields.get(k) for k in ("id", "keycloakClientId", "keycloakClientSecret"))
        if app_id is None or not isinstance(client_id, str) or not isinstance(secret, str) or not secret:
            raise PartnerConnectionError("10s redeemed the token but sent no credentials")
        contract_id, contract_ref = fields.get("contractId"), fields.get("clientRef")
        return PartnerAccount(
            account_id=str(app_id),
            client_id=client_id,
            client_secret=secret,
            contract_id=str(contract_id) if isinstance(contract_id, int) and not isinstance(contract_id, bool) else "",
            contract_ref=contract_ref if isinstance(contract_ref, str) else "",
            app_instance_id=str(fields.get("appInstanceId") or app_instance_id),
            logged_in_at=self._clock().isoformat(timespec="seconds"),
            environment=self._environment.name,
        )

    async def async_push_measurements(
        self, account: PartnerAccount, remote_id: int, blocks: Sequence[MeasurementBlock],
    ) -> int | None:
        """Deliver measurement blocks for one device in one request: ``POST /devices/{id}/measurements``.

        Returns:
            ``datapointsWritten`` from the ``202``, None when the answer does not carry it.

        Raises:
            PartnerAuthError: The credentials no longer grant a working token.
            PartnerRejectedError: ``400`` a malformed block, ``404`` / ``403`` a device 10s does not hold under this
                account.
            PartnerConnectionError: Unreachable, timed out, ``5xx`` or ``429``.
        """
        _, body = await self.async_request(
            account, "POST", f"/devices/{remote_id}/measurements",
            json=[block.as_payload() for block in blocks],
        )
        written = body.get("datapointsWritten") if isinstance(body, Mapping) else None
        return written if isinstance(written, int) and not isinstance(written, bool) else None

    async def async_eligible_categories(self, account: PartnerAccount) -> Mapping[str, str]:
        """Return what this account may register: ``GET /devices/eligible-categories``.

        Entries that are not ``{category, label}`` objects, and categories outside ``TENS_DEVICE_CATEGORIES``, are
        skipped. A missing label reads as the category itself.

        Returns:
            Category to 10s's label, in 10s's order.

        Raises:
            PartnerAuthError: The credentials no longer grant a working token.
            PartnerRejectedError: 10s refused the request.
            PartnerConnectionError: Unreachable, timed out, ``5xx`` / ``429``, or an answer that is not a list.
        """
        _, body = await self.async_request(account, "GET", "/devices/eligible-categories")
        if not isinstance(body, list):
            raise PartnerConnectionError("10s listed the eligible categories as something other than a list")
        eligible: dict[str, str] = {}
        for item in body:
            if not isinstance(item, Mapping) or not isinstance(item.get("category"), str):
                continue
            category = item["category"]
            if category not in TENS_DEVICE_CATEGORIES:
                _LOGGER.debug("10s offers category %r, which has no device kind here", category)
                continue
            label = item.get("label")
            eligible[category] = label if isinstance(label, str) and label else category
        return eligible

    async def async_register(self, account: PartnerAccount, device: MeteredDevice) -> RegistrationState:
        """Register a device with 10s: ``POST /devices/register``, the uuid going as ``externalId``.

        A ``409`` means 10s already holds a live row for it, a register whose answer was lost, and is a success: that
        row is returned.

        Returns:
            The state read off the row's window, with 10s's id as ``remote_id``.

        Raises:
            PartnerAuthError: The credentials no longer grant a working token.
            PartnerRejectedError: ``400`` an unknown category; an ineligible one is ``400`` only in the test fake, 10s's
                answer is unknown. Also a ``409`` whose row cannot be found, or any other refusal.
            PartnerConnectionError: Unreachable, timed out, ``5xx`` / ``429``, or a ``201`` without a device row.
        """
        status, body = await self.async_request(
            account, "POST", "/devices/register",
            json={"category": device.category, "appDeviceName": device.name, "externalId": device.id},
            accept=(409,),
        )
        row = _DeviceRow.parse(body)
        if status == 409 and (row is None or row.external_id != device.id):
            row = _pick(await self._async_rows(account), device.id, None)
            if row is None:
                raise PartnerRejectedError(409, "10s holds the device already but does not list it")
        if row is None:
            raise PartnerConnectionError("10s registered the device but sent no device row")
        self._remote_ids[(account.account_id, device.id)] = row.remote_id
        return row.state(self._clock())

    async def async_withdraw(self, account: PartnerAccount, device_id: str) -> RegistrationState:
        """Deregister a device from 10s: ``POST /devices/{id}/deregister``.

        10s sets ``effectiveTo`` two days out. A ``409`` (already deregistered) is a success carrying the existing
        ``effectiveTo``. A ``404`` / ``403`` on the remembered id is a stale mapping: the list is read again and the
        call repeated once on the id it gives; so is a ``409`` on a remembered id when the list shows the device
        registered again as a new row.

        Args:
            device_id: The device's uuid.

        Returns:
            The state read off the new window: ``withdrawing`` until ``effectiveTo``, ``withdrawn`` once it has passed.

        Raises:
            ValueError: 10s does not list the device under this account.
            PartnerAuthError: The credentials no longer grant a working token.
            PartnerRejectedError: The deregister was refused, after a resync where the refusal said the id was stale.
            PartnerConnectionError: Unreachable, timed out, ``5xx`` / ``429``, or an answer without ``effectiveTo`` for
                a device 10s then does not list.
        """
        key = (account.account_id, device_id)
        remote_id = self._remote_ids.get(key)
        resynced = remote_id is None
        if remote_id is None:
            remote_id = await self._async_resolve(account, device_id)
        try:
            status, body = await self._async_deregister(account, remote_id)
        except PartnerRejectedError as err:
            if resynced or err.status not in _STALE_ID_STATUSES:
                raise
            stale, remote_id = remote_id, await self._async_resolve(account, device_id)
            if remote_id == stale:
                raise
            _, body = await self._async_deregister(account, remote_id)
        else:
            if status == 409 and not resynced:
                # Already deregistered, or an old row the device has since been registered past.
                current = await self._async_resolve(account, device_id)
                if current != remote_id:
                    remote_id = current
                    _, body = await self._async_deregister(account, remote_id)
        self._remote_ids[key] = remote_id
        effective_to = parse_instant(body.get("effectiveTo")) if isinstance(body, Mapping) else None
        if effective_to is None:
            row = _pick(await self._async_rows(account), device_id, remote_id)
            if row is None:
                raise PartnerConnectionError("10s deregistered the device but no longer lists it")
            return row.state(self._clock())
        # A ``409`` body says only when the window ends; the next listing fills ``effectiveFrom`` in.
        return status_from_window(parse_instant(body.get("effectiveFrom")), effective_to, self._clock(), remote_id)

    async def async_statuses(
        self, account: PartnerAccount, device_ids: Collection[str],
    ) -> Mapping[str, RegistrationState]:
        """Return 10s's state for each device it lists, by device uuid: one ``GET /devices``.

        Devices 10s does not list are left out.

        Raises:
            PartnerAuthError: The credentials no longer grant a working token.
            PartnerRejectedError: 10s refused the listing.
            PartnerConnectionError: Unreachable, timed out, ``5xx`` / ``429``, or an answer that is not a list.
        """
        rows = await self._async_rows(account)
        now = self._clock()
        states: dict[str, RegistrationState] = {}
        for device_id in device_ids:
            key = (account.account_id, device_id)
            row = _pick(rows, device_id, self._remote_ids.get(key))
            if row is None:
                self._remote_ids.pop(key, None)
                continue
            self._remote_ids[key] = row.remote_id
            states[device_id] = row.state(now)
        return states

    async def _async_rows(self, account: PartnerAccount) -> list[_DeviceRow]:
        """Return every device row 10s lists for the account, deregistered ones included.

        Raises:
            PartnerConnectionError: The answer is not a list, or the call failed.
            PartnerAuthError: As :meth:`async_request`.
            PartnerRejectedError: As :meth:`async_request`.
        """
        _, body = await self.async_request(account, "GET", "/devices")
        if not isinstance(body, list):
            raise PartnerConnectionError("10s listed the devices as something other than a list")
        return [row for item in body if (row := _DeviceRow.parse(item)) is not None]

    async def _async_resolve(self, account: PartnerAccount, device_id: str) -> int:
        """Return 10s's current id for a device, read from the list.

        Raises:
            ValueError: 10s does not list the device.
        """
        row = _pick(await self._async_rows(account), device_id, None)
        if row is None:
            self._remote_ids.pop((account.account_id, device_id), None)
            raise ValueError(f"device {device_id!r} is not registered with 10s")
        return row.remote_id

    async def _async_deregister(self, account: PartnerAccount, remote_id: int) -> tuple[int, Any]:
        """Send one deregister; a ``409`` (already deregistered) is returned, not raised."""
        return await self.async_request(account, "POST", f"/devices/{remote_id}/deregister", accept=(409,))


def _pick(rows: Sequence[_DeviceRow], device_id: str, remote_id: int | None) -> _DeviceRow | None:
    """Return the row that stands for a device, or None when 10s lists none.

    Args:
        device_id: The device's uuid, its ``externalId``.
        remote_id: The id last seen for it, matched only when no row carries the ``externalId``.

    Returns:
        Among the rows with the ``externalId`` (else the one with the remembered id), the latest by ``effectiveFrom``,
        then by id: a device deregistered and registered again is a new row, the old one still listed.
    """
    matches = [row for row in rows if row.external_id == device_id]
    if not matches and remote_id is not None:
        matches = [row for row in rows if row.remote_id == remote_id]
    if not matches:
        return None
    floor = datetime.min.replace(tzinfo=UTC)
    return max(matches, key=lambda row: (row.effective_from or floor, row.remote_id))


def _decode(text: str) -> Any:
    """Return a body decoded as JSON, or None when it is empty or not JSON."""
    if not text:
        return None
    try:
        return jsonlib.loads(text)
    except ValueError:
        return None


def _refusal(status: int, body: Any) -> Exception:
    """Map a status the caller did not accept to the partner error it means, by status only.

    Args:
        body: The decoded body; its ``error`` / ``message`` text is kept for the log and the service error, never
            interpreted.

    Returns:
        :class:`PartnerConnectionError` for ``5xx`` / ``429`` and anything outside 4xx, :class:`PartnerAuthError` for
        ``401``, else :class:`PartnerRejectedError`.
    """
    detail = ""
    if isinstance(body, Mapping):
        detail = str(body.get("message") or body.get("error") or "")
    suffix = f": {detail}" if detail else ""
    if status >= 500 or status in _TRANSIENT_STATUSES or status < 400:
        return PartnerConnectionError(f"10s answered HTTP {status}{suffix}")
    if status == 401:
        return PartnerAuthError(f"10s refused the credentials (HTTP 401){suffix}")
    _LOGGER.debug("10s refused a request: HTTP %s%s", status, suffix)
    return PartnerRejectedError(status, detail)
