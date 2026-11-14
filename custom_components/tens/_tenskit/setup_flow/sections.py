"""Define the section and page ids the 10s steps share, with their labels."""
from __future__ import annotations

# The devices section is not a root row: its menu is reached from inside the 10s section (``tens_menu``).
SECTION_DEVICES = "devices"
SECTION_TENS = "tens"

PAGE_TENS_LOGIN = "tens_login"

SECTION_LABELS = {
    SECTION_DEVICES: "Metered devices",
    SECTION_TENS: "10s account",
}

# The host extends this with its own root ids (``hub`` / ``init``) and any other windows it opens.
BACK_NAMES = {
    SECTION_DEVICES: SECTION_LABELS[SECTION_DEVICES],
    "tens_menu": SECTION_LABELS[SECTION_TENS],
}