"""
CSV Backtest — Upload Import
=============================
Accepts a CSV of coin signals that can mix MULTIPLE timeframes in the same
file (5m, 15m, 30m, 1h, ...) — each row carries its own timeframe, which is
respected independently (this is separate from the rest of the app, which
only ever tracks a fixed set of intervals for its own scanner). For every
distinct (symbol, timeframe) pair found, fetches that coin's candle history
from Binance at that exact timeframe if not already stored, then replaces
the `imported_signals` table with this upload's rows.

Column names are matched case-insensitively against a few common aliases.
"""
from __future__ import annotations

import asyncio
import csv
import io
import logging
import re

import aiohttp
import ccxt.async_support as ccxt
import pandas as pd
from tenacity import AsyncRetrying, retry_if_exception_type, stop_after_attempt, wait_exponential

from app.db.database import AsyncSessionLocal
from app.services.repository import CandleRepository, ImportedSignalRepository

logger = logging.getLogger(__name__)

MARKET = "futures"
CANDLE_RETENTION_TARGET = 1000  # candles per (symbol, timeframe) — same regardless of granularity
PAGE_LIMIT = 1000
# On-demand deepening (see ensure_depth) is per-coin and user-triggered (opening
# a coin's Backtest Details page), so it can afford to go deeper than the
# bulk per-upload fetch — capped so a bad `days` value can't trigger an
# unbounded number of Binance pages.
MAX_BACKFILL_CANDLES = 20_000

VALID_BINANCE_INTERVALS = {
    "1m", "3m", "5m", "15m", "30m", "1h", "2h", "4h", "6h", "8h", "12h", "1d", "3d", "1w", "1M",
}

SYMBOL_ALIASES = ("symbol", "coin", "coin name", "coin_name", "pair", "ticker")
TYPE_ALIASES = ("signal_type", "type", "side", "signal", "direction")
TIME_ALIASES = ("detected_at", "signal_time", "cross_time", "time", "date", "timestamp")
TIMEFRAME_ALIASES = ("timeframe", "time_frame", "tf", "interval")
PRICE_ALIASES = ("cross_price", "price", "entry_price")
EMA_FAST_ALIASES = ("ema_fast", "ema fast", "ema7", "ema_7")
EMA_MID_ALIASES = ("ema_mid", "ema mid", "ema25", "ema_25")
EMA_SLOW_ALIASES = ("ema_slow", "ema slow", "ema99", "ema_99")
SCORE_ALIASES = ("score (0-100)", "score", "signal_score")

RETRYABLE = (ccxt.NetworkError,)  # includes RateLimitExceeded/DDoSProtection/ExchangeNotAvailable


async def _retry(fn, *args, **kwargs):
    # A big CSV can reference hundreds of (symbol, interval) pairs fetched
    # one after another — Binance rate-limit bans can last longer than a
    # few seconds, so this waits longer and tries more times than a single
    # one-off request would need.
    async for attempt in AsyncRetrying(
        stop=stop_after_attempt(6),
        wait=wait_exponential(multiplier=1, min=2, max=60),
        retry=retry_if_exception_type(RETRYABLE),
        reraise=True,
    ):
        with attempt:
            return await fn(*args, **kwargs)


def _normalize_header(s: str) -> str:
    """Lowercase + collapse spaces/underscores so 'Signal Time', 'signal_time',
    and 'signal time' all match the same alias."""
    return s.lower().strip().replace(" ", "_")


def _pick_column(headers: list[str], aliases: tuple[str, ...]) -> str | None:
    lowered = {_normalize_header(h): h for h in headers}
    for alias in aliases:
        if _normalize_header(alias) in lowered:
            return lowered[_normalize_header(alias)]
    return None


def interval_to_ms(interval: str) -> int | None:
    """Generic Binance-interval -> milliseconds, e.g. '5m'->300000, '1h'->3600000.
    Lowercase 'm' is minutes, uppercase 'M' is months — matches Binance's own
    convention, so this is NOT case-normalized."""
    m = re.fullmatch(r"(\d+)([mhdwM])", interval.strip())
    if not m:
        return None
    n, unit = int(m.group(1)), m.group(2)
    unit_ms = {"m": 60_000, "h": 3_600_000, "d": 86_400_000, "w": 604_800_000, "M": 2_592_000_000}
    return n * unit_ms[unit]


