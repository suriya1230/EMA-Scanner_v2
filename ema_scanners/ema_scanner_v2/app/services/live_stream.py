"""
Live Candle Stream — keeps every currently-stored coin's candles updated in
real time, across every timeframe in UNIVERSE_INTERVALS (see intervals.py),
via Binance USDT-M Futures' combined WebSocket kline stream — instead of
only refreshing on-demand via Collect Universe. Runs continuously in the
background for the lifetime of the app (started from main.py's lifespan)
and restarted (to pick up any newly-stored symbols) whenever Collect
Universe finishes — see app/api/universe.py.

Binance caps a single WebSocket connection at ~200 streams. With multiple
intervals per symbol, the unit sharded across connections is now individual
"<symbol>@kline_<interval>" stream names (symbols x intervals), not just
symbols — a 772-symbol x 7-interval universe is ~5,400 streams, needing on
the order of 30 connections. Each connection reconnects with backoff on drop.

The in-progress (not-yet-closed) candle IS updated live, per each kline
tick — Binance can push several updates per second per symbol under active
trading, so ticks are buffered in memory and flushed to Postgres on a fixed
interval rather than one DB write per tick. That keeps "live" meaning
sub-second-to-a-couple-seconds latency without turning hundreds of symbols'
trade activity directly into hundreds of individual writes per second.

A WebSocket connection can go quiet without ever raising an error — Binance
(or a network path in between) can stop actually delivering messages while
the TCP socket stays technically open, so the normal reconnect-on-exception
logic below never notices anything is wrong. A separate watchdog task (see
start_watchdog/_watchdog_loop) checks every WATCHDOG_INTERVAL_SECONDS
whether each connection has received a message recently; if one's gone
silent for longer than STALE_THRESHOLD_SECONDS, it force-restarts every
connection. This is a status check (a timestamp comparison), not a Binance
API call, so running it every 30 seconds costs nothing against rate limits
— unlike trying to re-poll every (symbol, interval) pair on a timer, which
would need far more request weight per minute than Binance allows.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time

import websockets

from app.db.database import AsyncSessionLocal
from app.services.intervals import UNIVERSE_INTERVALS
from app.services.repository import CandleRepository

logger = logging.getLogger(__name__)

MARKET = "futures"
STREAM_BASE = "wss://fstream.binance.com/stream"
STREAMS_PER_CONNECTION = 190  # stays under Binance's ~200-stream-per-connection cap
FLUSH_INTERVAL_SECONDS = 1.0
RECONNECT_BACKOFF = [1, 2, 5, 10, 30]  # seconds; holds at the last value once exhausted
WATCHDOG_INTERVAL_SECONDS = 30
STALE_THRESHOLD_SECONDS = 120  # no message from a connection this long -> treat it as dead

_tasks: list[asyncio.Task] = []
_pending: dict[tuple[str, str, str, int], dict] = {}
_stop_event = asyncio.Event()
_last_message_at: dict[int, float] = {}   # conn_id -> time.time() of its most recent message
_watchdog_task: asyncio.Task | None = None
_watchdog_stop_event = asyncio.Event()    # separate from _stop_event — must survive restart_live_stream()


def _chunk(items: list[str], size: int) -> list[list[str]]:
    return [items[i:i + size] for i in range(0, len(items), size)]


def _handle_message(raw: str) -> None:
    """Parses one combined-stream WebSocket frame and stages its candle row
    for the next flush. The row's interval comes from the kline payload's
    own "i" field, not a hardcoded constant, so one connection can carry a
    mix of timeframes. Plain dict mutation with no `await` inside — safe
    without a lock since asyncio never preempts mid-coroutine between await
    points."""
    global _pending
    try:
        msg = json.loads(raw)
        k = msg.get("data", {}).get("k")
        if not k:
            return
        row = {
            "symbol": k["s"], "market": MARKET, "interval": k["i"],
            "open_time": int(k["t"]), "open": float(k["o"]), "high": float(k["h"]),
            "low": float(k["l"]), "close": float(k["c"]), "volume": float(k["v"]),
            "close_time": int(k["T"]),
        }
        key = (row["symbol"], row["market"], row["interval"], row["open_time"])
        _pending[key] = row
    except Exception:
        logger.exception("Live stream: failed to parse message: %s", ascii(raw)[:200])


async def _flush_loop() -> None:
    global _pending
    while not _stop_event.is_set():
        await asyncio.sleep(FLUSH_INTERVAL_SECONDS)
        if not _pending:
            continue
        # Swap the dict out atomically (no await between read and clear) so
        # incoming ticks during the DB write below land in a fresh dict
        # instead of being lost or racing the upsert.
        rows, _pending = list(_pending.values()), {}
        try:
            async with AsyncSessionLocal() as session:
                await CandleRepository.upsert_candles(session, rows)
                await session.commit()
        except Exception:
            logger.exception("Live stream: failed to flush %d candle row(s).", len(rows))


async def _handle_connection(stream_names: list[str], conn_id: int) -> None:
    url = f"{STREAM_BASE}?streams={'/'.join(stream_names)}"
    backoff_idx = 0

    while not _stop_event.is_set():
        try:
            async with websockets.connect(url, ping_interval=180, ping_timeout=60) as ws:
                logger.info("Live stream conn #%d: connected (%d streams).", conn_id, len(stream_names))
                backoff_idx = 0
                _last_message_at[conn_id] = time.time()
                async for raw in ws:
                    if _stop_event.is_set():
                        break
                    _last_message_at[conn_id] = time.time()
                    _handle_message(raw)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            if _stop_event.is_set():
                break
            wait = RECONNECT_BACKOFF[min(backoff_idx, len(RECONNECT_BACKOFF) - 1)]
            backoff_idx += 1
            logger.warning("Live stream conn #%d: disconnected (%s) — retrying in %ds.", conn_id, exc, wait)
            await asyncio.sleep(wait)


async def start_live_stream() -> None:
    """Reads whichever symbols are currently stored (e.g. from the last
    Collect Universe run), builds a "<symbol>@kline_<interval>" stream name
    for every (symbol, interval) in UNIVERSE_INTERVALS, and opens one
    WebSocket connection per chunk of up to STREAMS_PER_CONNECTION of them,
    plus the periodic DB flush task. No-op if nothing is stored yet."""
    async with AsyncSessionLocal() as session:
        symbols = await CandleRepository.distinct_symbols(session, market=MARKET)

    if not symbols:
        logger.info("Live stream: no stored symbols yet — nothing to stream. Run Collect Universe first.")
        return

    _stop_event.clear()
    _last_message_at.clear()  # stale conn_ids from a previous run shouldn't confuse the watchdog
    stream_names = sorted(
        f"{symbol.lower()}@kline_{interval}"
        for symbol in symbols
        for interval in UNIVERSE_INTERVALS
    )
    chunks = _chunk(stream_names, STREAMS_PER_CONNECTION)
    for i, chunk in enumerate(chunks):
        _tasks.append(asyncio.create_task(_handle_connection(chunk, i)))
    _tasks.append(asyncio.create_task(_flush_loop()))
    logger.info(
        "Live stream: started %d connection(s) covering %d symbol(s) x %d interval(s) (%d stream(s)).",
        len(chunks), len(symbols), len(UNIVERSE_INTERVALS), len(stream_names),
    )


async def stop_live_stream() -> None:
    if not _tasks:
        return
    _stop_event.set()
    for t in _tasks:
        t.cancel()
    await asyncio.gather(*_tasks, return_exceptions=True)
    _tasks.clear()
    logger.info("Live stream: stopped.")


async def restart_live_stream() -> None:
    """Re-reads the stored symbol list and reopens connections — called
    after Collect Universe finishes so newly-stored coins join the live
    stream without needing a full backend restart."""
    await stop_live_stream()
    await start_live_stream()


def is_running() -> bool:
    return any(not t.done() for t in _tasks)


def status() -> dict:
    now = time.time()
    silent_for = {cid: round(now - ts) for cid, ts in _last_message_at.items()}
    return {
        "running": is_running(),
        "connections": max(len(_tasks) - 1, 0),  # excludes the flush-loop task
        "intervals": UNIVERSE_INTERVALS,
        "seconds_since_last_message": silent_for,
        "watchdog_running": _watchdog_task is not None and not _watchdog_task.done(),
    }


async def _watchdog_loop() -> None:
    """Runs for the lifetime of the app (independent of start/stop/restart
    cycles on the connections themselves) and checks every
    WATCHDOG_INTERVAL_SECONDS whether any connection has gone quiet for
    longer than STALE_THRESHOLD_SECONDS — a live, correctly-flowing
    connection should receive kline updates continuously (Binance pushes
    them roughly every 1-2s per open kline stream), so a multi-minute
    silence means the connection died without raising an exception."""
    while not _watchdog_stop_event.is_set():
        await asyncio.sleep(WATCHDOG_INTERVAL_SECONDS)
        if _watchdog_stop_event.is_set() or not _last_message_at:
            continue
        now = time.time()
        stale = [cid for cid, ts in _last_message_at.items() if now - ts > STALE_THRESHOLD_SECONDS]
        if stale:
            logger.warning(
                "Live stream watchdog: connection(s) %s silent for over %ds — forcing a full restart.",
                stale, STALE_THRESHOLD_SECONDS,
            )
            await restart_live_stream()


def start_watchdog() -> None:
    global _watchdog_task
    _watchdog_stop_event.clear()
    if _watchdog_task is None or _watchdog_task.done():
        _watchdog_task = asyncio.create_task(_watchdog_loop())
        logger.info("Live stream watchdog: started (checks every %ds).", WATCHDOG_INTERVAL_SECONDS)


async def stop_watchdog() -> None:
    global _watchdog_task
    _watchdog_stop_event.set()
    if _watchdog_task is not None:
        _watchdog_task.cancel()
        try:
            await _watchdog_task
        except asyncio.CancelledError:
            pass
        _watchdog_task = None
