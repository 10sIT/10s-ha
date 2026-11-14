"""Turn meter readings into 10s measurement blocks, purely.

``POST /devices/{id}/measurements`` takes ``{start, end, granularity, metricType, unit, values, totalValue}`` covering
``[start, end)`` (https://api.dev.10senergy.com/v3/api-docs/external). The values are the energy used in each slot, not
readings: :func:`slot_energies` is the one place the archive's slot totals are differenced, and everything that shows
slot energy consumes its output, so it shows what 10s receives. Readings are rounded to 3 decimals before they are
differenced, so the values of any split of a stretch add up to the difference of its end readings. What cannot be
measured is ``null`` in place, never skipped or guessed: a slot nobody read in, and a negative difference (a counter
reset or meter swap). The consumed counter feeds ``CONSUMPTION``, the returned one ``PRODUCTION``.
"""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

from ...contract import SLOT_MINUTES, DeviceSlot

METRIC_CONSUMPTION = "CONSUMPTION"
METRIC_PRODUCTION = "PRODUCTION"
ENERGY_UNIT = "kWh"
SLOT_GRANULARITY = timedelta(minutes=SLOT_MINUTES)

# Not a limit in the spec (``maxItems`` was dropped); chunking bounds a request should an unstated cap exist.
MAX_BLOCK_VALUES = 20_000

_SCALE = Decimal("0.001")
_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)
# Consumption before production when two blocks share a window, as the spec lists the metric types.
_METRIC_ORDER = {METRIC_CONSUMPTION: 0, METRIC_PRODUCTION: 1}


@dataclass(frozen=True)
class MeasurementBlock:
    """One block of fixed-length slots for one metric.

    Attributes:
        start: The UTC instant the first slot opens, on the granularity's grid.
        granularity: The length of every slot.
        metric_type: ``CONSUMPTION`` or ``PRODUCTION``.
        values: Per slot, the energy as a 3-decimal string, None when it cannot be measured.
    """

    start: datetime
    granularity: timedelta
    metric_type: str
    values: tuple[str | None, ...]

    @property
    def end(self) -> datetime:
        """Return the exclusive end of the block."""
        return self.start + self.granularity * len(self.values)

    @property
    def total(self) -> Decimal:
        """Return the exact sum of the non-null values at their 3 decimals."""
        return sum((Decimal(value) for value in self.values if value is not None), Decimal("0.000"))

    def as_payload(self) -> dict[str, Any]:
        """Return the block as the JSON-safe body item the endpoint takes.

        ``totalValue`` goes out as a float: every total has 3 decimals and few significant digits, so the shortest float
        ``repr`` is exactly the decimal and the server parses back the same sum.
        """
        return {
            "start": _iso_instant(self.start),
            "end": _iso_instant(self.end),
            "granularity": iso_duration(self.granularity),
            "metricType": self.metric_type,
            "unit": ENERGY_UNIT,
            "values": list(self.values),
            "totalValue": float(self.total),
        }


@dataclass(frozen=True)
class SlotEnergy:
    """The energy of one 15-minute slot, exactly as it is sent to 10s.

    Attributes:
        start: The mark that opens the slot; the total stored under it is the one that closes it.
        consumed_kwh: Energy in, None when the slot's total or the previous slot's is missing or the counter ran
            backwards.
        returned_kwh: Energy out, None likewise and always for a device with no returned side.
    """

    start: datetime
    consumed_kwh: Decimal | None
    returned_kwh: Decimal | None


