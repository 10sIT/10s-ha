"""Keep the local mirror of what the partner has registered, with its rules and store codec.

One row per metered device, plus the categories the partner last said the account may register. The partner decides
the statuses; this module only records them. The one local rule is the removal rule: a device the partner still holds
may not be dropped (:func:`ensure_removable`). Pure Python, driven only by the partner runtime.
"""
from __future__ import annotations

import hashlib
from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import datetime
from typing import Any

from ..contract import (
    DEFAULT_STATUS,
    TENS_DEVICE_COLOR_PALETTE,
    TENS_DEVICE_STATUSES,
    MeteredDevice,
    RegistrationState,
    is_held,
    normalize_color,
    parse_instant,
)

# The first offer plus three retries: a dump refused that often is refused for its shape, not a passing condition. A new
# registration resets the count.
MAX_BACKFILL_REFUSALS = 4


@dataclass
class Registrations:
    """Per-entry registration rows keyed by device id, the persisted value.

    The helpers below mutate it in place and return whether anything changed, so the caller can skip a write.

    Attributes:
        rows: Device id to the partner's last reported state.
        eligible: Category to the partner's label for the categories it last listed as registrable; None until asked.
        backfill_owed: Device id to the partner id (``remote_id``) whose history dump has not been accepted yet. Keyed
            by the partner id because a re-registration is a new partner row; kept beside the rows because every
            reconcile overwrites a row.
        backfill_refused: Device id to how many times the partner refused the owed dump; reset on registration, and at
            :data:`MAX_BACKFILL_REFUSALS` the dump is given up.
        registered_at: Device id to the instant the current registration was first seen, where the history dump ends.
            Persisted so a dump retried after a restart still ends there.
        colors: Device id to the operator's chart colour (``#rrggbb``), display only; kept here so a recolour never
            rewrites and reloads the config entry.
    """

    rows: dict[str, RegistrationState] = field(default_factory=dict)
    eligible: dict[str, str] | None = None
    backfill_owed: dict[str, int] = field(default_factory=dict)
    backfill_refused: dict[str, int] = field(default_factory=dict)
    registered_at: dict[str, datetime] = field(default_factory=dict)
    colors: dict[str, str] = field(default_factory=dict)


def current_status(registrations: Registrations, device_id: str) -> str:
    """Return a device's status, ``withdrawn`` when it has no row.

    Registration is manual, so a linked device the partner was never asked for is free.
    """
    row = registrations.rows.get(device_id)
    return DEFAULT_STATUS if row is None else row.status


def record(registrations: Registrations, device_id: str, state: RegistrationState) -> bool:
    """Write what the partner reported for one device.

    A deregister answer carries ``effectiveTo`` alone, so a report without ``effective_from`` for the registration
    already held (same partner id) keeps the edge the last listing gave. A new partner id starts without one.

    Returns:
        True when this changed the stored row.
    """
    previous = registrations.rows.get(device_id)
    if (
        state.effective_from is None and previous is not None
        and previous.remote_id == state.remote_id and previous.effective_from is not None
    ):
        state = replace(state, effective_from=previous.effective_from)
    if previous == state:
        return False
    registrations.rows[device_id] = state
    return True


def note_registration(registrations: Registrations, device_id: str, now: datetime) -> bool:
    """Note the instant a new registration is first seen; return True when that changed the stored instant."""
    if registrations.registered_at.get(device_id) == now:
        return False
    registrations.registered_at[device_id] = now
    return True


def owe_backfill(registrations: Registrations, device_id: str, remote_id: int) -> bool:
    """Note that the registration under ``remote_id`` is owed its history dump, with no refusal counted yet.

    Returns:
        True when this changed the stored marker.
    """
    refused = registrations.backfill_refused.pop(device_id, None)
    if registrations.backfill_owed.get(device_id) == remote_id:
        return refused is not None
    registrations.backfill_owed[device_id] = remote_id
    return True


def settle_backfill(registrations: Registrations, device_id: str, remote_id: int) -> bool:
    """Clear the owed dump of ``remote_id`` once the partner accepted it.

    Returns:
        True when a marker was cleared.
    """
    if registrations.backfill_owed.get(device_id) != remote_id:
        return False
    del registrations.backfill_owed[device_id]
    registrations.backfill_refused.pop(device_id, None)
    return True


def refuse_backfill(registrations: Registrations, device_id: str, remote_id: int) -> bool:
    """Count one refusal of the dump owed under ``remote_id``.

    A refusal for a registration no longer owed a dump is not counted.

    Returns:
        True when this refusal gave the dump up.
    """
    if registrations.backfill_owed.get(device_id) != remote_id:
        return False
    registrations.backfill_refused[device_id] = registrations.backfill_refused.get(device_id, 0) + 1
    return is_backfill_given_up(registrations, device_id)