def _to_float(raw) -> float | None:
    try:
        return float(raw)
    except (TypeError, ValueError):
        return None


def parse_csv(raw_bytes: bytes) -> tuple[list[dict], list[str]]:
    """Returns (parsed_rows, warnings). Each row:
    {symbol, signal_type, interval, cross_time (datetime), cross_price,
    ema_fast, ema_mid, ema_slow, score}."""
    text = raw_bytes.decode("utf-8-sig")
    reader = csv.DictReader(io.StringIO(text))
    if not reader.fieldnames:
        return [], ["CSV has no header row."]

    symbol_col = _pick_column(reader.fieldnames, SYMBOL_ALIASES)
    type_col = _pick_column(reader.fieldnames, TYPE_ALIASES)
    time_col = _pick_column(reader.fieldnames, TIME_ALIASES)
    tf_col = _pick_column(reader.fieldnames, TIMEFRAME_ALIASES)
    price_col = _pick_column(reader.fieldnames, PRICE_ALIASES)
    ema_fast_col = _pick_column(reader.fieldnames, EMA_FAST_ALIASES)
    ema_mid_col = _pick_column(reader.fieldnames, EMA_MID_ALIASES)
    ema_slow_col = _pick_column(reader.fieldnames, EMA_SLOW_ALIASES)
    score_col = _pick_column(reader.fieldnames, SCORE_ALIASES)

    if not symbol_col or not type_col or not time_col:
        return [], [
            f"Could not find required columns in header {reader.fieldnames} — "
            f"need a symbol column (one of {SYMBOL_ALIASES}), a signal type column "
            f"(one of {TYPE_ALIASES}), and a time column (one of {TIME_ALIASES})."
        ]

    rows = []
    warnings = []
    for i, raw in enumerate(reader, start=2):  # row 1 is the header
        symbol = (raw.get(symbol_col) or "").strip().upper()
        stype = (raw.get(type_col) or "").strip().upper()
        if stype in ("LONG", "B", "BULL", "BULLISH"):
            stype = "BUY"
        elif stype in ("SHORT", "S", "BEAR", "BEARISH"):
            stype = "SELL"
        if not symbol or stype not in ("BUY", "SELL"):
            warnings.append(f"Row {i}: skipped — symbol/signal type missing or invalid ({raw}).")
            continue
        # Strip Binance dated-contract suffixes like "_260626" (quarterly/
        # delivery futures expiry, e.g. BTCUSDT_260626) — treat as the
        # regular perpetual coin, which is what this app actually tracks.
        symbol = re.sub(r"_\d{6}$", "", symbol)
        if not symbol.endswith("USDT"):
            symbol = f"{symbol}USDT"

        interval = (raw.get(tf_col) or "1h").strip() if tf_col else "1h"
        if interval not in VALID_BINANCE_INTERVALS:
            warnings.append(f"Row {i}: skipped — unrecognized timeframe '{interval}'.")
            continue

        raw_time = raw.get(time_col)
        try:
            ts = pd.to_datetime(raw_time)
            if pd.isna(ts):
                raise ValueError("unparseable")
            # The CSV's timestamps have no timezone marker but are recorded
            # in IST (the source system's local time), not UTC — localize as
            # IST then convert to real UTC so the frontend's UTC->IST display
            # conversion doesn't double-shift the time forward by 5:30.
            if ts.tzinfo is None:
                ts = ts.tz_localize("Asia/Kolkata").tz_convert("UTC")
            else:
                ts = ts.tz_convert("UTC")
            cross_time = ts.to_pydatetime()
        except Exception:
            warnings.append(f"Row {i}: skipped — could not parse time '{raw_time}'.")
            continue

        rows.append({
            "symbol": symbol, "signal_type": stype, "interval": interval,
            "cross_time": cross_time,
            "cross_price": _to_float(raw.get(price_col)) if price_col else None,
            "ema_fast": _to_float(raw.get(ema_fast_col)) if ema_fast_col else None,
            "ema_mid": _to_float(raw.get(ema_mid_col)) if ema_mid_col else None,
            "ema_slow": _to_float(raw.get(ema_slow_col)) if ema_slow_col else None,
            "score": _to_float(raw.get(score_col)) if score_col else None,
        })

    return rows, warnings


