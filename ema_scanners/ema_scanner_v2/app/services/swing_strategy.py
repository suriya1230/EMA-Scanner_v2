"""
Swing Zone Retest Strategy — detects percentage-confirmed swing points on
each coin's 1d candles, projects them forward as price zones, and reports
which coins currently have a live (ARMED or TRIGGERED) LONG and/or SHORT
zone near price.

Detection is a SINGLE alternating state machine (matches the formal
"Swing High & Low Strategy Specification" v1.0), not two independent
long/short candidate searches — a confirmed swing low is always followed
by tracking toward a swing high, then back to tracking a low, and so on:

  1. SEARCHING (bootstrap only, until the very first swing point exists):
     track the running peak and running trough from the start of history;
     whichever leg (fall from the running peak, or rise from the running
     trough) reaches SWING_THRESHOLD first decides the initial direction
     and fixes the opposite extreme as the anchor going forward.

  2. TRACKING_LOW: the anchor (a swing high — either the previous CONFIRMED
     swing high, or the bootstrap peak) is FIXED; it does not move again
     just because a new intraday peak appears while we're in this phase.
     Track the running trough since the anchor as the CANDIDATE swing low
     — each new lower low replaces the candidate (Rule 4.1). The candidate
     only becomes eligible for confirmation ("PRIMED") once the fall from
     the anchor down to it is ITSELF >= SWING_THRESHOLD — the anchor has
     to be a genuine qualifying peak relative to this candidate, not just
     whatever the most recent high happened to be. Once primed, the
     moment a later candle's CLOSE (not wick — the recovery must actually
     hold through the close, not just spike past it intrabar) recovers >=
     SWING_THRESHOLD above the candidate's own low, the candidate is
     CONFIRMED as a swing low: Z = candidate.low, a tradeable LONG zone
     arms, and the state flips to TRACKING_HIGH with the anchor now fixed
     at this candidate.

  3. TRACKING_HIGH: the mirror of the above — candidate = running peak,
     primed once the rise from the anchor up to it is itself >=
     SWING_THRESHOLD, confirmed by a later candle's CLOSE falling >=
     SWING_THRESHOLD below the candidate's own high. A confirmed swing
     high arms a tradeable SHORT zone (Z = candidate.high) the same way,
     and flips the state back to TRACKING_LOW.

So every swing point needs THREE qualifying legs, not two: the move INTO
the candidate (anchor -> candidate, checked via "primed"), and the move
OUT of it that confirms the reversal (candidate -> confirming candle) —
both independently >= SWING_THRESHOLD.

A later candle can never confirm a candidate off itself in the same
iteration that just extended it (same-day exclusion / anti-look-ahead) —
enforced by `continue`-ing immediately after a candidate update.

LONG and SHORT each keep their own independent live trade — confirming a
new swing low only replaces the LONG trade, confirming a new swing high
only replaces the SHORT trade, so a coin can hold one live position of
each side at once even though detection itself is a single alternating
chain. Both keep advancing (entry touch, then wick-based stop and
wick-based target — an intrabar touch, not a candle close) every
subsequent candle regardless of which tracking phase the detector is in.

Detection is fully mechanical, replayed from the complete stored candle
history on every call rather than maintained as separately-persisted
incremental state — since the rules are deterministic and the full history
is already durable in Postgres, replaying it always reproduces the exact
same zone/state/age a hand-maintained state table would, without a second
source of truth to keep in sync across restarts.
"""
from __future__ import annotations

import asyncio

from sqlalchemy.ext.asyncio import AsyncSession

from app.db.database import AsyncSessionLocal
from app.services.repository import CandleRepository

