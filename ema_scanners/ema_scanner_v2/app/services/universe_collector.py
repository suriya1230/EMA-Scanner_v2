"""
Universe Collector — bulk-seeds candle history, across every timeframe in
UNIVERSE_INTERVALS (see intervals.py), for EVERY Binance USDT-M Futures
perpetual with 24h quote volume at or above a threshold, independent of the
CSV upload flow (see csv_import.py). This builds a full historical dataset
up front so different strategies can be backtested/analyzed at any of those
timeframes against any qualifying coin, not just whichever ones happen to
appear in an uploaded CSV.

Reuses csv_import.py's per-(symbol, interval) fetch/backfill logic
(_ensure_candles) so candle storage stays consistent across both bulk
universe collection and CSV-driven imports. Also reuses its _retry, which
now paces every REST call against a shared adaptive weight-based throttle
(see rate_limit.py) — critical here since collecting N qualifying symbols
across len(UNIVERSE_INTERVALS) timeframes multiplies request volume by
however many intervals are configured.
"""
from __future__ import annotations

import asyncio
import logging

import aiohttp
import ccxt.async_support as ccxt

from app.core.config import settings
from app.services.csv_import import _ensure_candles, _retry
from app.services.intervals import UNIVERSE_INTERVALS
from app.services.live_stream import restart_live_stream

logger = logging.getLogger(__name__)

# Same bound as csv_import.py's CSV upload path — keeps total concurrent
# Binance requests reasonable across however many (symbol, interval) pairs
# qualify. The adaptive weight throttle (rate_limit.py) is the real safety
# net against actually hitting Binance's limit; this just caps how many
# requests can be in flight waiting on that throttle at once.
FETCH_CONCURRENCY = 10

# A full collection run (hundreds of coins x 8 intervals, thousands of
# REST calls under the adaptive rate throttle) takes several minutes —
# much longer than this interval. _collect_lock (below) is the guard that
# actually matters: it's what stops runs from ever overlapping, whether
# triggered by the scheduler or a manual "Collect Universe" click.
SCHEDULE_INTERVAL_SECONDS = 60
_scheduler_task: asyncio.Task | None = None
_collect_lock = asyncio.Lock()


async def collect_universe(min_volume_usdt: float | None = None) -> dict:
    """Scans every active USDT-M perpetual, keeps the ones with 24h quote
    volume >= min_volume_usdt (defaults to settings.MIN_VOLUME_USDT_SIGNAL),
    and ensures each has ~1000 stored candles at every interval in
    UNIVERSE_INTERVALS. Returns a summary dict."""
    min_volume = min_volume_usdt if min_volume_usdt is not None else settings.MIN_VOLUME_USDT_SIGNAL

    # Same aiohttp session workaround as csv_import.py — ccxt's default
    # aiohttp DNS resolver hangs on this machine resolving Binance hostnames,
    # which would block the whole async event loop without it.
    connector = aiohttp.TCPConnector(resolver=aiohttp.ThreadedResolver(), limit=50)
    session = aiohttp.ClientSession(connector=connector)
    client = ccxt.binanceusdm({"enableRateLimit": True, "session": session})
    qualifying: list[str] = []
    errors: list[str] = []
    try:
        await _retry(client.load_markets)
        tickers = await _retry(client.fetch_tickers)

        for market in client.markets.values():
            # swap=True selects perpetuals only, excluding dated/quarterly
            # delivery futures — same coins the CSV importer's symbol
            # normalization (stripping "_260626"-style suffixes) treats as
            # the regular perpetual.
            if not market.get("swap") or not market.get("active"):
                continue
            if market.get("quote") != "USDT":
                continue
            ticker = tickers.get(market["symbol"])
            volume = ticker.get("quoteVolume") if ticker else None
            if volume is None or volume < min_volume:
                continue
            qualifying.append(market["id"])

        qualifying.sort()
        logger.info(
            "Universe collector: %d/%d USDT-M perpetuals qualify (>= $%.0f 24h volume) — "
            "fetching %d interval(s) each (%d pairs total).",
            len(qualifying), len(client.markets), min_volume,
            len(UNIVERSE_INTERVALS), len(qualifying) * len(UNIVERSE_INTERVALS),
        )

        pairs = [(symbol, interval) for symbol in qualifying for interval in UNIVERSE_INTERVALS]
        semaphore = asyncio.Semaphore(FETCH_CONCURRENCY)

        async def _bounded_ensure(symbol: str, interval: str) -> str | None:
            async with semaphore:
                return await _ensure_candles(client, symbol, interval)

        results = await asyncio.gather(*(_bounded_ensure(s, iv) for s, iv in pairs))
        for err in results:
            if err:
                errors.append(err)
                # ascii() so a garbled/unreadable symbol can't turn into
                # "?" spam on Windows consoles — same as csv_import.py.
                logger.warning("Universe collector: %s", ascii(err))
    finally:
        await client.close()
        await session.close()

    return {
        "min_volume_usdt": min_volume,
        "intervals": UNIVERSE_INTERVALS,
        "symbols_scanned": len(qualifying),
        "pairs_attempted": len(qualifying) * len(UNIVERSE_INTERVALS),
        "pairs_stored": len(qualifying) * len(UNIVERSE_INTERVALS) - len(errors),
        "errors": errors,
    }


async def run_collect_universe_guarded(min_volume_usdt: float | None = None) -> dict | None:
    """Runs collect_universe(), but skips it (returns None) instead of
    queueing behind or overlapping a run that's already in progress — used
    by both the scheduler below and the manual "Collect Universe" button's
    API route, so the two can never run concurrently either."""
    if _collect_lock.locked():
        return None
    async with _collect_lock:
        return await collect_universe(min_volume_usdt)


async def _scheduler_loop() -> None:
    """Fires run_collect_universe_guarded() every SCHEDULE_INTERVAL_SECONDS.
    A full run takes far longer than this interval, so most ticks will find
    the lock held and simply skip — logged, not silent, so it's obvious in
    the logs that the schedule is intentionally being throttled by the lock
    rather than something being broken."""
    while True:
        await asyncio.sleep(SCHEDULE_INTERVAL_SECONDS)
        if _collect_lock.locked():
            logger.info("Universe collector: previous run still in progress, skipping this tick.")
            continue
        try:
            result = await run_collect_universe_guarded()
            if result is not None:
                # Re-subscribe the live WebSocket stream so any newly-
                # discovered coins start getting real-time updates right
                # away, same as after a manual "Collect Universe" click.
                await restart_live_stream()
        except Exception:
            logger.exception("Universe collector: scheduled run failed.")


def start_scheduler() -> None:
    global _scheduler_task
    if _scheduler_task is None or _scheduler_task.done():
        _scheduler_task = asyncio.create_task(_scheduler_loop())
        logger.info("Universe collector: scheduler started (every %ds).", SCHEDULE_INTERVAL_SECONDS)


async def stop_scheduler() -> None:
    global _scheduler_task
    if _scheduler_task is not None:
        _scheduler_task.cancel()
        try:
            await _scheduler_task
        except asyncio.CancelledError:
            pass
        _scheduler_task = None
