"""
Swing Zone Retest Strategy — detects percentage-confirmed swing points on
each coin's 1d candles, projects them forward as price zones, and reports
which coins currently have a live (ARMED or TRIGGERED) zone near price.

Mirrored long/short rules:

  LONG (swing low):
    - Track the running minimum low (the CANDIDATE, L) as price declines.
    - L is CONFIRMED the moment price rises >= SWING_THRESHOLD from the
      confirmation reference (L.low if CONFIRM_FROM="wick", else L.close).
    - Zone level Z = L.close (the candle's close, not its low/open —
      candle colour is irrelevant), projected forward and ARMED.
    - Entry: price trades back down and touches Z (intrabar low <= Z).
    - Stop: Z * (1 - SL_PCT). Target: Z * (1 + TP_PCT).
    - Invalidation: a candle CLOSES at or beyond the stop level (a close-
      based rule, distinct from the wick-based entry touch).

  SHORT (swing high): the exact mirror — running maximum high, confirmed on
    a >= SWING_THRESHOLD fall, Z = H.close, stop above / target below.

A coin can hold one live long zone and one live short zone at once. Each
side runs as its own independent single pass over the full candle history —
a fresh confirmation on a side always REPLACES whatever zone that side
currently holds, regardless of the old zone's state (ARMED or TRIGGERED).

Detection is fully mechanical, replayed from the complete stored candle
history on every call rather than maintained as separately-persisted
incremental state — since the rules are deterministic and the full history
is already durable in Postgres, replaying it always reproduces the exact
same zone/state/age a hand-maintained state table would, without a second
source of truth to keep in sync across restarts.
"""
from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession

from app.services.repository import CandleRepository

TIMEFRAME = "1d"
SWING_THRESHOLD = 0.10          # confirmation move required
TP_PCT = 0.10                   # target distance from Z
SL_PCT = 0.05                   # stop distance from Z
DEFAULT_CONFIRM_FROM = "wick"   # "wick" | "close" — see module docstring
DEFAULT_MIN_VOLUME_USDT = 5_000_000.0
LIVE_STATES = ("ARMED", "TRIGGERED")
# A 1d candle's own open_time is always UTC midnight regardless of when
# within that day the confirming move actually happened — cross-referencing
# stored 1h candles (collected for nearly every coin, unlike 5m/30m) narrows
# "which day" down to "which hour" for a much more meaningful detected time.
REFINE_INTERVAL = "1h"
DAY_MS = 86_400_000


def _run_side(
    candles: list[dict], side: str, confirm_from: str, max_zone_age: int | None,
) -> dict | None:
    """Replays one coin's full candle history once for one side ("long" or
    "short") and returns whatever zone is currently live (state ARMED or
    TRIGGERED), or None if there isn't one. See module docstring for rules."""
    candidate: dict | None = None
    zone: dict | None = None

    for idx, c in enumerate(candles):
        # 1. Advance the existing zone's lifecycle using this candle
        #    (entry touch, then close-based stop/target) before checking
        #    whether this same candle also confirms a brand-new zone.
        if zone is not None and zone["state"] in LIVE_STATES:
            z, sl, tp = zone["z"], zone["sl"], zone["tp"]
            if side == "long":
                if zone["state"] == "ARMED" and c["low"] <= z:
                    zone["state"] = "TRIGGERED"
                    zone["triggered_at"] = idx
                if zone["state"] == "TRIGGERED":
                    if c["close"] <= sl:
                        zone["state"] = "SL_HIT"
                    elif c["high"] >= tp:
                        zone["state"] = "TP_HIT"
                elif c["close"] <= sl:  # still ARMED, never touched — close-based invalidation
                    zone["state"] = "SL_HIT"
            else:
                if zone["state"] == "ARMED" and c["high"] >= z:
                    zone["state"] = "TRIGGERED"
                    zone["triggered_at"] = idx
                if zone["state"] == "TRIGGERED":
                    if c["close"] >= sl:
                        zone["state"] = "SL_HIT"
                    elif c["low"] <= tp:
                        zone["state"] = "TP_HIT"
                elif c["close"] >= sl:
                    zone["state"] = "SL_HIT"

            if (
                zone["state"] == "ARMED" and max_zone_age is not None
                and idx - zone["confirmed_at"] > max_zone_age
            ):
                zone["state"] = "EXPIRED"

        # 2. Advance the candidate-extreme tracker while price is still
        #    making a new extreme ("track the running minimum low AS PRICE
        #    DECLINES") — a candle that just became the new extreme is, by
        #    definition, still on the declining leg, so it can't ALSO be
        #    the candle that confirms a rise away from it. Confirmation is
        #    only checked on candles that did NOT extend the extreme,
        #    i.e. strictly after the candidate was set. Without this split,
        #    a single volatile candle could become its own candidate AND
        #    satisfy the confirming move off itself in the same iteration —
        #    which is how one candle used to produce both a BUY and a SELL
        #    zone simultaneously (long and short each self-confirming off
        #    their own still-forming candle).
        if side == "long":
            if candidate is None or c["low"] < candidate["low"]:
                candidate = c
                continue
            reference = candidate["low"] if confirm_from == "wick" else candidate["close"]
            move_pct = (c["high"] - reference) / reference if reference else 0.0
        else:
            if candidate is None or c["high"] > candidate["high"]:
                candidate = c
                continue
            reference = candidate["high"] if confirm_from == "wick" else candidate["close"]
            move_pct = (reference - c["low"]) / reference if reference else 0.0

        if reference and move_pct >= SWING_THRESHOLD:
            z = candidate["close"]
            rng = candidate["high"] - candidate["low"]
            body_ratio = abs(candidate["close"] - candidate["open"]) / rng if rng else 0.0
            zone = {
                "z": z,
                "state": "ARMED",
                "sl": z * (1 - SL_PCT) if side == "long" else z * (1 + SL_PCT),
                "tp": z * (1 + TP_PCT) if side == "long" else z * (1 - TP_PCT),
                "swing_extreme": candidate["low"] if side == "long" else candidate["high"],
                "confirm_move_pct": move_pct,
                "body_ratio": body_ratio,
                "confirmed_at": idx,
                "triggered_at": None,
            }
            candidate = None  # restart the search for this side's next extreme

    return zone if zone is not None and zone["state"] in LIVE_STATES else None