def is_backfill_given_up(registrations: Registrations, device_id: str) -> bool:
    """Return whether the partner refused the device's owed dump too often for it to be offered again."""
    return (
        device_id in registrations.backfill_owed
        and registrations.backfill_refused.get(device_id, 0) >= MAX_BACKFILL_REFUSALS
    )


def owes_backfill(registrations: Registrations, device_id: str, remote_id: int) -> bool:
    """Return whether the registration under ``remote_id`` is still owed its dump and it is still offered."""
    return registrations.backfill_owed.get(device_id) == remote_id and not is_backfill_given_up(
        registrations, device_id,
    )


def record_eligible(registrations: Registrations, eligible: Mapping[str, str]) -> bool:
    """Cache the partner's list of registrable categories; return True when that changed it.

    An empty answer is not cached over a known list: the partner never narrows every category away on purpose, so it
    reads as a glitch. With nothing cached yet it is kept.
    """
    if not eligible and registrations.eligible:
        return False
    wanted = dict(eligible)
    if registrations.eligible == wanted:
        return False
    registrations.eligible = wanted
    return True


def is_eligible(registrations: Registrations, device: MeteredDevice) -> bool | None:
    """Return whether the cached list lets this device be registered, None when the partner has not been asked yet."""
    if registrations.eligible is None:
        return None
    return device.category in registrations.eligible


def prune(registrations: Registrations, device_ids: Collection[str]) -> bool:
    """Drop everything kept for devices that are no longer linked; return True when at least one entry was dropped."""
    keep = set(device_ids)
    stale = [device_id for device_id in registrations.rows if device_id not in keep]
    for device_id in stale:
        del registrations.rows[device_id]
    dropped = [device_id for device_id in registrations.backfill_owed if device_id not in keep]
    for device_id in dropped:
        del registrations.backfill_owed[device_id]
    uncounted = [device_id for device_id in registrations.backfill_refused if device_id not in keep]
    for device_id in uncounted:
        del registrations.backfill_refused[device_id]
    unnoted = [device_id for device_id in registrations.registered_at if device_id not in keep]
    for device_id in unnoted:
        del registrations.registered_at[device_id]
    uncoloured = [device_id for device_id in registrations.colors if device_id not in keep]
    for device_id in uncoloured:
        del registrations.colors[device_id]
    return bool(stale or dropped or uncounted or unnoted or uncoloured)


def ensure_removable(registrations: Registrations, device_id: str) -> None:
    """Raise unless a device may be dropped, without changing anything.

    Split from :func:`forget` so a caller can check the rule before writing anything it would have to undo.

    Raises:
        ValueError: The partner still holds the device; a missing row counts as ``withdrawn`` and is removable.
    """
    status = current_status(registrations, device_id)
    if is_held(status):
        raise ValueError(f"device {device_id!r} is {status!r}; withdraw it before removing it")


def forget(registrations: Registrations, device_id: str) -> None:
    """Drop everything kept for a device the caller has checked with :func:`ensure_removable`."""
    registrations.rows.pop(device_id, None)
    registrations.backfill_owed.pop(device_id, None)
    registrations.backfill_refused.pop(device_id, None)
    registrations.registered_at.pop(device_id, None)
    registrations.colors.pop(device_id, None)


def device_color(device_id: str) -> str:
    """Return a stable chart colour for a device, derived from its id, from ``TENS_DEVICE_COLOR_PALETTE``.

    The fallback for a device with no operator colour; :func:`resolved_color` picks between them.
    """
    # Not security: sha1 is only a stable palette pick.
    digest = hashlib.sha1(device_id.encode()).hexdigest()
    return TENS_DEVICE_COLOR_PALETTE[int(digest, 16) % len(TENS_DEVICE_COLOR_PALETTE)]


def set_color(registrations: Registrations, device_id: str, color: str) -> None:
    """Record the operator's colour for a device, or clear it back to the palette's with "".

    Args:
        color: A normalised ``#rrggbb`` colour, or "".
    """
    if color:
        registrations.colors[device_id] = color
    else:
        registrations.colors.pop(device_id, None)


def resolved_color(registrations: Registrations, device: MeteredDevice) -> str:
    """Return the colour a device is drawn in, the operator's else the palette's; never empty."""
    return registrations.colors.get(device.id) or device_color(device.id)


