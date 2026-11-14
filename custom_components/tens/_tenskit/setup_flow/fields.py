"""Form helpers the 10s forms share: the Confirm dropdown, selector lists, ``suggest`` and ``missing_required``."""
from __future__ import annotations

from typing import Any

import voluptuous as vol
from homeassistant.helpers.selector import (
    SelectSelector,
    SelectSelectorConfig,
    SelectSelectorMode,
)

# HA gives a form one button only, so what Confirm does is chosen in a selector: save and go back (the default) or
# discard and go back to the window the form came from.
SAVE = "save"
GO_TO = "go_to"

_SAVE_LABEL = "\u2713 save and go back to {parent}"
_DISCARD_LABEL = "\u2715 discard and go back to {parent}"


def next_options(back_to: str, parent: str, save_label: str | None = None) -> list[dict[str, str]]:
    """Return a form's Confirm dropdown options: save, or discard to the parent.

    The discard option's value is the parent window's step id, so the step handler can route to it directly.

    Args:
        back_to: Step id of the parent window the form was opened from.
        parent: Its display label, shown in the option text.
        save_label: Text of the SAVE option when Confirm does something other than save and go back.
    """
    return [
        {"value": SAVE, "label": save_label or _SAVE_LABEL.format(parent=parent)},
        {"value": back_to, "label": _DISCARD_LABEL.format(parent=parent)},
    ]


def with_next(schema: vol.Schema, options: list[dict[str, str]]) -> vol.Schema:
    """Return the schema with the Confirm dropdown appended: a select field ``GO_TO`` defaulting to ``SAVE``."""
    config = SelectSelectorConfig(options=options, mode=SelectSelectorMode.DROPDOWN)
    return schema.extend({vol.Optional(GO_TO, default=SAVE): SelectSelector(config)})


def list_selector(options: list[dict[str, str]], *, multiple: bool = False) -> SelectSelector:
    """Build a list-mode SelectSelector from a list of ``{"value": ..., "label": ...}`` dicts."""
    return SelectSelector(SelectSelectorConfig(options=options, mode=SelectSelectorMode.LIST, multiple=multiple))


def suggest(value: Any) -> dict[str, Any]:
    """Return a field description that pre-fills ``value`` without making it a default.

    HA fills a missing optional field with its ``default`` and a cleared field is submitted as missing, so a ``default``
    can never be cleared; ``suggested_value`` only pre-fills the widget.
    """
    return {"suggested_value": value} if value not in (None, "", []) else {}


def missing_required(user_input: dict, keys: Any) -> dict[str, str]:
    """Return a ``required`` error for every listed field left blank; empty when all are set."""
    return {key: "required" for key in keys if user_input.get(key) in (None, "", [])}