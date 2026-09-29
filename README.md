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
Polling adapts to how soon the next (non-cancelled) train leaves. Defaults - all changeable under
Settings -> Devices & services -> RealTimeTrains -> **Configure**:

| Next train due in | Poll every |
|---|---|
| under 10 min | 60 s |
| 10 to 20 min | 120 s |
| nothing within 20 min | 240 s |

* Each poll is 1 Location call. Service (route) lookups are cached: a train's service is fetched once,
  and again only if a Location call shows its due time at your station has changed.
* Safety net: if an hourly/daily/weekly quota drops below 20% the interval is at least 5 min, below 5%
  at least 15 min. On HTTP 429 the `Retry-After` value is honoured.

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
