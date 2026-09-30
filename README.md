# RealTimeTrains for Home Assistant

Live UK departures from one home station, using the Realtime Trains (RTT) API v2.

## Install
1. Copy `custom_components/realtimetrains` into your Home Assistant `config/custom_components/` folder.
2. Restart Home Assistant.
3. Settings → Devices & services → Add integration → **RealTimeTrains**.
4. Enter your long-lived token (from https://api-portal.rtt.io), the home station code (e.g. `CLJ`) and the max departure count (default 10).

## What you get
* `sensor.<station>_departures_1` ... `_N` - one identical sensor per departure.
  * **State:** expected departure time (timestamp). `unknown` if there is no Nth train.
  * **Attributes:** status, delay_minutes, platform, platform_planned, platform_changed, operator,
    headcode, origin, destination, number_of_vehicles, train_class, units, facilities, reason(s),
    `calling_at` (stops after your station) and `calling_points` (origin -> intermediates -> destination,
    each with planned/forecast/actual times and platform).
* `sensor.<station>_api_status` (diagnostic)
  * **State:** `ok` / `degraded` / `rate_limited` / `auth_error` / `error`
  * **Attributes:** api_success, calls_remaining_minute/hour/day/week, call_limit_*, last_success,
    last_error, poll_interval_seconds, RTT system status, entitlements, token expiry.

## Polling and rate limits
RTT quotas are small (for example 10 calls/minute, 100/hour, 1000/day), so polling is deliberately frugal.

**Schedule** - how soon the next (non-cancelled) train leaves decides the poll interval. Defaults, all
changeable under Settings -> Devices & services -> RealTimeTrains -> **Configure**:

| Next train due in | Poll every |
|---|---|
| under 3 min | 60 s |
| 3 to 10 min | 120 s |
| nothing within 10 min | 300 s |

**Cached route lookups** - a train's route (service lookup) is fetched once, and again only if a Location
call shows its due time at your station has changed. At most 3 route lookups are made per poll (nearest
trains first), so filling the cache after a restart takes a few polls and never bursts past the per-minute limit.

**Budget governor** - independent of the schedule, average usage is held to a share of your hourly, daily and
weekly quotas (default 75%, changeable). Short bursts are allowed (a reserve of 20%), so nothing is slowed
down until usage genuinely runs ahead of budget; then polls are stretched just enough. Whatever the
settings, calls in any hour/day/week stay within ~95% of the limit. The API status sensor shows
`budget_delay_seconds` (extra wait added by the governor) and `budget_calls_available_hour/day`.
On HTTP 429 the `Retry-After` value is honoured.

## Example template
```jinja
{% set t = states.sensor.clapham_junction_departures_1 %}
{{ t.attributes.platform }} to {{ t.attributes.destination }} - {{ t.attributes.status }}
Calling at: {{ t.attributes.calling_at | join(', ') }}
```

## Lovelace card
`www/realtimetrains-card.js` is a dependency-free custom card.

1. Copy it to `/config/www/realtimetrains-card.js`.
2. Add a dashboard resource: URL `/local/realtimetrains-card.js?v=1` (bump `v` after updates), type *JavaScript module*.
3. Add the card:
```yaml
type: custom:realtimetrains-card
station: clapham_junction      # slug from sensor.clapham_junction_departures_1
count: 5                       # departures to show
countdown_minutes: 60          # live countdown for trains leaving within this time (0 = off)
expand: first                  # none | first | all - which routes start expanded
```
Tap a departure to show or hide its full route.
