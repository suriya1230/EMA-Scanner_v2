"""
Adaptive Binance rate-limit throttle — reads the X-MBX-USED-WEIGHT-1M
response header ccxt exposes after every REST call and, once usage nears
the real per-minute cap Binance enforces, holds further requests until the
next weight-reset window instead of guessing a fixed delay.

Kept as a single process-wide instance since Binance's weight budget is per
source IP, not per ccxt client instance — every REST caller in this app
(CSV import, Universe Collector's bulk fetch and its ensure-depth) shares
the same real budget, so they need to throttle against the same tracker,
not one each.
"""
from __future__ import annotations

import asyncio
import logging
import time

logger = logging.getLogger(__name__)

# Binance's documented general REST limit for USDM Futures is 2400 request
# weight per minute per IP. The threshold is held well under that so bursts
# across many concurrent (symbol, interval) fetches don't tip the account
# into a 418 (IP ban) or 429 (rate limited) in the first place — the
# tenacity retry/backoff already in csv_import.py's _retry is a fallback
# for when a limit gets hit anyway, not the primary defense.
WEIGHT_LIMIT_PER_MINUTE = 2400
SAFETY_RATIO = 0.75


class _WeightThrottle:
    def __init__(self, limit: int = WEIGHT_LIMIT_PER_MINUTE, safety_ratio: float = SAFETY_RATIO):
        self.threshold = int(limit * safety_ratio)
        self._pause_until = 0.0

    def observe(self, client) -> None:
        """Call after a REST response comes back — inspects ccxt's
        last_response_headers for the actual used weight and, if it's
        already near the real limit, schedules a pause for new requests."""
        headers = getattr(client, "last_response_headers", None)
        if not headers:
            return
        used = None
        for key, value in headers.items():
            if key.upper() == "X-MBX-USED-WEIGHT-1M":
                try:
                    used = int(value)
                except (TypeError, ValueError):
                    pass
                break
        if used is None or used < self.threshold:
            return

        now = time.time()
        # Binance's used-weight window resets on the minute boundary — wait
        # until just past the next one rather than guessing a fixed delay.
        wait_for = (60 - now % 60) + 0.5
        new_pause_until = now + wait_for
        if new_pause_until > self._pause_until:
            logger.warning(
                "Rate limit throttle: used weight %d >= threshold %d — pausing new requests for %.1fs.",
                used, self.threshold, wait_for,
            )
            self._pause_until = new_pause_until

    async def wait(self) -> None:
        """Call before making a REST request — sleeps if a prior observe()
        scheduled a pause that hasn't elapsed yet."""
        now = time.time()
        if now < self._pause_until:
            await asyncio.sleep(self._pause_until - now)


# One throttle per process — see module docstring for why this is shared
# rather than instantiated per ccxt client.
throttle = _WeightThrottle()