TIMEFRAME = "1d"
# Confirmation move required, EACH leg (down-leg into a candidate, and the
# up-leg that confirms it away from it) — exposed as an overridable
# `swing_threshold` param on both scan_swing_zones/scan_swing_backtest
# (spec Rule 8: the threshold itself must be configurable/backtestable),
# this module constant is just the default when nothing else is passed.
SWING_THRESHOLD = 0.10
# Default risk:reward — SL/TP distance from Z as a fraction. Both scan_swing_
# zones and scan_swing_backtest accept sl_pct/tp_pct overrides (see the RR
# presets exposed in app/api/swing_strategy.py: 1:2 5%/10%, 1:5 2%/10%,
# matching the spec's own two-variant backtest matrix), so these are just
# what's used when nothing else is passed.
DEFAULT_TP_PCT = 0.10
DEFAULT_SL_PCT = 0.05
DEFAULT_MIN_VOLUME_USDT = 10_000_000.0
# An ARMED zone (confirmed but never retested) more than this many candles
# (= days, at TIMEFRAME="1d") old expires — keeps the dashboard from
# showing an unconfirmed setup that's sat untouched for months as if it
# were still a fresh, actionable watch item. Only affects ARMED zones —
# an already-TRIGGERED (open) trade has no age cap, and a resolved
# TP_HIT/SL_HIT is unaffected either way.
DEFAULT_MAX_ZONE_AGE = 30
LIVE_STATES = ("ARMED", "TRIGGERED")
DIRECTIONS = ("LONG", "SHORT")
# The backtest can carry thousands of historical trades — intraday-refining
# entry_time/exit_time for every one of them (2+ extra DB queries each)
# turned a full scan into ~35s. Only bother refining trades whose entry
# happened within this many candles (days) of the most recent one; older
# trades fall back to the coarser day-level timestamp (still shows the
# right day, just not the exact minute) rather than paying that cost for
# history nobody's actively re-checking.
BACKTEST_REFINE_RECENT_DAYS = 60
# A 1d candle's own open_time is always UTC midnight regardless of when
# within that day a moment (confirmation / entry touch / TP-SL resolution)
# actually happened — cross-referencing finer stored intervals narrows
# "which day" down to "which minute" wherever that data is available.
# Tried finest-first; falls through to a coarser tier only when a symbol
# has NO data at all for that interval on that day (5m/15m/30m coverage is
# less complete than 1h — see intervals.py), so most lookups resolve on
# the first, most precise tier without extra queries.
REFINE_CASCADE_INTERVALS = ["5m", "15m", "30m", "1h"]
DAY_MS = 86_400_000
# Each zone/trade needs up to ~6 sequential intraday-refinement queries
# (detected_at, triggered_at, resolved_at, anchor_time, candidate_time,
# zeroth_time), and running them one zone at a time — awaiting each query
# before starting the next — was the actual bottleneck (20-30s for a full
# scan), not the detection logic itself (which is pure in-memory Python and
# fast). scan_swing_zones/scan_swing_backtest now build every zone's row
# independently (each on its own short-lived DB session so they don't
# serialize on a single connection) and run them concurrently via
# asyncio.gather, bounded by this semaphore. Kept below the connection
# pool's pool_size (20, see app/db/database.py) so a full-concurrency scan
# still leaves headroom for other requests hitting the same pool at once.
REFINE_CONCURRENCY = 16


def _make_zone(
    anchor: dict, anchor_idx: int, extreme: dict, extreme_idx: int,
    pivot_type: str, move_pct: float, idx: int, confirm_price: float,
    sl_pct: float, tp_pct: float,
) -> dict:
    is_long = pivot_type == "low"
    # Zone level Z = the candidate candle's own extreme (its low for a BUY
    # zone, its high for a SELL zone) — not its close. This is the same
    # value as "swing_extreme" below; the two fields are kept separate in
    # the output because "swing_extreme" is documented/displayed as a
    # distinct reference column, even though they're now numerically equal.
    z = extreme["low"] if is_long else extreme["high"]
    rng = extreme["high"] - extreme["low"]
    body_ratio = abs(extreme["close"] - extreme["open"]) / rng if rng else 0.0
    return {
        "direction": "LONG" if is_long else "SHORT",
        "z": z,
        "state": "ARMED",
        "sl": z * (1 - sl_pct) if is_long else z * (1 + sl_pct),
        "tp": z * (1 + tp_pct) if is_long else z * (1 - tp_pct),
        "swing_extreme": z,
        "confirm_move_pct": move_pct,
        "confirm_price": confirm_price,  # the confirming candle's own close
        "body_ratio": body_ratio,
        "anchor_at": anchor_idx,          # candle index of the opposing extreme (diagram point 1)
        "anchor_price": anchor["high"] if is_long else anchor["low"],
        "candidate_at": extreme_idx,      # candle index of the swing low/high itself (diagram point 2)
        "confirmed_at": idx,              # candle index of the confirming candle (diagram point 3)
        "triggered_at": None,             # candle index of the entry touch (diagram point 4)
        "resolved_at": None,  # candle index where TP_HIT/SL_HIT/EXPIRED happened
    }