async def _refine_detected_at(
    db: AsyncSession, symbol: str, market: str, day_open_time: int, reference: float, side: str,
) -> int:
    """Narrows a confirming 1d candle's own open_time (always UTC midnight)
    down to the actual hour that day the confirming move first happened, by
    replaying that single day's stored 1h candles against the same
    reference/threshold the 1d-level confirmation used. Falls back to the
    day's open_time if no 1h data covers that day (e.g. a coin missing 1h
    history) or none of it actually reaches the threshold (can happen if the
    confirming 1d candle's own OTHER wick, not covered by hourly data yet on
    a still-forming day, is what crossed it)."""
    if not reference:
        return day_open_time
    hourly = await CandleRepository.get_ohlc_in_range(
        db, symbol=symbol, interval=REFINE_INTERVAL, market=market,
        start_time=day_open_time, end_time=day_open_time + DAY_MS,
    )
    for row in hourly:
        move_pct = (
            (row["high"] - reference) / reference if side == "long"
            else (reference - row["low"]) / reference
        )
        if move_pct >= SWING_THRESHOLD:
            return row["open_time"]
    return day_open_time


async def scan_swing_zones(
    db: AsyncSession,
    market: str = "futures",
    min_volume_usdt: float = DEFAULT_MIN_VOLUME_USDT,
    confirm_from: str = DEFAULT_CONFIRM_FROM,
    max_zone_age: int | None = None,
) -> dict:
    """Scans every coin with stored 1d candles, keeps the ones whose latest
    day's approximate USD volume (volume * close — 1d candle volume already
    IS the day's total, so no separate 24h lookback is needed) is >=
    min_volume_usdt, and returns one dashboard row per currently-live
    long/short zone, sorted by absolute distance to Z ascending (the
    actionable watchlist first)."""
    candles_by_symbol = await CandleRepository.get_ohlc_by_symbol(db, interval=TIMEFRAME, market=market)

    rows = []
    symbols_scanned = 0
    for symbol, candles in candles_by_symbol.items():
        if len(candles) < 2:
            continue
        latest = candles[-1]
        approx_volume_usdt = latest["volume"] * latest["close"]
        if approx_volume_usdt < min_volume_usdt:
            continue
        symbols_scanned += 1
        price = latest["close"]
        latest_idx = len(candles) - 1

        for side in ("long", "short"):
            zone = _run_side(candles, side, confirm_from, max_zone_age)
            if zone is None:
                continue
            day_open_time = candles[zone["confirmed_at"]]["open_time"]
            reference = zone["swing_extreme"] if confirm_from == "wick" else zone["z"]
            # Only refine when the confirming day has already fully closed —
            # if it confirmed off the still-forming latest candle (the
            # same-day self-confirmation case), `reference` is itself only
            # known because the day is aggregated so far, so checking it
            # against that same day's earlier hours would be look-ahead
            # bias (an early hour's high compared against a low that, in
            # real time, hadn't happened yet). Falls back to the day's
            # open_time in that case, same as before this refinement existed.
            if zone["confirmed_at"] < latest_idx:
                detected_at = await _refine_detected_at(db, symbol, market, day_open_time, reference, side)
            else:
                detected_at = day_open_time
            rows.append({
                "symbol": symbol,
                "direction": "LONG" if side == "long" else "SHORT",
                "state": zone["state"],
                "z": zone["z"],
                "price": price,
                "distance_pct": (price - zone["z"]) / zone["z"] * 100,
                "sl": zone["sl"],
                "tp": zone["tp"],
                "confirm_move_pct": zone["confirm_move_pct"] * 100,
                "zone_age": latest_idx - zone["confirmed_at"],
                "detected_at": detected_at,  # ms epoch, narrowed to the actual hour where possible
                "body_ratio": zone["body_ratio"],
                "swing_extreme": zone["swing_extreme"],
            })

    rows.sort(key=lambda r: abs(r["distance_pct"]))

    return {
        "market": market,
        "timeframe": TIMEFRAME,
        "min_volume_usdt": min_volume_usdt,
        "confirm_from": confirm_from,
        "swing_threshold": SWING_THRESHOLD,
        "tp_pct": TP_PCT,
        "sl_pct": SL_PCT,
        "symbols_scanned": symbols_scanned,
        "zones": rows,
    }
