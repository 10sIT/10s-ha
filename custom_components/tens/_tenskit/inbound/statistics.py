"""Read a meter's recorded history from Home Assistant's statistics, for the registration dump only.

``change`` is the only column read. It is in kWh only because the read asks for it (:data:`REQUESTED_UNITS`); unasked,
the recorder answers in the entity's current state unit and a ``Wh`` counter would be dumped 1000 times too large.
Home Assistant 2024.11, the floor, already returns ``change`` and a unix-timestamp ``start``, so there is no fallback
for another shape. :func:`parse_statistics` and below are pure.
"""
from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

# Without it the recorder answers in the entity's current unit.
REQUESTED_UNITS = {"energy": "kWh"}

PERIOD_FIVE_MINUTE = "5minute"
PERIOD_HOUR = "hour"

_PERIOD_MINUTES = {PERIOD_FIVE_MINUTE: 5, PERIOD_HOUR: 60}

RECORDER_DOMAIN = "recorder"


class RecorderReadError(Exception):
    """The recorder is loaded but its statistics could not be read.

    Distinct from an empty answer: a caller treating the two alike would record a transient database error as a
    delivered, empty history.
    """


@dataclass(frozen=True)
class StatBucket:
    """One statistics bucket.

    Attributes:
        start: The bucket's UTC start.
        change: The bucket's ``change`` in kWh as the recorder computes it; None when the recorder has a row but no
            usable value (present but empty, not absent).
    """

    start: datetime
    change: float | None


def period_minutes(period: str) -> int:
    """Return how many minutes one bucket of ``period`` covers."""
    return _PERIOD_MINUTES[period]


async def async_fetch_statistics(
    hass: Any,
    entity_ids: Iterable[str],
    start: datetime,
    end: datetime,
    period: str = PERIOD_FIVE_MINUTE,
) -> dict[str, list[StatBucket]]:
    """Return each counter's statistics buckets over ``[start, end)``, time-sorted.

    Args:
        entity_ids: The counters to read; blanks and duplicates are dropped.
        start: Inclusive UTC window start.
        end: Exclusive UTC window end.
        period: :data:`PERIOD_FIVE_MINUTE` or :data:`PERIOD_HOUR`.

    Returns:
        Buckets per entity id. A counter the recorder holds nothing for is absent; an install without the recorder
        yields an empty mapping.

    Raises:
        RecorderReadError: The recorder is loaded but the read failed.
    """
    wanted = {entity_id for entity_id in entity_ids if entity_id}
    if not wanted or end <= start or RECORDER_DOMAIN not in hass.config.components:
        return {}
    try:
        from homeassistant.components.recorder import get_instance
        from homeassistant.components.recorder.statistics import statistics_during_period

        instance = get_instance(hass)
        raw = await instance.async_add_executor_job(
            _call_statistics, statistics_during_period, hass, start, end, wanted, period,
        )
    # The recorder raises whatever its database layer raises (SQLAlchemy, sqlite3, a missing instance); all mean
    # "not read", which a caller must tell apart from "nothing recorded".
    except Exception as err:
        raise RecorderReadError(f"statistics read failed for {sorted(wanted)}: {err!r}") from err
    return parse_statistics(raw)


def _call_statistics(
    fetch: Any, hass: Any, start: datetime, end: datetime, statistic_ids: set[str], period: str,
) -> Any:
    """Call the recorder's ``statistics_during_period`` in its executor.

    Args:
        fetch: The recorder function, passed in so its import stays in the caller.
    """
    return fetch(hass, start, end, statistic_ids, period, REQUESTED_UNITS, {"change"})


def parse_statistics(raw: Any) -> dict[str, list[StatBucket]]:
    """Turn the recorder's raw ``{statistic_id: [row, ...]}`` mapping into time-sorted buckets per id.

    Ids with no usable row are left out.
    """
    if not isinstance(raw, Mapping):
        return {}
    out: dict[str, list[StatBucket]] = {}
    for statistic_id, rows in raw.items():
        buckets = _buckets(rows)
        if buckets:
            out[str(statistic_id)] = buckets
    return out


def _buckets(rows: Any) -> list[StatBucket]:
    """Return one statistic's rows as buckets, earliest first."""
    buckets = [
        StatBucket(start, _number(row.get("change")))
        for row in rows or ()
        if isinstance(row, Mapping) and (start := _row_start(row.get("start"))) is not None
    ]
    buckets.sort(key=lambda bucket: bucket.start)
    return buckets


def _row_start(value: Any) -> datetime | None:
    """Return a statistics row's unix-timestamp ``start`` as an aware UTC datetime."""
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return datetime.fromtimestamp(float(value), tz=UTC)
    return None


def _number(value: Any) -> float | None:
    """Return a row field as a float, or None when it is not a usable number."""
    if value is None or isinstance(value, bool):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None