def _advance_zone(zone: dict | None, c: dict, idx: int, max_zone_age: int | None) -> None:
    """Advances one live trade (LONG or SHORT, mutated in place) using this
    candle — entry touch, then wick-based stop and wick-based target (an
    intrabar "price trades at/below the stop level" touch, not a candle
    close). No-op if there's no zone or it's already resolved."""
    if zone is None or zone["state"] not in LIVE_STATES:
        return
    is_long = zone["direction"] == "LONG"
    z, sl, tp = zone["z"], zone["sl"], zone["tp"]

    if is_long:
        if zone["state"] == "ARMED" and c["low"] <= z:
            zone["state"] = "TRIGGERED"
            zone["triggered_at"] = idx
        if zone["state"] == "TRIGGERED":
            if c["low"] <= sl:
                zone["state"] = "SL_HIT"
                zone["resolved_at"] = idx
            elif c["high"] >= tp:
                zone["state"] = "TP_HIT"
                zone["resolved_at"] = idx
        elif zone["state"] == "ARMED" and c["low"] <= sl:  # never touched — unreachable in practice (sl < z, so low > z already implies low > sl), kept for symmetry
            zone["state"] = "SL_HIT"
            zone["resolved_at"] = idx
    else:
        if zone["state"] == "ARMED" and c["high"] >= z:
            zone["state"] = "TRIGGERED"
            zone["triggered_at"] = idx
        if zone["state"] == "TRIGGERED":
            if c["high"] >= sl:
                zone["state"] = "SL_HIT"
                zone["resolved_at"] = idx
            elif c["low"] <= tp:
                zone["state"] = "TP_HIT"
                zone["resolved_at"] = idx
        elif zone["state"] == "ARMED" and c["high"] >= sl:  # unreachable in practice, mirrors the LONG side above
            zone["state"] = "SL_HIT"
            zone["resolved_at"] = idx

    if (
        zone["state"] == "ARMED" and max_zone_age is not None
        and idx - zone["confirmed_at"] > max_zone_age
    ):
        zone["state"] = "EXPIRED"
        zone["resolved_at"] = idx


