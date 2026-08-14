"""
Swing Zone Retest Strategy — detects percentage-confirmed swing points on
each coin's 1d candles, projects them forward as a price zone, and reports
which coins currently have a live (ARMED or TRIGGERED) LONG zone near price.

Rewritten to match the formal "Swing High & Low Strategy Specification"
(v1.0): a SINGLE alternating state machine, not two independent long/short
passes — a confirmed swing low is always followed by tracking toward a
swing high, then back to tracking a low, and so on:

  1. SEARCHING (bootstrap only, until the very first swing point exists):
     track the running peak and running trough from the start of history;
     whichever leg (fall from the running peak, or rise from the running
     trough) reaches SWING_THRESHOLD first decides the initial direction
     and fixes the opposite extreme as the anchor going forward.

  2. TRACKING_LOW: the anchor (a swing high — either the previous CONFIRMED
     swing high, or the bootstrap peak) is FIXED; it does not move again
     just because a new intraday peak appears while we're in this phase.
     Track the running trough since the anchor as the CANDIDATE swing low
     — each new lower low replaces the candidate (Rule 4.1). The moment a
     later candle's HIGH (wick, not close) recovers >= SWING_THRESHOLD
     above the candidate's own low, the candidate is CONFIRMED as a swing
     low: Z = candidate.low, a tradeable LONG zone arms, and the state
     flips to TRACKING_HIGH with the anchor now fixed at this candidate.

  3. TRACKING_HIGH: the mirror of the above (candidate = running peak,
     confirmed by a later candle's LOW falling >= SWING_THRESHOLD below
     the candidate's own high). A confirmed swing high is structural only
     — per the spec, only LONG trades are taken; a swing high just fixes
     the next anchor and flips the state back to TRACKING_LOW. It is not
     returned as a tradeable zone.

A later candle can never confirm a candidate off itself in the same
iteration that just extended it (same-day exclusion / anti-look-ahead) —
enforced by `continue`-ing immediately after a candidate update.

Once a LONG zone confirms it becomes the one live trade, and keeps
advancing (entry touch, then wick-based stop and wick-based target — the
spec's "price trades at/below the stop level" wording means an intrabar
touch, not a candle close) on every subsequent candle regardless of which
tracking phase the detector is in — it's only replaced once the NEXT
swing low actually confirms, matching the spec's "pending order is
cancelled/moved when a new swing low confirms" rule.

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
DEFAULT_MIN_VOLUME_USDT = 5_000_000.0
# An ARMED zone (confirmed but never retested) more than this many candles
# (= days, at TIMEFRAME="1d") old expires — keeps the dashboard from
# showing an unconfirmed setup that's sat untouched for months as if it
# were still a fresh, actionable watch item. Only affects ARMED zones —
# an already-TRIGGERED (open) trade has no age cap, and a resolved
# TP_HIT/SL_HIT is unaffected either way.
DEFAULT_MAX_ZONE_AGE = 30
LIVE_STATES = ("ARMED", "TRIGGERED")
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


def _make_zone(
    anchor: dict, anchor_idx: int, extreme: dict, extreme_idx: int,
    move_pct: float, idx: int, confirm_price: float,
    sl_pct: float, tp_pct: float,
) -> dict:
    # Zone level Z = the candidate candle's own low — not its close. This is
    # the same value as "swing_extreme" below; the two fields are kept
    # separate in the output because "swing_extreme" is documented/
    # displayed as a distinct reference column, even though they're now
    # numerically equal.
    z = extreme["low"]
    rng = extreme["high"] - extreme["low"]
    body_ratio = abs(extreme["close"] - extreme["open"]) / rng if rng else 0.0
    return {
        "z": z,
        "state": "ARMED",
        "sl": z * (1 - sl_pct),
        "tp": z * (1 + tp_pct),
        "swing_extreme": z,
        "confirm_move_pct": move_pct,
        "confirm_price": confirm_price,  # the confirming candle's own close
        "body_ratio": body_ratio,
        "anchor_at": anchor_idx,          # candle index of the swing high anchor (diagram point 1)
        "anchor_price": anchor["high"],
        "candidate_at": extreme_idx,      # candle index of the swing low itself (diagram point 2)
        "confirmed_at": idx,              # candle index of the confirming candle (diagram point 3)
        "triggered_at": None,             # candle index of the entry touch (diagram point 4)
        "resolved_at": None,  # candle index where TP_HIT/SL_HIT/EXPIRED happened
    }


def _run_chain_history(
    candles: list[dict], max_zone_age: int | None,
    sl_pct: float = DEFAULT_SL_PCT, tp_pct: float = DEFAULT_TP_PCT,
    swing_threshold: float = SWING_THRESHOLD,
) -> list[dict]:
    """Replays one coin's full candle history once as a SINGLE alternating
    low/high chain and returns EVERY LONG zone ever confirmed (not just the
    current live one) — each dict's "state" reflects wherever its lifecycle
    actually ended up (TP_HIT, SL_HIT, EXPIRED, or still ARMED/TRIGGERED if
    it's the last one and still live). Swing highs are tracked internally
    (they fix the next anchor and flip the direction being tracked) but are
    never returned as their own zone — per the spec, only long trades are
    taken. See module docstring for the full state-machine rules."""
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
    zone: dict | None = None
    history: list[dict] = []

    for idx in range(1, n):
        c = candles[idx]

        # 0. Advance the current live LONG trade using this candle — entry
        #    touch, then wick-based stop and wick-based target (an intrabar
        #    "price trades at/below the stop level" touch, not a candle
        #    close) — independent of whichever tracking phase the detector
        #    is in below. Only replaced once the NEXT swing low actually
        #    confirms (step 2).
        if zone is not None and zone["state"] in LIVE_STATES:
            z, sl, tp = zone["z"], zone["sl"], zone["tp"]
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

            if (
                zone["state"] == "ARMED" and max_zone_age is not None
                and idx - zone["confirmed_at"] > max_zone_age
            ):
                zone["state"] = "EXPIRED"
                zone["resolved_at"] = idx

        # 1. Bootstrap — no swing point has ever been confirmed yet, so
        #    there's no fixed anchor to track from. Track the running peak
        #    AND running trough simultaneously; whichever leg reaches
        #    SWING_THRESHOLD first fixes the opposite extreme as the
        #    anchor and commits the detector to that direction. A candle
        #    that just became the new running peak/trough can't also be
        #    the move that crosses the threshold off itself (same-day
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
            elif rise_pct >= swing_threshold and boot_trough_idx != idx:
                mode = "tracking_high"
                anchor, anchor_idx = boot_trough, boot_trough_idx
                candidate, candidate_idx = c, idx
            continue

        # 2. TRACKING_LOW — anchor (a swing high) is fixed; only the
        #    candidate (running low) updates, always to a strictly lower
        #    low (Rule 4.1). Confirmation is wick-based: a LATER candle's
        #    HIGH recovering >= threshold above the candidate's own low.
        if mode == "tracking_low":
            if c["low"] < candidate["low"]:
                candidate, candidate_idx = c, idx
                continue
            cand_ref = candidate["low"]
            move_pct = (c["high"] - cand_ref) / cand_ref if cand_ref else 0.0
            if cand_ref and move_pct >= swing_threshold:
                new_zone = _make_zone(
                    anchor, anchor_idx, candidate, candidate_idx,
                    move_pct, idx, c["close"], sl_pct, tp_pct,
                )
                history.append(new_zone)
                zone = new_zone
                anchor, anchor_idx = candidate, candidate_idx
                mode = "tracking_high"
                # Seed the next candidate (running peak) with this same
                # confirming candle rather than None — it's the first
                # data point available after the flip, same idea as the
                # bootstrap phase picking a starting reference.
                candidate, candidate_idx = c, idx
            continue

        # 3. TRACKING_HIGH — the mirror. Confirmed swing highs are
        #    structural only (fix the next anchor, flip direction) — never
        #    returned as their own tradeable zone.
        if mode == "tracking_high":
            if c["high"] > candidate["high"]:
                candidate, candidate_idx = c, idx
                continue
            cand_ref = candidate["high"]
            move_pct = (cand_ref - c["low"]) / cand_ref if cand_ref else 0.0
            if cand_ref and move_pct >= swing_threshold:
                anchor, anchor_idx = candidate, candidate_idx
                mode = "tracking_low"
                # Same seeding as the tracking_low->tracking_high flip above.
                candidate, candidate_idx = c, idx
            continue

    return history


def _run_live(
    candles: list[dict], max_zone_age: int | None,
    sl_pct: float = DEFAULT_SL_PCT, tp_pct: float = DEFAULT_TP_PCT,
    swing_threshold: float = SWING_THRESHOLD,
) -> dict | None:
    """Live-dashboard view: the most recent LONG zone, whatever its state —
    ARMED/TRIGGERED (still live) or TP_HIT/SL_HIT/EXPIRED (completed) — so
    a coin's last completed outcome keeps showing on the dashboard until a
    new zone confirms, instead of vanishing the moment it resolves."""
    history = _run_chain_history(candles, max_zone_age, sl_pct, tp_pct, swing_threshold)
    return history[-1] if history else None


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


def _confirm_predicate(reference: float, swing_threshold: float):
    if not reference:
        return lambda row: False
    return lambda row: (row["high"] - reference) / reference >= swing_threshold


def _touch_predicate(z: float):
    return lambda row: row["low"] <= z


async def _refine_extreme_moment(
    db: AsyncSession, symbol: str, market: str, day_open_time: int, seeking_high: bool,
) -> int:
    """Pinpoints WHEN during a day its own extreme (the swing high anchor,
    or the swing low candidate) was actually printed — using whichever
    finer interval tier is available, picking the row with the max high
    (seeking_high=True) or min low (seeking_high=False). Falls back to the
    day's own open_time if no finer data covers it."""
    rows = await _fetch_finest_available_rows(db, symbol, market, day_open_time)
    if not rows:
        return day_open_time
    best = max(rows, key=lambda r: r["high"]) if seeking_high else min(rows, key=lambda r: r["low"])
    return best["open_time"]


def _resolution_predicate(sl: float, tp: float, outcome: str):
    """Checks whichever condition matches the ALREADY-KNOWN 1d-level
    outcome (TP_HIT or SL_HIT) — not re-derived at finer granularity, so
    the refined moment can't disagree with the daily-level determination
    of how the trade actually ended."""
    if outcome == "TP_HIT":
        return lambda row: row["high"] >= tp
    return lambda row: row["low"] <= sl


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
    min_volume_usdt, and returns one dashboard row per currently-live LONG
    zone, sorted by absolute distance to Z ascending (the actionable
    watchlist first)."""
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

        zone = _run_live(candles, max_zone_age, sl_pct, tp_pct, swing_threshold)
        if zone is None:
            continue
        day_open_time = candles[zone["confirmed_at"]]["open_time"]
        reference = zone["swing_extreme"]
        # Only refine the confirmation moment when that day has already
        # fully closed — if it confirmed off the still-forming latest
        # candle (the same-day self-confirmation case), `reference` is
        # itself only known because the day is aggregated so far, so
        # checking it against that same day's earlier minutes/hours would
        # be look-ahead bias (an early moment's high compared against a
        # low that, in real time, hadn't happened yet). Falls back to the
        # day's own open_time in that case. Entry touch and TP/SL
        # resolution have no such circularity (Z/SL/TP are already fixed
        # from an earlier day), so those always refine.
        if zone["confirmed_at"] < latest_idx:
            detected_at = await _refine_moment(db, symbol, market, day_open_time, _confirm_predicate(reference, swing_threshold))
        else:
            detected_at = day_open_time

        triggered_at = None
        if zone["triggered_at"] is not None:
            trig_day = candles[zone["triggered_at"]]["open_time"]
            triggered_at = await _refine_moment(db, symbol, market, trig_day, _touch_predicate(zone["z"]))

        resolved_at = None
        if zone["resolved_at"] is not None and zone["state"] in ("TP_HIT", "SL_HIT"):
            resolved_day = candles[zone["resolved_at"]]["open_time"]
            resolved_at = await _refine_moment(
                db, symbol, market, resolved_day,
                _resolution_predicate(zone["sl"], zone["tp"], zone["state"]),
            )
        elif zone["resolved_at"] is not None:
            resolved_at = candles[zone["resolved_at"]]["open_time"]  # EXPIRED — no finer condition to refine against

        # Anchor (diagram point 1, the swing high) and candidate (point 2,
        # the swing low itself) both sit on already-elapsed days relative
        # to confirmation, so no look-ahead-bias concern refining either —
        # unlike detected_at above, these always refine. The anchor is
        # always a peak (seeking its high); the candidate is always a
        # trough (seeking its low).
        anchor_day = candles[zone["anchor_at"]]["open_time"]
        anchor_time = await _refine_extreme_moment(db, symbol, market, anchor_day, seeking_high=True)
        candidate_day = candles[zone["candidate_at"]]["open_time"]
        candidate_time = await _refine_extreme_moment(db, symbol, market, candidate_day, seeking_high=False)

        rows.append({
            "symbol": symbol,
            "direction": "LONG",
            "state": zone["state"],
            "anchor_time": anchor_time,      # diagram point 1 — the swing high
            "anchor_price": zone["anchor_price"],
            "candidate_time": candidate_time,  # diagram point 2 — the swing low (Z's candle)
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
        })

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
    scan_swing_zones) and returns every historical LONG trade that actually
    had an entry — a zone that got TRIGGERED at some point — resolved as
    WIN (TP_HIT), LOSS (SL_HIT), or still OPEN (TRIGGERED, unresolved).
    Zones that confirmed but were never retested/entered aren't trades and
    are excluded. Entry is always exactly Z and exit is always exactly TP
    or SL (this strategy has no slippage/partial-fill concept), so every
    WIN/LOSS has the identical fixed PnL% (+tp_pct / -sl_pct) — only which
    coin and when differ. Sorted oldest entry first, matching a backtest
    trade log rather than the live dashboard's distance-to-Z ordering."""
    candles_by_symbol = await CandleRepository.get_ohlc_by_symbol(db, interval=TIMEFRAME, market=market)

    trades = []
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
            if zone["state"] == "TP_HIT":
                result, exit_price, gain_pct = "WIN", zone["tp"], tp_pct * 100
            elif zone["state"] == "SL_HIT":
                result, exit_price, gain_pct = "LOSS", zone["sl"], -sl_pct * 100
            else:  # still TRIGGERED, unresolved
                result, exit_price, gain_pct = "OPEN", None, None

            # Same intraday refinement as the live dashboard (see
            # scan_swing_zones), but only for recent trades — refining all
            # of them (2+ extra DB queries each) was turning a full
            # backtest scan into ~35s. Older trades keep the coarser
            # day-level timestamp (still the correct day, just not the
            # exact minute).
            is_recent = (latest_idx - zone["triggered_at"]) <= BACKTEST_REFINE_RECENT_DAYS
            entry_day = candles[zone["triggered_at"]]["open_time"]
            if is_recent:
                entry_time = await _refine_moment(db, symbol, market, entry_day, _touch_predicate(zone["z"]))
            else:
                entry_time = entry_day

            if zone["resolved_at"] is not None and zone["state"] in ("TP_HIT", "SL_HIT"):
                exit_day = candles[zone["resolved_at"]]["open_time"]
                if is_recent:
                    exit_time = await _refine_moment(
                        db, symbol, market, exit_day,
                        _resolution_predicate(zone["sl"], zone["tp"], zone["state"]),
                    )
                else:
                    exit_time = exit_day
            elif zone["resolved_at"] is not None:
                exit_time = candles[zone["resolved_at"]]["open_time"]  # EXPIRED — no finer condition to refine against
            else:
                exit_time = None

            # Same 4-point timeline as the live dashboard (see
            # scan_swing_zones): anchor (point 1), candidate/Z (point 2),
            # confirmation (point 3). Gated by the same is_recent flag as
            # entry/exit above, for the same performance reason.
            confirm_day = candles[zone["confirmed_at"]]["open_time"]
            reference = zone["swing_extreme"]
            if is_recent:
                detected_at = await _refine_moment(db, symbol, market, confirm_day, _confirm_predicate(reference, swing_threshold))
                anchor_day = candles[zone["anchor_at"]]["open_time"]
                anchor_time = await _refine_extreme_moment(db, symbol, market, anchor_day, seeking_high=True)
                candidate_day = candles[zone["candidate_at"]]["open_time"]
                candidate_time = await _refine_extreme_moment(db, symbol, market, candidate_day, seeking_high=False)
            else:
                detected_at = confirm_day
                anchor_time = candles[zone["anchor_at"]]["open_time"]
                candidate_time = candles[zone["candidate_at"]]["open_time"]

            trades.append({
                "symbol": symbol,
                "direction": "LONG",
                "z": zone["z"],
                "sl": zone["sl"],
                "tp": zone["tp"],
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
            })

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
