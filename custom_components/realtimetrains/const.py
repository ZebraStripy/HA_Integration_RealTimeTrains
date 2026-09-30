"""Constants for the RealTimeTrains integration."""
from __future__ import annotations

from datetime import timedelta

DOMAIN = "realtimetrains"

# ---------------------------------------------------------------------------
# Config entry keys (what the user types into the config flow)
# ---------------------------------------------------------------------------
CONF_TOKEN = "token"                  # RTT long-lived (refresh) token
CONF_STATION = "station"              # Home station code, e.g. "CLJ"
CONF_STATION_NAME = "station_name"    # Human name resolved from the API, e.g. "Clapham Junction"
CONF_MAX_DEPARTURES = "max_departures"

DEFAULT_MAX_DEPARTURES = 10
MIN_MAX_DEPARTURES = 1
MAX_MAX_DEPARTURES = 20               # Sanity cap: one entity per departure

# ---------------------------------------------------------------------------
# RTT API
# ---------------------------------------------------------------------------
API_BASE_URL = "https://data.rtt.io"
ENDPOINT_ACCESS_TOKEN = "/api/get_access_token"
ENDPOINT_LOCATION = "/gb-nr/location"   # Network Rail location line-up (departures board)
ENDPOINT_SERVICE = "/gb-nr/service"     # Network Rail service detail (all calling points)

# The spec allows pinning the API version with a "Version: YYYY-MM-DD" header.
# Pinning protects you from breaking changes; leave as None to always get the
# latest version. Set it to a date you have tested against, e.g. "2026-01-18".
API_VERSION: str | None = None

REQUEST_TIMEOUT = 20                                # seconds per HTTP request
TOKEN_EXPIRY_MARGIN = timedelta(seconds=60)         # renew the access token a minute early

# How far ahead the Location lookup looks. One call returns *all* services in
# the window, so a wider window costs bandwidth, not extra API calls. It has to
# be wide enough to contain MAX departures at a quiet station.
LOCATION_TIME_WINDOW_MINUTES = 120

# ---------------------------------------------------------------------------
# Polling behaviour (all user-configurable via the integration's Configure button)
# ---------------------------------------------------------------------------
# How often we poll depends on how soon the next (non-cancelled) train leaves:
#   next train due in < FAST_WINDOW minutes            -> poll every FAST_INTERVAL seconds
#   next train due in FAST_WINDOW .. MEDIUM_WINDOW min -> poll every MEDIUM_INTERVAL seconds
#   nothing due within MEDIUM_WINDOW minutes           -> poll every SLOW_INTERVAL seconds
CONF_FAST_INTERVAL = "fast_interval"
CONF_MEDIUM_INTERVAL = "medium_interval"
CONF_SLOW_INTERVAL = "slow_interval"
CONF_FAST_WINDOW = "fast_window_minutes"
CONF_MEDIUM_WINDOW = "medium_window_minutes"
CONF_BUDGET_SHARE = "budget_share_percent"   # % of the API quota we allow ourselves to use

DEFAULT_FAST_INTERVAL = 60       # seconds
DEFAULT_MEDIUM_INTERVAL = 120    # seconds
DEFAULT_SLOW_INTERVAL = 300      # seconds
DEFAULT_FAST_WINDOW = 3          # minutes
DEFAULT_MEDIUM_WINDOW = 10       # minutes
# These defaults are sized for a 1000-calls/day quota. Longer/faster settings
# work too - the budget governor below slows polling down if usage runs ahead.

DEFAULT_BUDGET_SHARE = 75          # percent of the Hour/Day/Week quota to use on average

DEFAULT_OPTIONS = {
    CONF_FAST_INTERVAL: DEFAULT_FAST_INTERVAL,
    CONF_MEDIUM_INTERVAL: DEFAULT_MEDIUM_INTERVAL,
    CONF_SLOW_INTERVAL: DEFAULT_SLOW_INTERVAL,
    CONF_FAST_WINDOW: DEFAULT_FAST_WINDOW,
    CONF_MEDIUM_WINDOW: DEFAULT_MEDIUM_WINDOW,
    CONF_BUDGET_SHARE: DEFAULT_BUDGET_SHARE,
}

MIN_INTERVAL = 30                 # seconds; don't allow polling faster than this
MAX_INTERVAL = 3600

# Service (route) lookups per refresh. RTT's per-minute limit can be as low as 10,
# and a refresh already spends 1 call on the Location lookup, so route lookups are
# spread over several refreshes (nearest trains first).
MAX_SERVICE_CALLS_PER_UPDATE = 3

# Rate-limit header dimensions defined by the RTT spec.
RATE_LIMIT_DIMENSIONS = ("Minute", "Hour", "Day", "Week")

# Values of the API status sensor
STATUS_OK = "ok"                    # everything worked
STATUS_DEGRADED = "degraded"        # departures fetched, but some detail lookups were skipped/failed
STATUS_RATE_LIMITED = "rate_limited"
STATUS_AUTH_ERROR = "auth_error"
STATUS_ERROR = "error"
STATUS_UNKNOWN = "unknown"          # nothing attempted yet
STATUS_OPTIONS = [
    STATUS_OK,
    STATUS_DEGRADED,
    STATUS_RATE_LIMITED,
    STATUS_AUTH_ERROR,
    STATUS_ERROR,
    STATUS_UNKNOWN,
]

ATTRIBUTION = "Data provided by Realtime Trains (rtt.io)"