def _run_chain_history(
    candles: list[dict], max_zone_age: int | None,
    sl_pct: float = DEFAULT_SL_PCT, tp_pct: float = DEFAULT_TP_PCT,
    swing_threshold: float = SWING_THRESHOLD,
) -> list[dict]:
    """Replays one coin's full candle history once as a SINGLE alternating
    low/high chain and returns EVERY zone ever confirmed on EITHER side (not
    just the current live ones) — each dict's "state" reflects wherever its
    lifecycle actually ended up (TP_HIT, SL_HIT, EXPIRED, or still
    ARMED/TRIGGERED if it's the side's last one and still live). LONG and
    SHORT each track their own independent live trade even though the
    underlying swing-point detection is a single alternating chain — see
    module docstring for the full state-machine rules."""
    n = len(candles)
    if n < 2:
        return []

    mode: str | None = None  # None (bootstrap) | "tracking_low" | "tracking_high"
    boot_peak, boot_peak_idx = candles[0], 0
    boot_trough, boot_trough_idx = candles[0], 0
    anchor: dict | None = None
    anchor_idx: int | None = None
    candidate: dict | None = None
    candidate_idx: int | None = None
    primed = False  # has the leg INTO the current candidate reached swing_threshold?
    long_zone: dict | None = None
    short_zone: dict | None = None
    history: list[dict] = []

    def leg_into_candidate_pct() -> float:
        # % move from the fixed anchor into the current candidate — the
        # candidate only becomes eligible for confirmation once this leg
        # ITSELF is >= swing_threshold (the anchor must be a genuine
        # qualifying peak/trough relative to this candidate, not just
        # whatever the most recent extreme happened to be).
        if mode == "tracking_low":
            ref = anchor["high"]
            return (ref - candidate["low"]) / ref if ref else 0.0
        ref = anchor["low"]
        return (candidate["high"] - ref) / ref if ref else 0.0

    for idx in range(1, n):
        c = candles[idx]

        # 0. Advance both sides' current live trade using this candle —
        #    independent of whichever tracking phase the detector is in
        #    below, and independent of each other. Only replaced once the
        #    matching side's NEXT swing point actually confirms (steps 2/3).
        _advance_zone(long_zone, c, idx, max_zone_age)
        _advance_zone(short_zone, c, idx, max_zone_age)

        # 1. Bootstrap — no swing point has ever been confirmed yet, so
        #    there's no fixed anchor to track from. Track the running peak
        #    AND running trough simultaneously; whichever leg reaches
        #    SWING_THRESHOLD first fixes the opposite extreme as the
        #    anchor and commits the detector to that direction — that leg
        #    IS the anchor->candidate leg, so the very first candidate is
        #    already primed the moment a mode is chosen. A candle that
        #    just became the new running peak/trough can't also be the
        #    move that crosses the threshold off itself (same-day
        #    exclusion), same as the checks below.
        if mode is None:
            if c["high"] > boot_peak["high"]:
                boot_peak, boot_peak_idx = c, idx
            if c["low"] < boot_trough["low"]:
                boot_trough, boot_trough_idx = c, idx
            fall_pct = (boot_peak["high"] - c["low"]) / boot_peak["high"] if boot_peak["high"] else 0.0
            rise_pct = (c["high"] - boot_trough["low"]) / boot_trough["low"] if boot_trough["low"] else 0.0
            if fall_pct >= swing_threshold and boot_peak_idx != idx:
                mode = "tracking_low"
                anchor, anchor_idx = boot_peak, boot_peak_idx
                candidate, candidate_idx = c, idx
                primed = True
            elif rise_pct >= swing_threshold and boot_trough_idx != idx:
                mode = "tracking_high"
                anchor, anchor_idx = boot_trough, boot_trough_idx
                candidate, candidate_idx = c, idx
                primed = True
            continue

        # 2. TRACKING_LOW — anchor (a swing high) is fixed; the candidate
        #    (running low) starts completely fresh after a flip (not
        #    seeded from the candle that just confirmed the prior swing
        #    high) and updates to a strictly lower low each time (Rule
        #    4.1), re-checking "primed" against it. It only becomes
        #    eligible for confirmation once SOME later candle's low is
        #    itself >= swing_threshold below the anchor — a shallow dip
        #    that never gets that far never counts as a candidate at all,
        #    however long the detector waits for one that does. Once
        #    primed, confirmation is wick-based: a LATER candle's HIGH
        #    recovering >= threshold above the candidate's own low.
        if mode == "tracking_low":
            if candidate is None or c["low"] < candidate["low"]:
                candidate, candidate_idx = c, idx
                primed = leg_into_candidate_pct() >= swing_threshold
                continue
            if primed:
                cand_ref = candidate["low"]
                move_pct = (c["close"] - cand_ref) / cand_ref if cand_ref else 0.0
                if cand_ref and move_pct >= swing_threshold:
                    new_zone = _make_zone(
                        anchor, anchor_idx, candidate, candidate_idx,
                        "low", move_pct, idx, c["close"], sl_pct, tp_pct,
                    )
                    # "0 candle" (diagram point 0) — the prior extreme that
                    # validated THIS zone's own anchor (point 1) via its own
                    # qualifying leg. That's exactly the anchor of whichever
                    # zone confirmed immediately before this one in the chain.
                    if history:
                        new_zone["zeroth_at"] = history[-1]["anchor_at"]
                        new_zone["zeroth_price"] = history[-1]["anchor_price"]
                    else:
                        new_zone["zeroth_at"] = None
                        new_zone["zeroth_price"] = None
                    history.append(new_zone)
                    long_zone = new_zone
                    anchor, anchor_idx = candidate, candidate_idx
                    mode = "tracking_high"
                    # Start the next candidate completely fresh — do NOT
                    # reuse this confirming candle's own high, since it's
                    # by definition already >= threshold above the new
                    # anchor (that's what confirmation just required) and
                    # would make the new phase's "primed" gate a no-op.
                    candidate, candidate_idx = None, None
                    primed = False
            continue

        # 3. TRACKING_HIGH — the mirror. A confirmed swing high arms a
        #    tradeable SHORT zone the same way a swing low arms LONG.
        if mode == "tracking_high":
            if candidate is None or c["high"] > candidate["high"]:
                candidate, candidate_idx = c, idx
                primed = leg_into_candidate_pct() >= swing_threshold
                continue
            if primed:
                cand_ref = candidate["high"]
                move_pct = (cand_ref - c["close"]) / cand_ref if cand_ref else 0.0
                if cand_ref and move_pct >= swing_threshold:
                    new_zone = _make_zone(
                        anchor, anchor_idx, candidate, candidate_idx,
                        "high", move_pct, idx, c["close"], sl_pct, tp_pct,
                    )
                    # Same "0 candle" derivation as the tracking_low branch above.
                    if history:
                        new_zone["zeroth_at"] = history[-1]["anchor_at"]
                        new_zone["zeroth_price"] = history[-1]["anchor_price"]
                    else:
                        new_zone["zeroth_at"] = None
                        new_zone["zeroth_price"] = None
                    history.append(new_zone)
                    short_zone = new_zone
                    anchor, anchor_idx = candidate, candidate_idx
                    mode = "tracking_low"
                    # Same fresh-start reset as the tracking_low->tracking_high flip above.
                    candidate, candidate_idx = None, None
                    primed = False
            continue

    return history