def serialize_devices(
    devices: Sequence[MeteredDevice],
    registrations: Registrations,
    now: datetime,
) -> list[dict]:
    """Merge the linked devices with their registrations for the panel.

    Args:
        now: Current UTC instant; a ``pending_until`` already in the past is reported as None, so the panel never shows
            a backdated countdown.

    Returns:
        Per-device dicts of exactly what the panel reads: ``id``, ``name``, ``meters``, ``category_label``, ``status``,
        ``transition_at`` (ISO-8601 UTC or None), ``color`` and ``eligible`` (None while unknown).
    """
    result = []
    for device in devices:
        status = current_status(registrations, device.id)
        row = registrations.rows.get(device.id)
        pending_until = row.pending_until if row is not None else None
        if pending_until is not None and pending_until <= now:
            pending_until = None
        result.append({
            "id": device.id,
            "name": device.name,
            "meters": device.as_config()["meters"],
            "category_label": device.category_label,
            "status": status,
            "transition_at": pending_until.isoformat(timespec="seconds") if pending_until else None,
            "color": resolved_color(registrations, device),
            "eligible": is_eligible(registrations, device),
        })
    return result


def serialize_registrations(value: Registrations) -> dict[str, Any]:
    """Serialise the mirror to a JSON-safe payload; every row carries all five keys, ``remote_id`` included when None.

    Returns:
        ``{"rows": {device_id: {"status", "pending_until", "remote_id", "effective_to", "effective_from"}},
        "eligible": {category: label} | None, "backfill_owed": {device_id: remote_id},
        "backfill_refused": {device_id: count}, "registered_at": {device_id: ISO instant},
        "colors": {device_id: "#rrggbb"}}``.
    """
    return {
        "eligible": None if value.eligible is None else dict(value.eligible),
        "backfill_owed": dict(value.backfill_owed),
        "backfill_refused": dict(value.backfill_refused),
        "registered_at": {
            device_id: moment.isoformat(timespec="seconds") for device_id, moment in value.registered_at.items()
        },
        "colors": dict(value.colors),
        "rows": {
            device_id: {
                "status": row.status,
                "pending_until": (
                    row.pending_until.isoformat(timespec="seconds")
                    if row.pending_until is not None else None
                ),
                "remote_id": row.remote_id,
                "effective_to": (
                    row.effective_to.isoformat(timespec="seconds")
                    if row.effective_to is not None else None
                ),
                "effective_from": (
                    row.effective_from.isoformat(timespec="seconds")
                    if row.effective_from is not None else None
                ),
            }
            for device_id, row in value.rows.items()
        },
    }


def deserialize_registrations(payload: dict[str, Any]) -> Registrations:
    """Rebuild the mirror from a stored payload, dropping what is corrupt.

    A bad row is dropped rather than failing the whole load; a missing row reads as ``withdrawn`` and the next sync asks
    the partner. A missing or unreadable ``eligible`` reads as not asked yet, ``backfill_owed`` as nothing owed,
    ``backfill_refused`` as nothing refused, ``registered_at`` as none (the dump then ends at the slot of the call), and
    a colour that is not ``#rrggbb`` falls back to the palette.
    """
    rows: dict[str, RegistrationState] = {}
    for device_id, raw in (payload.get("rows") or {}).items():
        if not isinstance(raw, Mapping) or raw.get("status") not in TENS_DEVICE_STATUSES:
            continue
        rows[str(device_id)] = RegistrationState(
            status=str(raw["status"]),
            pending_until=parse_instant(raw.get("pending_until")),
            remote_id=_stored_int(raw.get("remote_id")),
            effective_to=parse_instant(raw.get("effective_to")),
            effective_from=parse_instant(raw.get("effective_from")),
        )
    owed = payload.get("backfill_owed")
    refused = payload.get("backfill_refused")
    noted = payload.get("registered_at")
    colors = payload.get("colors")
    return Registrations(
        rows=rows,
        eligible=_eligible(payload.get("eligible")),
        backfill_owed={
            str(device_id): remote_id
            for device_id, raw_id in (owed.items() if isinstance(owed, Mapping) else ())
            if (remote_id := _stored_int(raw_id)) is not None
        },
        backfill_refused={
            str(device_id): count
            for device_id, raw_count in (refused.items() if isinstance(refused, Mapping) else ())
            if (count := _stored_int(raw_count)) is not None
        },
        registered_at={
            str(device_id): moment
            for device_id, raw_moment in (noted.items() if isinstance(noted, Mapping) else ())
            if (moment := parse_instant(raw_moment)) is not None
        },
        colors={
            str(device_id): color
            for device_id, raw_color in (colors.items() if isinstance(colors, Mapping) else ())
            if (color := normalize_color(raw_color))
        },
    )


def _eligible(raw: Any) -> dict[str, str] | None:
    """Return a stored eligible list, or None when absent or not a mapping; bad entries are dropped."""
    if not isinstance(raw, Mapping):
        return None
    return {str(key): str(label) for key, label in raw.items() if isinstance(label, str)}


def _stored_int(raw: Any) -> int | None:
    """Return a stored integer (a partner id or a refusal count), or None when it is absent or not an integer."""
    if isinstance(raw, bool) or not isinstance(raw, int):
        return None
    return raw
