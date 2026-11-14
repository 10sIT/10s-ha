"""Constants shared between the kit and its host: config-entry keys, device vocabulary, slot and cadence timings."""

CONF_METERED_DEVICES = "metered_devices"
CONF_TENS_ACCOUNT = "tens_account"
# The 10s environments by the name a redeemed account stores; production first, the default. A stored account naming
# any other is no account.
TENS_ENVIRONMENT_PRODUCTION = "production"
TENS_ENVIRONMENT_DEV = "dev"
TENS_ENVIRONMENT_NAMES = (TENS_ENVIRONMENT_PRODUCTION, TENS_ENVIRONMENT_DEV)
# 10s's own category names in its order (``GET /devices/eligible-categories`` on the dev environment, 2026-10-22);
# they cross the wire as they are.
TENS_DEVICE_CATEGORIES = ("HEAT_PUMP", "EV_CHARGER", "BATTERY", "BOILER")

# A newly linked device is ``withdrawn`` until it is registered; from then on the status is read off the partner's
# effective window (``status_from_window``).
TENS_STATUS_REGISTERING = "registering"
TENS_STATUS_REGISTERED = "registered"
TENS_STATUS_WITHDRAWING = "withdrawing"
TENS_STATUS_WITHDRAWN = "withdrawn"
TENS_DEVICE_STATUSES = (
    TENS_STATUS_REGISTERING,
    TENS_STATUS_REGISTERED,
    TENS_STATUS_WITHDRAWING,
    TENS_STATUS_WITHDRAWN,
)
TENS_DEVICE_COLOR_PALETTE = (
    "#42a5f5", "#66bb6a", "#ffa726", "#ef5350", "#ab47bc",
    "#26c6da", "#fbc02d", "#8d6e63", "#5c6bc0", "#ec407a",
)
SLOT_MINUTES = 15
# The push is capped at 72 h, so nothing consumes a reading beyond it.
DEVICE_SLOT_RETENTION_HOURS = 72
# Eight quarter-hour marks: a meter that skips a cycle or two never trips it, a dead one is reported the same day.
SILENT_METER_HOURS = 2
# 10s wants every slot within 36 hours; 8 hours leaves room for three lost contacts in a row. Each installation keeps
# the phase its own start gave it, which spreads the load on 10s.
CONTACT_INTERVAL_HOURS = 8
# Two missed contacts in a row plus slack, so a single failed contact never trips it; two more contacts are still due
# inside 10s's 36 hours.
STALLED_UPLOAD_HOURS = 2 * CONTACT_INTERVAL_HOURS + 4