def _run_live(
    candles: list[dict], max_zone_age: int | None,
    sl_pct: float = DEFAULT_SL_PCT, tp_pct: float = DEFAULT_TP_PCT,
    swing_threshold: float = SWING_THRESHOLD,
) -> dict:
    """Live-dashboard view: the most recent zone on EACH side, whatever its
    state — ARMED/TRIGGERED (still live) or TP_HIT/SL_HIT/EXPIRED
    (completed) — so a coin's last completed outcome keeps showing on the
    dashboard until a new zone confirms on that side, instead of vanishing
    the moment it resolves. Returns {"LONG": zone_or_None, "SHORT": zone_or_None}."""
    history = _run_chain_history(candles, max_zone_age, sl_pct, tp_pct, swing_threshold)
    result = {"LONG": None, "SHORT": None}
    for zone in history:
        result[zone["direction"]] = zone
    return result


async def _fetch_finest_available_rows(
    db: AsyncSession, symbol: str, market: str, day_open_time: int,
) -> list[dict]:
    """Tries REFINE_CASCADE_INTERVALS finest-first for this one day and
    returns whichever tier actually has stored data covering it — a symbol
    missing 5m history for that day falls through to 15m, then 30m, then
    1h, rather than failing to refine at all."""
    for interval in REFINE_CASCADE_INTERVALS:
        rows = await CandleRepository.get_ohlc_in_range(
            db, symbol=symbol, interval=interval, market=market,
            start_time=day_open_time, end_time=day_open_time + DAY_MS,
        )
        if rows:
            return rows
    return []


async def _refine_moment(
    db: AsyncSession, symbol: str, market: str, day_open_time: int, predicate,
) -> int:
    """Narrows a 1d candle's own open_time (always UTC midnight) down to
    the actual moment that day some condition (`predicate`, given a finer
    candle row, returns True/False) first held — using whichever finer
    interval tier is actually available (see _fetch_finest_available_rows).
    Falls back to the day's own open_time if no finer data covers that day,
    or none of it satisfies the predicate (can happen when what actually
    crossed the threshold is a wick the finer interval doesn't capture, or
    on a still-forming day's most recent, not-yet-elapsed candles)."""
    rows = await _fetch_finest_available_rows(db, symbol, market, day_open_time)
    for row in rows:
        if predicate(row):
            return row["open_time"]
    return day_open_time


def _confirm_predicate(reference: float, direction: str, swing_threshold: float):
    # Confirmation is close-based (not wick) — the recovery/decline must
    # actually hold through the candle's close, not just wick past it.
    if not reference:
        return lambda row: False
    if direction == "LONG":
        return lambda row: (row["close"] - reference) / reference >= swing_threshold
    return lambda row: (reference - row["close"]) / reference >= swing_threshold


def _touch_predicate(z: float, direction: str):
    if direction == "LONG":
        return lambda row: row["low"] <= z
    return lambda row: row["high"] >= z


async def _refine_extreme_moment(
    db: AsyncSession, symbol: str, market: str, day_open_time: int, seeking_high: bool,
) -> int:
    """Pinpoints WHEN during a day its own extreme (the opposing-side
    anchor, or the swing low/high candidate) was actually printed — using
    whichever finer interval tier is available, picking the row with the
    max high (seeking_high=True) or min low (seeking_high=False). Falls
    back to the day's own open_time if no finer data covers it."""
    rows = await _fetch_finest_available_rows(db, symbol, market, day_open_time)
    if not rows:
        return day_open_time
    best = max(rows, key=lambda r: r["high"]) if seeking_high else min(rows, key=lambda r: r["low"])
    return best["open_time"]


def _resolution_predicate(sl: float, tp: float, direction: str, outcome: str):
    """Checks whichever condition matches the ALREADY-KNOWN 1d-level
    outcome (TP_HIT or SL_HIT) — not re-derived at finer granularity, so
    the refined moment can't disagree with the daily-level determination
    of how the trade actually ended."""
    is_long = direction == "LONG"
    if outcome == "TP_HIT":
        return (lambda row: row["high"] >= tp) if is_long else (lambda row: row["low"] <= tp)
    return (lambda row: row["low"] <= sl) if is_long else (lambda row: row["high"] >= sl)


