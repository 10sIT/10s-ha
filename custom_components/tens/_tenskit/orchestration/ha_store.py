"""Define the store seam: the shape a host's persistence must satisfy, keeping the kit off Home Assistant storage."""
from __future__ import annotations

from typing import Protocol, TypeVar

T = TypeVar("T")


class Store(Protocol[T]):
    """The persisted-value contract the partner runtime requires.

    ``value`` is the loaded value, None until the first load or save; the runtime only reads it. ``save`` persists the
    whole value.
    """

    @property
    def value(self) -> T | None:
        """Return the loaded value, or None when it has not been read or written."""
        ...

    async def save(self, value: T) -> None:
        """Persist ``value`` as the store's new value."""