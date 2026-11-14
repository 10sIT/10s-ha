"""The 10s account form: the registration token, spent by the redemption and never persisted.

The token alone names the contract it was issued under, so it is the form's only field.
"""
from __future__ import annotations

import voluptuous as vol
from homeassistant.helpers.selector import (
    TextSelector,
    TextSelectorConfig,
    TextSelectorType,
)

TENS_REGISTRATION_TOKEN = "registration_token"

# Password type so the one-time token is neither echoed on screen nor offered back by the browser.
_TOKEN = TextSelector(TextSelectorConfig(type=TextSelectorType.PASSWORD, autocomplete="off"))


def tens_login_schema() -> vol.Schema:
    """Build the 10s login form: the registration token."""
    # Optional so the frontend lets "← Back" through; the step checks the field.
    return vol.Schema({vol.Optional(TENS_REGISTRATION_TOKEN): _TOKEN})


def validate_login(user_input: dict) -> dict[str, str]:
    """Return field-keyed errors for the login form before contacting 10s; empty when a redemption can be attempted.

    The token is opaque to us, so only its presence is checked.
    """
    if not str(user_input.get(TENS_REGISTRATION_TOKEN, "")).strip():
        return {TENS_REGISTRATION_TOKEN: "required"}
    return {}