async def _build_zone_row(
    symbol: str, direction: str, zone: dict, candles: list[dict],
    price: float, latest_idx: int, market: str, swing_threshold: float,
) -> dict:
    """Builds one live-dashboard row for a single zone, including every
    intraday-refinement query it needs — on its own short-lived DB session
    so many of these can run truly concurrently (see scan_swing_zones)
    instead of serializing one at a time on a single shared connection."""
    async with AsyncSessionLocal() as db:
        day_open_time = candles[zone["confirmed_at"]]["open_time"]
        reference = zone["swing_extreme"]
        # Only refine the confirmation moment when that day has already
        # fully closed — if it confirmed off the still-forming latest
        # candle (the same-day self-confirmation case), `reference` is
        # itself only known because the day is aggregated so far, so
        # checking it against that same day's earlier minutes/hours
        # would be look-ahead bias (an early moment's high compared
        # against a low that, in real time, hadn't happened yet).
        # Falls back to the day's own open_time in that case. Entry
        # touch and TP/SL resolution have no such circularity (Z/SL/TP
        # are already fixed from an earlier day), so those always refine.
        if zone["confirmed_at"] < latest_idx:
            detected_at = await _refine_moment(db, symbol, market, day_open_time, _confirm_predicate(reference, direction, swing_threshold))
        else:
            detected_at = day_open_time

        triggered_at = None
        if zone["triggered_at"] is not None:
            trig_day = candles[zone["triggered_at"]]["open_time"]
            triggered_at = await _refine_moment(db, symbol, market, trig_day, _touch_predicate(zone["z"], direction))

        resolved_at = None
        if zone["resolved_at"] is not None and zone["state"] in ("TP_HIT", "SL_HIT"):
            resolved_day = candles[zone["resolved_at"]]["open_time"]
            resolved_at = await _refine_moment(
                db, symbol, market, resolved_day,
                _resolution_predicate(zone["sl"], zone["tp"], direction, zone["state"]),
            )
        elif zone["resolved_at"] is not None:
            resolved_at = candles[zone["resolved_at"]]["open_time"]  # EXPIRED — no finer condition to refine against

        # Anchor (diagram point 1) and candidate (point 2, the swing
        # low/high itself) both sit on already-elapsed days relative to
        # confirmation, so no look-ahead-bias concern refining either —
        # unlike detected_at above, these always refine. For LONG the
        # anchor is a peak (seeking its high) and the candidate is a
        # trough (seeking its low); for SHORT it's the reverse.
        anchor_day = candles[zone["anchor_at"]]["open_time"]
        anchor_time = await _refine_extreme_moment(db, symbol, market, anchor_day, seeking_high=(direction == "LONG"))
        candidate_day = candles[zone["candidate_at"]]["open_time"]
        candidate_time = await _refine_extreme_moment(db, symbol, market, candidate_day, seeking_high=(direction == "SHORT"))

        # "0 candle" (diagram point 0) — the prior extreme that
        # validated this zone's own anchor. None for the very first
        # zone ever detected for this coin (nothing before the start
        # of stored history to validate against). Opposite seeking_high
        # from this zone's own anchor, since it's the anchor of the
        # OPPOSITE-direction zone that came immediately before it.
        zeroth_time = None
        if zone.get("zeroth_at") is not None:
            zeroth_day = candles[zone["zeroth_at"]]["open_time"]
            zeroth_time = await _refine_extreme_moment(db, symbol, market, zeroth_day, seeking_high=(direction == "SHORT"))

    return {
        "symbol": symbol,
        "direction": direction,
        "state": zone["state"],
        "zeroth_time": zeroth_time,       # diagram point 0 — the prior extreme validating the anchor
        "zeroth_price": zone.get("zeroth_price"),
        "anchor_time": anchor_time,      # diagram point 1 — the opposing extreme
        "anchor_price": zone["anchor_price"],
        "candidate_time": candidate_time,  # diagram point 2 — the swing low/high (Z's candle)
        "confirm_price": zone["confirm_price"],  # diagram point 3's close (the confirming candle)
        "entry_price": zone["z"],          # diagram point 4 — always exactly Z
        "z": zone["z"],
        "price": price,
        "distance_pct": (price - zone["z"]) / zone["z"] * 100,
        "sl": zone["sl"],
        "tp": zone["tp"],
        "confirm_move_pct": zone["confirm_move_pct"] * 100,
        "zone_age": latest_idx - zone["confirmed_at"],
        "detected_at": detected_at,   # ms epoch when ARMED (confirmed) — narrowed to the actual hour where possible
        "triggered_at": triggered_at, # ms epoch when TRIGGERED (entry touch), or None if never touched
        "resolved_at": resolved_at,   # ms epoch when TP_HIT/SL_HIT/EXPIRED, or None if still live
        "body_ratio": zone["body_ratio"],
        "swing_extreme": zone["swing_extreme"],
    }