def slot_energies(readings: Sequence[DeviceSlot]) -> list[SlotEnergy]:
    """Return the energy of every slot after a device's first archived total, up to its last.

    Each side is differenced on its own from totals rounded to 3 decimals. The first total only opens the next slot.

    Args:
        readings: One device's slot totals on the 15-minute grid in any order; a slot seen twice keeps its last total.

    Returns:
        One entry per slot, earliest first, gaps and resets as None; empty with fewer than two totals.
    """
    consumed = {slot.start: _quantize(slot.consumed_kwh) for slot in readings}
    returned = {slot.start: _quantize(slot.returned_kwh) for slot in readings if slot.returned_kwh is not None}
    if len(consumed) < 2:
        return []
    first, last = min(consumed), max(consumed)
    marks = [first + SLOT_GRANULARITY * (index + 1) for index in range((last - first) // SLOT_GRANULARITY)]
    return [
        SlotEnergy(
            start=mark,
            consumed_kwh=_slot_difference(consumed, mark),
            returned_kwh=_slot_difference(returned, mark),
        )
        for mark in marks
    ]


def _slot_difference(held: Mapping[datetime, Decimal], mark: datetime) -> Decimal | None:
    """Return the total of the slot at ``mark`` minus the previous slot's, or None when it has none."""
    closing = held.get(mark)
    opening = held.get(mark - SLOT_GRANULARITY)
    if opening is None or closing is None or closing < opening:
        return None
    return closing - opening


def _returned_span(readings: Sequence[DeviceSlot]) -> tuple[datetime, datetime] | None:
    """Return the first and last marks with a returned reading, or None when fewer than two have one."""
    marks = {slot.start for slot in readings if slot.returned_kwh is not None}
    if len(marks) < 2:
        return None
    return min(marks), max(marks)


def encode_device(
    readings: Sequence[DeviceSlot], *, max_values: int = MAX_BLOCK_VALUES,
) -> list[MeasurementBlock]:
    """Encode one device's readings as 15-minute measurement blocks.

    Args:
        readings: One device's slot totals, earliest first, starting with the total that opens the first slot to send
            (its watermark reading when it has one).
        max_values: The most values one block may carry.

    Returns:
        The blocks ordered by ``start``, consumption before production within a window; empty until two totals close a
        slot. The last consumption block's ``end`` is one slot after the mark of the last total used, which is the
        watermark to confirm once 10s has accepted them.
    """
    energies = slot_energies(readings)
    if not energies:
        return []
    blocks = build_blocks(
        METRIC_CONSUMPTION, energies[0].start, SLOT_GRANULARITY,
        [energy.consumed_kwh for energy in energies], max_values=max_values,
    )
    # PRODUCTION covers only the stretch the returned totals open and close.
    span = _returned_span(readings)
    if span is not None:
        first, last = span
        blocks.extend(build_blocks(
            METRIC_PRODUCTION, first + SLOT_GRANULARITY, SLOT_GRANULARITY,
            [energy.returned_kwh for energy in energies if first < energy.start <= last], max_values=max_values,
        ))
    return sorted(blocks, key=lambda block: (block.start, _METRIC_ORDER[block.metric_type]))


def series_blocks(
    metric_type: str,
    energies: Mapping[datetime, float],
    granularity: timedelta,
    *,
    max_values: int = MAX_BLOCK_VALUES,
) -> list[MeasurementBlock]:
    """Encode a sparse series of per-slot energies, the history dump, as blocks.

    The values are already energy per slot, so nothing is differenced. An absent slot, and a negative one, is ``null``
    in place.

    Args:
        energies: Slot start to energy in kWh, for the slots that were measured.
        granularity: The slot length, which the starts must sit on.

    Raises:
        ValueError: A start is not on the UTC grid of ``granularity``.
    """
    if not energies:
        return []
    first, last = min(energies), max(energies)
    values: list[float | None] = []
    for index in range((last - first) // granularity + 1):
        energy = energies.get(first + granularity * index)
        values.append(None if energy is None or energy < 0 else energy)
    return build_blocks(metric_type, first, granularity, values, max_values=max_values)


def build_blocks(
    metric_type: str,
    start: datetime,
    granularity: timedelta,
    values: Sequence[Decimal | float | None],
    *,
    max_values: int = MAX_BLOCK_VALUES,
) -> list[MeasurementBlock]:
    """Cut one metric's per-slot energies into blocks of at most ``max_values``, windows meeting exactly.

    Values are rounded to 3 decimals; judging them (negative, reset) is the caller's job.

    Args:
        values: Per slot, the energy in kWh, None when unknown.

    Raises:
        ValueError: ``start`` is not on the UTC grid of ``granularity``.
    """
    start = start.astimezone(UTC)
    if (start - _EPOCH) % granularity:
        raise ValueError(f"{start.isoformat()} is not on the {iso_duration(granularity)} grid")
    rendered = tuple(None if value is None else _render(value) for value in values)
    return [
        MeasurementBlock(
            start=start + granularity * offset,
            granularity=granularity,
            metric_type=metric_type,
            values=rendered[offset:offset + max_values],
        )
        for offset in range(0, len(rendered), max_values)
    ]


def iso_duration(span: timedelta) -> str:
    """Return a slot length as an ISO-8601 duration: ``PT15M``, ``PT5M``, ``PT1H``.

    Raises:
        ValueError: ``span`` is not a positive whole number of seconds.
    """
    seconds = span.total_seconds()
    if seconds <= 0 or seconds != int(seconds):
        raise ValueError(f"not a slot length: {span}")
    whole = int(seconds)
    if whole % 3600 == 0:
        return f"PT{whole // 3600}H"
    if whole % 60 == 0:
        return f"PT{whole // 60}M"
    return f"PT{whole}S"


def _iso_instant(moment: datetime) -> str:
    """Return an instant as UTC ISO-8601 with a ``Z`` suffix, to the second."""
    return moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _quantize(value: float | Decimal) -> Decimal:
    """Round a kWh figure to the wire's 3 decimals; a float goes through its shortest ``repr``."""
    exact = value if isinstance(value, Decimal) else Decimal(repr(float(value)))
    return exact.quantize(_SCALE)


def _render(value: Decimal | float) -> str:
    """Return one value as its wire string: ``.`` decimal, exactly 3 decimals."""
    quantized = _quantize(value)
    # A tiny negative rounds to -0.000, which is zero energy.
    return f"{quantized.copy_abs() if quantized.is_zero() else quantized:.3f}"
