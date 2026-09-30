"""Quota governor: keeps our API usage inside the RTT rate limits, whatever the schedule.

Why this exists
---------------
The polling schedule (see const.py) says how often we WOULD like to poll. The
RTT quota (e.g. 1000 calls/day, 100/hour) says how often we CAN. This module
sits between the two: it never lets the average call rate exceed a chosen share
of the quota, but it still allows short bursts, so nothing is slowed down
unless usage is genuinely running ahead of budget.

How it works: one "token bucket" per rate-limit period (Hour, Day, Week)
  * The bucket refills continuously at  share x limit / period  tokens/second.
  * It holds at most a small reserve (default 20% of the limit).
  * Every API call we make spends one token (it may go negative).
  * If a bucket is empty, the next poll is delayed until it has refilled enough.

Guarantee: in ANY window of one period we make at most
  (share + reserve) x limit calls  - e.g. 0.75 + 0.20 = 95% of the limit -
regardless of how the server defines its window (rolling or fixed), and
regardless of restarts, because the server's own `Remaining` counter is used as
a cap whenever it is lower than our estimate.

Pure Python (no Home Assistant imports) so it is easy to test.
"""
from __future__ import annotations

from datetime import datetime

# Periods we govern. "Minute" is left out on purpose: bursts within a minute are
# limited by the schedule's minimum interval and the per-update service-call cap.
PERIOD_SECONDS: dict[str, int] = {"Hour": 3600, "Day": 86400, "Week": 604800}

# Keep this fraction of every limit in hand even when the server says we have
# more (protects against other apps sharing the same token, restarts, etc.).
_SERVER_RESERVE = 0.05


class CallBudget:
    """Token buckets for the Hour / Day / Week API limits."""

    def __init__(self, share: float = 0.75, burst: float = 0.20) -> None:
        self.share = share
        # Reserve size as a fraction of each limit. Never let share + reserve
        # exceed 95% of the limit.
        self.burst = max(0.01, min(burst, 0.95 - share))
        self._tokens: dict[str, float] = {}
        self._updated: datetime | None = None

    def _rate(self, limit: int, dim: str) -> float:
        """Tokens per second."""
        return self.share * limit / PERIOD_SECONDS[dim]

    def record(
        self, now: datetime, calls: int, limits: dict[str, int], remaining: dict[str, int]
    ) -> None:
        """Account for `calls` API calls just made. Call after every update attempt."""
        elapsed = max(0.0, (now - self._updated).total_seconds()) if self._updated else 0.0
        self._updated = now

        for dim in PERIOD_SECONDS:
            limit = limits.get(dim)
            if not limit:
                continue                                    # server hasn't told us this limit (yet)
            capacity = self.burst * limit
            tokens = self._tokens.get(dim, capacity)        # first time: start with a full reserve
            tokens = min(capacity, tokens + self._rate(limit, dim) * elapsed) - calls
            left = remaining.get(dim)
            if left is not None:
                # Trust the server if it says we have less than we think.
                tokens = min(tokens, left - _SERVER_RESERVE * limit)
            self._tokens[dim] = tokens

    def seconds_until_allowed(self, limits: dict[str, int], needed: float = 1.0) -> float:
        """How long until every bucket holds `needed` tokens (0 if we can call now)."""
        wait = 0.0
        for dim in PERIOD_SECONDS:
            limit = limits.get(dim)
            tokens = self._tokens.get(dim)
            if not limit or tokens is None or tokens >= needed:
                continue
            wait = max(wait, (needed - tokens) / self._rate(limit, dim))
        return wait

    @property
    def tokens(self) -> dict[str, float]:
        """Calls currently available per period (for the status sensor)."""
        return dict(self._tokens)