async def scan_swing_zones(
    db: AsyncSession,
    market: str = "futures",
    min_volume_usdt: float = DEFAULT_MIN_VOLUME_USDT,
    max_zone_age: int | None = DEFAULT_MAX_ZONE_AGE,
    sl_pct: float = DEFAULT_SL_PCT,
    tp_pct: float = DEFAULT_TP_PCT,
    swing_threshold: float = SWING_THRESHOLD,
) -> dict:
    """Scans every coin with stored 1d candles, keeps the ones whose latest
    day's approximate USD volume (volume * close — 1d candle volume already
    IS the day's total, so no separate 24h lookback is needed) is >=
    min_volume_usdt, and returns one dashboard row per currently-live
    long/short zone, sorted by absolute distance to Z ascending (the
    actionable watchlist first). Detection (pure in-memory Python, fast) and
    the intraday-refinement DB queries (the actual slow part) are split into
    two phases — every zone's refinement runs concurrently, bounded by
    REFINE_CONCURRENCY, instead of one zone at a time."""
    candles_by_symbol = await CandleRepository.get_ohlc_by_symbol(db, interval=TIMEFRAME, market=market)

    pending = []
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

        live = _run_live(candles, max_zone_age, sl_pct, tp_pct, swing_threshold)
        for direction in DIRECTIONS:
            zone = live[direction]
            if zone is None:
                continue
            pending.append((symbol, direction, zone, candles, price, latest_idx))

    semaphore = asyncio.Semaphore(REFINE_CONCURRENCY)

    async def _bounded(args):
        async with semaphore:
            return await _build_zone_row(*args, market=market, swing_threshold=swing_threshold)

    rows = list(await asyncio.gather(*(_bounded(item) for item in pending)))
    rows.sort(key=lambda r: abs(r["distance_pct"]))

    return {
        "market": market,
        "timeframe": TIMEFRAME,
        "min_volume_usdt": min_volume_usdt,
        "swing_threshold": swing_threshold,
        "tp_pct": tp_pct,
        "sl_pct": sl_pct,
        "symbols_scanned": symbols_scanned,
        "zones": rows,
    }


async def _build_trade(
    symbol: str, zone: dict, candles: list[dict], latest_idx: int,
    market: str, tp_pct: float, sl_pct: float, swing_threshold: float,
) -> dict:
    """Builds one backtest trade row, including its intraday-refinement
    queries when it's recent enough to warrant them — on its own
    short-lived DB session (only opened when actually needed) so many of
    these can run concurrently (see scan_swing_backtest)."""
    direction = zone["direction"]
    if zone["state"] == "TP_HIT":
        result, exit_price, gain_pct = "WIN", zone["tp"], tp_pct * 100
    elif zone["state"] == "SL_HIT":
        result, exit_price, gain_pct = "LOSS", zone["sl"], -sl_pct * 100
    else:  # still TRIGGERED, unresolved
        result, exit_price, gain_pct = "OPEN", None, None

    # Same intraday refinement as the live dashboard (see scan_swing_zones),
    # but only for recent trades — refining all of them was the original
    # bottleneck. Older trades keep the coarser day-level timestamp (still
    # the correct day, just not the exact minute) and skip DB work entirely.
    is_recent = (latest_idx - zone["triggered_at"]) <= BACKTEST_REFINE_RECENT_DAYS
    entry_day = candles[zone["triggered_at"]]["open_time"]
    confirm_day = candles[zone["confirmed_at"]]["open_time"]
    reference = zone["swing_extreme"]
    zeroth_price = zone.get("zeroth_price")

    if not is_recent:
        entry_time = entry_day
        exit_time = (
            candles[zone["resolved_at"]]["open_time"] if zone["resolved_at"] is not None else None
        )
        detected_at = confirm_day
        anchor_time = candles[zone["anchor_at"]]["open_time"]
        candidate_time = candles[zone["candidate_at"]]["open_time"]
        zeroth_time = (
            candles[zone["zeroth_at"]]["open_time"] if zone.get("zeroth_at") is not None else None
        )
    else:
        async with AsyncSessionLocal() as db:
            entry_time = await _refine_moment(db, symbol, market, entry_day, _touch_predicate(zone["z"], direction))

            if zone["resolved_at"] is not None and zone["state"] in ("TP_HIT", "SL_HIT"):
                exit_day = candles[zone["resolved_at"]]["open_time"]
                exit_time = await _refine_moment(
                    db, symbol, market, exit_day,
                    _resolution_predicate(zone["sl"], zone["tp"], direction, zone["state"]),
                )
            elif zone["resolved_at"] is not None:
                exit_time = candles[zone["resolved_at"]]["open_time"]  # EXPIRED — no finer condition to refine against
            else:
                exit_time = None

            # "0 candle" (diagram point 0) — same derivation as the live
            # dashboard. None for a coin's very first zone ever (no prior
            # extreme exists before the start of stored history).
            zeroth_time = None
            if zone.get("zeroth_at") is not None:
                zeroth_day = candles[zone["zeroth_at"]]["open_time"]
                zeroth_time = await _refine_extreme_moment(db, symbol, market, zeroth_day, seeking_high=(direction == "SHORT"))

            # Same 4-point timeline as the live dashboard (see
            # scan_swing_zones): anchor (point 1), candidate/Z (point 2),
            # confirmation (point 3).
            detected_at = await _refine_moment(db, symbol, market, confirm_day, _confirm_predicate(reference, direction, swing_threshold))
            anchor_day = candles[zone["anchor_at"]]["open_time"]
            anchor_time = await _refine_extreme_moment(db, symbol, market, anchor_day, seeking_high=(direction == "LONG"))
            candidate_day = candles[zone["candidate_at"]]["open_time"]
            candidate_time = await _refine_extreme_moment(db, symbol, market, candidate_day, seeking_high=(direction == "SHORT"))

    return {
        "symbol": symbol,
        "direction": direction,
        "z": zone["z"],
        "sl": zone["sl"],
        "tp": zone["tp"],
        "zeroth_time": zeroth_time,
        "zeroth_price": zeroth_price,
        "anchor_time": anchor_time,
        "anchor_price": zone["anchor_price"],
        "candidate_time": candidate_time,
        "detected_at": detected_at,
        "confirm_price": zone["confirm_price"],
        "entry_time": entry_time,
        "exit_time": exit_time,
        "entry_price": zone["z"],
        "exit_price": exit_price,
        "result": result,
        "duration_ms": (exit_time - entry_time) if exit_time is not None else None,
        "gain_pct": gain_pct,
    }