async def _ensure_candles(client: "ccxt.binanceusdm", symbol: str, interval: str) -> str | None:
    """Makes sure `symbol` has candles stored at this exact timeframe —
    fetches from Binance (first-time page-back, or incremental catch-up) if
    needed, same convention as market_data_collector.py's _fetch_and_store.
    Returns an error string on failure, None on success."""
    candle_ms = interval_to_ms(interval)
    entry = client.markets_by_id.get(symbol)
    market_info = entry[0] if isinstance(entry, list) else entry
    if not market_info:
        return f"{symbol}: not found on Binance Futures."
    unified = market_info["symbol"]

    async with AsyncSessionLocal() as session:
        latest_open = await CandleRepository.get_latest_open_time(
            session, symbol, interval=interval, market=MARKET
        )

    rows: list = []
    try:
        if latest_open is None:
            since = None
            fetched = 0
            max_rounds = -(-CANDLE_RETENTION_TARGET // PAGE_LIMIT) + 1
            for _ in range(max_rounds):
                page = await _retry(client.fetch_ohlcv, unified, timeframe=interval, since=since, limit=PAGE_LIMIT)
                if not page:
                    break
                rows.extend(page)
                fetched += len(page)
                since = int(page[-1][0]) + 1
                if fetched >= CANDLE_RETENTION_TARGET or len(page) < PAGE_LIMIT:
                    break
        else:
            page = await _retry(client.fetch_ohlcv, unified, timeframe=interval, since=latest_open, limit=PAGE_LIMIT)
            rows.extend(page or [])
    except Exception as exc:
        return f"{symbol} [{interval}]: Binance fetch failed ({exc})."

    if rows:
        candle_rows = [
            {
                "symbol": symbol, "market": MARKET, "interval": interval,
                "open_time": int(r[0]), "open": float(r[1]), "high": float(r[2]),
                "low": float(r[3]), "close": float(r[4]), "volume": float(r[5]),
                "close_time": int(r[0]) + candle_ms - 1,
            }
            for r in rows
        ]
        async with AsyncSessionLocal() as session:
            await CandleRepository.upsert_candles(session, candle_rows)
            await session.commit()

    # The incremental fetch above only ever extends forward from the latest
    # stored candle — it can't notice or heal a hole left in the MIDDLE of a
    # symbol's history by an earlier interrupted/rate-limited import. Since
    # the entry-price lookup just walks candles in stored order, a gap there
    # silently makes it land on the wrong candle. Check for and backfill any
    # such gaps every time.
    return await _backfill_gaps(client, unified, symbol, interval, candle_ms)


async def _backfill_gaps(
    client: "ccxt.binanceusdm", unified: str, symbol: str, interval: str, candle_ms: int
) -> str | None:
    async with AsyncSessionLocal() as session:
        times = await CandleRepository.get_open_times(session, symbol, interval=interval, market=MARKET)
    if len(times) < 2:
        return None

    gaps = [(times[i - 1] + candle_ms, times[i]) for i in range(1, len(times)) if times[i] - times[i - 1] > candle_ms]
    if not gaps:
        return None

    rows: list = []
    try:
        for gap_start, gap_end in gaps:
            since = gap_start
            while since < gap_end:
                page = await _retry(client.fetch_ohlcv, unified, timeframe=interval, since=since, limit=PAGE_LIMIT)
                page = [r for r in page if r[0] < gap_end] if page else []
                if not page:
                    break
                rows.extend(page)
                since = int(page[-1][0]) + candle_ms
    except Exception as exc:
        return f"{symbol} [{interval}]: gap backfill failed ({exc})."

    if not rows:
        return None

    candle_rows = [
        {
            "symbol": symbol, "market": MARKET, "interval": interval,
            "open_time": int(r[0]), "open": float(r[1]), "high": float(r[2]),
            "low": float(r[3]), "close": float(r[4]), "volume": float(r[5]),
            "close_time": int(r[0]) + candle_ms - 1,
        }
        for r in rows
    ]
    async with AsyncSessionLocal() as session:
        await CandleRepository.upsert_candles(session, candle_rows)
        await session.commit()
    return None


async def _fetch_missing_candles(
    client: "ccxt.binanceusdm", unified: str, interval: str, candle_ms: int,
    target_count: int, existing_count: int, earliest: int | None,
) -> list:
    """Fetches (but does not store) whatever candles are needed to bring one
    pair's stored history up to `target_count`, extending backward from
    `earliest`. Split out of ensure_depth so the tricky backward-paging
    logic is easy to read on its own."""
    rows: list = []
    # A pair with literally nothing stored yet shouldn't normally happen
    # (CSV import seeds ~1000 candles for every pair it processes) but can
    # if that pair's initial fetch errored out. `since=None` returns
    # Binance's MOST RECENT candles, not its oldest — so establish a real
    # `earliest` reference from that before trying to page further into the
    # past, instead of (incorrectly) paging forward from it, which just
    # re-requests data at/after "now" and always comes back empty.
    if earliest is None:
        seed_page = await _retry(client.fetch_ohlcv, unified, timeframe=interval, since=None, limit=PAGE_LIMIT)
        if not seed_page:
            return rows
        rows.extend(seed_page)
        earliest = int(seed_page[0][0])

    need = target_count - existing_count - len(rows)
    fetched = 0
    max_rounds = -(-max(need, 0) // PAGE_LIMIT) + 1
    since = earliest - need * candle_ms
    for _ in range(max_rounds):
        if need <= 0:
            break
        page = await _retry(client.fetch_ohlcv, unified, timeframe=interval, since=since, limit=PAGE_LIMIT)
        if not page:
            break
        # Once we've paged forward far enough to reach candles we already
        # have stored, stop — the rest is already covered.
        page = [r for r in page if r[0] < earliest]
        rows.extend(page)
        fetched += len(page)
        if not page or fetched >= need:
            break
        since = int(page[-1][0]) + candle_ms
    return rows


def _rows_to_candle_dicts(symbol: str, interval: str, candle_ms: int, rows: list) -> list[dict]:
    return [
        {
            "symbol": symbol, "market": MARKET, "interval": interval,
            "open_time": int(r[0]), "open": float(r[1]), "high": float(r[2]),
            "low": float(r[3]), "close": float(r[4]), "volume": float(r[5]),
            "close_time": int(r[0]) + candle_ms - 1,
        }
        for r in rows
    ]


async def ensure_depth(symbol: str, interval: str, days: int) -> dict:
    """On-demand deep history fetch for ONE (symbol, interval) pair.

    The bulk CSV import only pulls CANDLE_RETENTION_TARGET (1000) candles per
    pair to keep a large upload fast — for a short timeframe like 5m that's
    only ~3.5 days, far less than the 7d/14d/30d windows the per-coin
    Backtest Details page offers. Rather than fetch deep history for every
    coin up front (wasted work for coins nobody opens), this extends ONE
    coin's stored range further into the past, triggered only when a user
    actually opens that coin's Details page and picks a window needing it.
    """
    candle_ms = interval_to_ms(interval)
    target_count = min(MAX_BACKFILL_CANDLES, -(-int(days * 86_400_000) // candle_ms) + 50)

    # Cheap check FIRST: an earlier version of this function called
    # client.load_markets() — a real Binance API call — before checking
    # whether anything even needed fetching, so every already-sufficient
    # pair hit Binance anyway. Checking our own DB first avoids that.
    async with AsyncSessionLocal() as db_session:
        times = await CandleRepository.get_open_times(db_session, symbol, interval=interval, market=MARKET)
    if len(times) >= target_count:
        return {"added": 0}

    connector = aiohttp.TCPConnector(resolver=aiohttp.ThreadedResolver(), limit=50)
    session_http = aiohttp.ClientSession(connector=connector)
    client = ccxt.binanceusdm({"enableRateLimit": True, "session": session_http})
    try:
        await _retry(client.load_markets)
        entry = client.markets_by_id.get(symbol)
        market_info = entry[0] if isinstance(entry, list) else entry
        if not market_info:
            return {"added": 0, "error": f"{symbol}: not found on Binance Futures."}
        unified = market_info["symbol"]

        try:
            rows = await _fetch_missing_candles(
                client, unified, interval, candle_ms, target_count, len(times), times[0] if times else None
            )
        except Exception as exc:
            return {"added": 0, "error": f"{symbol} [{interval}]: deep fetch failed ({exc})."}

        if not rows:
            return {"added": 0}

        candle_rows = _rows_to_candle_dicts(symbol, interval, candle_ms, rows)
        async with AsyncSessionLocal() as db_session:
            await CandleRepository.upsert_candles(db_session, candle_rows)
            await db_session.commit()
        return {"added": len(candle_rows)}
    finally:
        await client.close()
        await session_http.close()


async def import_csv(raw_bytes: bytes) -> dict:
    """Parses the CSV, fetches/stores candles for every (symbol, timeframe)
    pair referenced, replaces `imported_signals` with this upload, and
    returns a summary."""
    parsed_rows, warnings = parse_csv(raw_bytes)
    if not parsed_rows:
        return {"symbols_fetched": 0, "signals_imported": 0, "warnings": warnings, "errors": []}

    pairs = sorted({(r["symbol"], r["interval"]) for r in parsed_rows})

    # Same aiohttp session workaround as market_data_collector.py — on this
    # machine, ccxt's default aiohttp DNS resolver (aiodns/pycares) hangs
    # trying to resolve Binance hostnames, which would block the entire
    # async event loop (freezing the whole backend, not just this request)
    # without it.
    connector = aiohttp.TCPConnector(resolver=aiohttp.ThreadedResolver(), limit=50)
    session = aiohttp.ClientSession(connector=connector)
    client = ccxt.binanceusdm({"enableRateLimit": True, "session": session})
    errors: list[str] = []
    try:
        await _retry(client.load_markets)

        # A CSV can reference hundreds of distinct (symbol, interval) pairs —
        # fetching them one at a time (as before) could take many minutes.
        # ccxt's own enableRateLimit still throttles every request through
        # this one client, so running several pairs concurrently just fills
        # the gaps between throttle waits instead of fetching one-by-one.
        semaphore = asyncio.Semaphore(10)

        async def _bounded_ensure(symbol: str, interval: str) -> str | None:
            async with semaphore:
                return await _ensure_candles(client, symbol, interval)

        results = await asyncio.gather(*(_bounded_ensure(s, iv) for s, iv in pairs))
        for err in results:
            if err:
                errors.append(err)
                # ascii() so a garbled/unreadable symbol from the CSV (bad
                # characters) shows as a safe escaped string instead of
                # turning into "?" spam on Windows consoles.
                logger.warning("CSV import: %s", ascii(err))
    finally:
        await client.close()
        await session.close()

    failed_pairs = set()
    for e in errors:
        sym = e.split(" [")[0].split(":")[0]
        tf = e.split("[")[1].split("]")[0] if "[" in e else None
        failed_pairs.add((sym, tf))

    importable_rows = [r for r in parsed_rows if (r["symbol"], r["interval"]) not in failed_pairs]

    db_rows = [
        {
            "symbol": r["symbol"], "market": MARKET, "interval": r["interval"],
            "signal_type": r["signal_type"], "cross_price": r["cross_price"],
            "cross_time": r["cross_time"], "ema_fast": r["ema_fast"],
            "ema_mid": r["ema_mid"], "ema_slow": r["ema_slow"], "score": r["score"],
        }
        for r in importable_rows
    ]
    async with AsyncSessionLocal() as session:
        inserted = await ImportedSignalRepository.replace_all(session, db_rows)
        await session.commit()

    return {
        "symbols_fetched": len(pairs) - len(failed_pairs),
        "signals_imported": inserted,
        "warnings": warnings,
        "errors": errors,
    }