async def scan_swing_backtest(
    db: AsyncSession,
    market: str = "futures",
    min_volume_usdt: float = DEFAULT_MIN_VOLUME_USDT,
    max_zone_age: int | None = DEFAULT_MAX_ZONE_AGE,
    sl_pct: float = DEFAULT_SL_PCT,
    tp_pct: float = DEFAULT_TP_PCT,
    swing_threshold: float = SWING_THRESHOLD,
) -> dict:
    """Scans every coin with stored 1d candles (same $-volume filter as
    scan_swing_zones) and returns every historical trade that actually had
    an entry — a zone that got TRIGGERED at some point — resolved as WIN
    (TP_HIT), LOSS (SL_HIT), or still OPEN (TRIGGERED, unresolved). Zones
    that confirmed but were never retested/entered aren't trades and are
    excluded. Entry is always exactly Z and exit is always exactly TP or
    SL (this strategy has no slippage/partial-fill concept), so every
    WIN/LOSS has the identical fixed PnL% (+tp_pct / -sl_pct) — only which
    coin and when differ. Sorted oldest entry first, matching a backtest
    trade log rather than the live dashboard's distance-to-Z ordering.
    Detection (fast, in-memory) and refinement (the slow DB-bound part) are
    split into two phases the same way as scan_swing_zones — every trade's
    refinement runs concurrently, bounded by REFINE_CONCURRENCY."""
    candles_by_symbol = await CandleRepository.get_ohlc_by_symbol(db, interval=TIMEFRAME, market=market)

    pending = []
    symbols_scanned = 0
    for symbol, candles in candles_by_symbol.items():
        if len(candles) < 2:
            continue
        latest = candles[-1]
        approx_volume_usdt = latest["volume"] * latest["close"]
        if approx_volume_usdt < min_volume_usdt:
            continue
        symbols_scanned += 1
        latest_idx = len(candles) - 1

        for zone in _run_chain_history(candles, max_zone_age, sl_pct, tp_pct, swing_threshold):
            if zone["triggered_at"] is None:
                continue  # never retested/entered — not a trade
            pending.append((symbol, zone, candles, latest_idx))

    semaphore = asyncio.Semaphore(REFINE_CONCURRENCY)

    async def _bounded(args):
        async with semaphore:
            return await _build_trade(*args, market=market, tp_pct=tp_pct, sl_pct=sl_pct, swing_threshold=swing_threshold)

    trades = list(await asyncio.gather(*(_bounded(item) for item in pending)))
    trades.sort(key=lambda t: t["entry_time"])

    return {
        "market": market,
        "timeframe": TIMEFRAME,
        "min_volume_usdt": min_volume_usdt,
        "swing_threshold": swing_threshold,
        "tp_pct": tp_pct,
        "sl_pct": sl_pct,
        "symbols_scanned": symbols_scanned,
        "trades": trades,
    }
