"""
Swing Zone Retest Strategy — detects percentage-confirmed swing points on
each coin's 1d candles, projects them forward as price zones, and reports
which coins currently have a live (ARMED or TRIGGERED) zone near price.

LONG and SHORT are tracked as two fully INDEPENDENT passes over the same
candle history (not one shared alternating zigzag chain) — each side needs
BOTH of the following legs, each >= SWING_THRESHOLD, before it arms a zone:

  LONG (swing low):
    1. DOWN-LEG: track the running peak (highest high) as the "anchor".
       Whenever price makes a new peak, the anchor resets to it (the leg
       into a candidate low must be measured from the MOST RECENT peak,
       not a stale one).
    2. Track the running trough (lowest low) since that anchor — the
       CANDIDATE, L. The moment the decline from anchor to L reaches
       >= SWING_THRESHOLD, L becomes ELIGIBLE (this is "the swing low is
       detected" — the down-leg itself must be a genuine >=10% move, not
       just any dip).
    3. UP-LEG (confirmation): once eligible, track price rising away from
       L. The moment a candle's CLOSE — never its wick/high — is
       >= SWING_THRESHOLD above L (L.low if CONFIRM_FROM="wick", else
       L.close), L is CONFIRMED. Unlike the down-leg above, an intrabar
       spike doesn't count here — the reversal must actually hold through
       the candle's close, not just wick past the threshold.
    4. Zone level Z = L.low (the candle's own extreme, the same value as
       "swing_extreme" below — not its close), projected forward and ARMED.
    5. Entry: price trades back down and touches Z (intrabar low <= Z).
    6. Stop: Z * (1 - sl_pct). Target: Z * (1 + tp_pct) — both configurable
       per call (see the RR presets in app/api/swing_strategy.py), default
       5% / 10%.
    7. Invalidation: a candle CLOSES at or beyond the stop level (a close-
       based rule, distinct from the wick-based entry touch).
    8. After confirming, the anchor/candidate reset and the search starts
       fresh for this side's NEXT swing low.

  SHORT (swing high): the exact mirror — running trough as anchor, running
    peak as candidate, confirmed on a >= SWING_THRESHOLD fall from it.

A coin can hold one live long zone and one live short zone at once, from
two independently-tracked histories — a fresh confirmation on a side always
REPLACES whatever zone that side currently holds, regardless of the old
zone's state (ARMED or TRIGGERED).

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
SWING_THRESHOLD = 0.10          # confirmation move required, EACH leg (down and up)
# Default risk:reward — SL/TP distance from Z as a fraction. Both scan_swing_
# zones and scan_swing_backtest accept sl_pct/tp_pct overrides (see the RR
# presets exposed in app/api/swing_strategy.py: 1:2 5%/10%, 1:5 2%/10%,
# 1:3 2%/6%), so these are just what's used when nothing else is passed.
DEFAULT_TP_PCT = 0.10
DEFAULT_SL_PCT = 0.05
DEFAULT_CONFIRM_FROM = "wick"   # "wick" | "close" — see module docstring
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
        "z": z,
        "state": "ARMED",
        "sl": z * (1 - sl_pct) if is_long else z * (1 + sl_pct),
        "tp": z * (1 + tp_pct) if is_long else z * (1 - tp_pct),
        "swing_extreme": extreme["low"] if is_long else extreme["high"],
        "confirm_move_pct": move_pct,
        "confirm_price": confirm_price,  # the confirming candle's own close
        "body_ratio": body_ratio,
        "anchor_at": anchor_idx,          # candle index of the peak/trough (diagram point 1)
        "anchor_price": anchor["high"] if is_long else anchor["low"],
        "candidate_at": extreme_idx,      # candle index of the swing low/high itself (diagram point 2)
        "confirmed_at": idx,              # candle index of the confirming candle (diagram point 3)
        "triggered_at": None,             # candle index of the entry touch (diagram point 4)
        "resolved_at": None,  # candle index where TP_HIT/SL_HIT/EXPIRED happened
    }


def _run_side_history(
    candles: list[dict], side: str, confirm_from: str, max_zone_age: int | None,
    sl_pct: float = DEFAULT_SL_PCT, tp_pct: float = DEFAULT_TP_PCT,
) -> list[dict]:
    """Replays one coin's full candle history once for one side ("long" or
    "short"), independently of the other side, and returns EVERY zone ever
    confirmed on this side (not just the current live one) — each dict's
    "state" reflects wherever its lifecycle actually ended up (TP_HIT,
    SL_HIT, EXPIRED, or still ARMED/TRIGGERED if it's the last one and
    still live). Used by both the live dashboard (scan_swing_zones, which
    just wants the last entry if it's still live) and the backtest
    (scan_swing_backtest, which wants the full resolved history). See
    module docstring for the two-leg (down-leg then up-leg, or the mirror)
    rule."""
    anchor: dict | None = None      # the opposing extreme the current leg is measured from
    anchor_idx: int | None = None
    candidate: dict | None = None   # the running extreme (trough for long, peak for short)
    candidate_idx: int | None = None
    primed = False                  # has the leg INTO candidate already reached SWING_THRESHOLD?
    zone: dict | None = None
    history: list[dict] = []

    for idx, c in enumerate(candles):
        # 1. Advance the existing zone's lifecycle using this candle
        #    (entry touch, then close-based stop/target) — independent of
        #    the anchor/candidate tracking below.
        if zone is not None and zone["state"] in LIVE_STATES:
            z, sl, tp = zone["z"], zone["sl"], zone["tp"]
            if side == "long":
                if zone["state"] == "ARMED" and c["low"] <= z:
                    zone["state"] = "TRIGGERED"
                    zone["triggered_at"] = idx
                if zone["state"] == "TRIGGERED":
                    if c["close"] <= sl:
                        zone["state"] = "SL_HIT"
                        zone["resolved_at"] = idx
                    elif c["high"] >= tp:
                        zone["state"] = "TP_HIT"
                        zone["resolved_at"] = idx
                elif c["close"] <= sl:  # still ARMED, never touched — close-based invalidation
                    zone["state"] = "SL_HIT"
                    zone["resolved_at"] = idx
            else:
                if zone["state"] == "ARMED" and c["high"] >= z:
                    zone["state"] = "TRIGGERED"
                    zone["triggered_at"] = idx
                if zone["state"] == "TRIGGERED":
                    if c["close"] >= sl:
                        zone["state"] = "SL_HIT"
                        zone["resolved_at"] = idx
                    elif c["low"] <= tp:
                        zone["state"] = "TP_HIT"
                        zone["resolved_at"] = idx
                elif c["close"] >= sl:
                    zone["state"] = "SL_HIT"
                    zone["resolved_at"] = idx

            if (
                zone["state"] == "ARMED" and max_zone_age is not None
                and idx - zone["confirmed_at"] > max_zone_age
            ):
                zone["state"] = "EXPIRED"
                zone["resolved_at"] = idx

        # 2. Track the anchor (the opposing extreme the down-leg/up-leg is
        #    measured from) — a new anchor invalidates whatever partial leg
        #    was being measured from the OLD one, since the leg into a
        #    candidate must be measured from the MOST RECENT such extreme.
        #    A candle that just became the new anchor can't also be a
        #    candidate or confirmation in this same iteration (anti-look-
        #    ahead-bias, same reasoning as the candidate check below).
        is_new_anchor = anchor is None or (
            c["high"] > anchor["high"] if side == "long" else c["low"] < anchor["low"]
        )
        if is_new_anchor:
            anchor, anchor_idx, candidate, candidate_idx, primed = c, idx, None, None, False
            continue

        # 3. Track the candidate extreme (trough for long, peak for short)
        #    since the anchor, and whether the leg INTO it ("the down-leg
        #    is 10% or more") has reached SWING_THRESHOLD yet — this is
        #    "detecting" the swing point. A candle that just extended the
        #    candidate can't also confirm the reversal off it in the same
        #    iteration, same anti-look-ahead-bias rule.
        is_new_candidate = candidate is None or (
            c["low"] < candidate["low"] if side == "long" else c["high"] > candidate["high"]
        )
        if is_new_candidate:
            candidate, candidate_idx = c, idx
            anchor_ref = anchor["high" if side == "long" else "low"] if confirm_from == "wick" else anchor["close"]
            leg_pct = (
                (anchor_ref - c["low"]) / anchor_ref if side == "long"
                else (c["high"] - anchor_ref) / anchor_ref
            ) if anchor_ref else 0.0
            if leg_pct >= SWING_THRESHOLD:
                primed = True
            continue

        # 4. Once primed (down-leg/up-leg already >= threshold), check for
        #    the confirming reversal away from the candidate — "the up-leg
        #    reversal 10%" that actually arms the zone. This leg is always
        #    measured by CANDLE CLOSE, never the wick — a candle's high/low
        #    spiking past the threshold intrabar doesn't count; the candle
        #    must actually CLOSE beyond it. (The down-leg/up-leg INTO the
        #    candidate in step 3 above is unaffected — that still measures
        #    the real decline/rise via wick, since it's identifying how far
        #    price actually traded, not confirming a reversal.)
        if primed:
            cand_ref = candidate["low" if side == "long" else "high"] if confirm_from == "wick" else candidate["close"]
            move_pct = (
                (c["close"] - cand_ref) / cand_ref if side == "long"
                else (cand_ref - c["close"]) / cand_ref
            ) if cand_ref else 0.0
            if cand_ref and move_pct >= SWING_THRESHOLD:
                zone = _make_zone(
                    anchor, anchor_idx, candidate, candidate_idx,
                    "low" if side == "long" else "high", move_pct, idx, c["close"],
                    sl_pct, tp_pct,
                )
                history.append(zone)
                # restart the search for this side's next swing point
                anchor, anchor_idx, candidate, candidate_idx, primed = None, None, None, None, False

    return history


def _run_side(
    candles: list[dict], side: str, confirm_from: str, max_zone_age: int | None,
    sl_pct: float = DEFAULT_SL_PCT, tp_pct: float = DEFAULT_TP_PCT,
) -> dict | None:
    """Live-dashboard view: this side's most recent zone, whatever its
    state — ARMED/TRIGGERED (still live) or TP_HIT/SL_HIT/EXPIRED
    (completed) — so a coin's last completed outcome keeps showing on the
    dashboard until a new zone confirms on that side, instead of vanishing
    the moment it resolves."""
    history = _run_side_history(candles, side, confirm_from, max_zone_age, sl_pct, tp_pct)
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


def _confirm_predicate(reference: float, side: str):
    if not reference:
        return lambda row: False
    if side == "long":
        return lambda row: (row["high"] - reference) / reference >= SWING_THRESHOLD
    return lambda row: (reference - row["low"]) / reference >= SWING_THRESHOLD


def _touch_predicate(z: float, side: str):
    if side == "long":
        return lambda row: row["low"] <= z
    return lambda row: row["high"] >= z


async def _refine_extreme_moment(
    db: AsyncSession, symbol: str, market: str, day_open_time: int, seeking_high: bool,
) -> int:
    """Pinpoints WHEN during a day its own extreme (the peak/trough anchor,
    or the swing low/high candidate) was actually printed — using whichever
    finer interval tier is available, picking the row with the max high
    (seeking_high=True) or min low (seeking_high=False). Falls back to the
    day's own open_time if no finer data covers it."""
    rows = await _fetch_finest_available_rows(db, symbol, market, day_open_time)
    if not rows:
        return day_open_time
    best = max(rows, key=lambda r: r["high"]) if seeking_high else min(rows, key=lambda r: r["low"])
    return best["open_time"]


def _resolution_predicate(sl: float, tp: float, side: str, outcome: str):
    """Checks whichever condition matches the ALREADY-KNOWN 1d-level
    outcome (TP_HIT or SL_HIT) — not re-derived at finer granularity, so
    the refined moment can't disagree with the daily-level determination
    of how the trade actually ended."""
    if outcome == "TP_HIT":
        return (lambda row: row["high"] >= tp) if side == "long" else (lambda row: row["low"] <= tp)
    return (lambda row: row["close"] <= sl) if side == "long" else (lambda row: row["close"] >= sl)


async def scan_swing_zones(
    db: AsyncSession,
    market: str = "futures",
    min_volume_usdt: float = DEFAULT_MIN_VOLUME_USDT,
    confirm_from: str = DEFAULT_CONFIRM_FROM,
    max_zone_age: int | None = DEFAULT_MAX_ZONE_AGE,
    sl_pct: float = DEFAULT_SL_PCT,
    tp_pct: float = DEFAULT_TP_PCT,
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
            zone = _run_side(candles, side, confirm_from, max_zone_age, sl_pct, tp_pct)
            if zone is None:
                continue
            day_open_time = candles[zone["confirmed_at"]]["open_time"]
            reference = zone["swing_extreme"] if confirm_from == "wick" else zone["z"]
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
                detected_at = await _refine_moment(db, symbol, market, day_open_time, _confirm_predicate(reference, side))
            else:
                detected_at = day_open_time

            triggered_at = None
            if zone["triggered_at"] is not None:
                trig_day = candles[zone["triggered_at"]]["open_time"]
                triggered_at = await _refine_moment(db, symbol, market, trig_day, _touch_predicate(zone["z"], side))

            resolved_at = None
            if zone["resolved_at"] is not None and zone["state"] in ("TP_HIT", "SL_HIT"):
                resolved_day = candles[zone["resolved_at"]]["open_time"]
                resolved_at = await _refine_moment(
                    db, symbol, market, resolved_day,
                    _resolution_predicate(zone["sl"], zone["tp"], side, zone["state"]),
                )
            elif zone["resolved_at"] is not None:
                resolved_at = candles[zone["resolved_at"]]["open_time"]  # EXPIRED — no finer condition to refine against

            # Anchor (diagram point 1, the peak/trough) and candidate
            # (point 2, the swing low/high itself) both sit on already-
            # elapsed days relative to confirmation, so no look-ahead-bias
            # concern refining either — unlike detected_at above, these
            # always refine. seeking_high flips between the two: for LONG,
            # the anchor is a peak (seeking its high) and the candidate is
            # a trough (seeking its low); for SHORT it's the reverse.
            anchor_day = candles[zone["anchor_at"]]["open_time"]
            anchor_time = await _refine_extreme_moment(db, symbol, market, anchor_day, seeking_high=(side == "long"))
            candidate_day = candles[zone["candidate_at"]]["open_time"]
            candidate_time = await _refine_extreme_moment(db, symbol, market, candidate_day, seeking_high=(side == "short"))

            rows.append({
                "symbol": symbol,
                "direction": "LONG" if side == "long" else "SHORT",
                "state": zone["state"],
                "anchor_time": anchor_time,      # diagram point 1 — peak/trough
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
            })

    rows.sort(key=lambda r: abs(r["distance_pct"]))

    return {
        "market": market,
        "timeframe": TIMEFRAME,
        "min_volume_usdt": min_volume_usdt,
        "confirm_from": confirm_from,
        "swing_threshold": SWING_THRESHOLD,
        "tp_pct": tp_pct,
        "sl_pct": sl_pct,
        "symbols_scanned": symbols_scanned,
        "zones": rows,
    }


async def scan_swing_backtest(
    db: AsyncSession,
    market: str = "futures",
    min_volume_usdt: float = DEFAULT_MIN_VOLUME_USDT,
    confirm_from: str = DEFAULT_CONFIRM_FROM,
    max_zone_age: int | None = DEFAULT_MAX_ZONE_AGE,
    sl_pct: float = DEFAULT_SL_PCT,
    tp_pct: float = DEFAULT_TP_PCT,
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

        for side in ("long", "short"):
            for zone in _run_side_history(candles, side, confirm_from, max_zone_age, sl_pct, tp_pct):
                if zone["triggered_at"] is None:
                    continue  # never retested/entered — not a trade
                if zone["state"] == "TP_HIT":
                    result, exit_price, gain_pct = "WIN", zone["tp"], tp_pct * 100
                elif zone["state"] == "SL_HIT":
                    result, exit_price, gain_pct = "LOSS", zone["sl"], -sl_pct * 100
                else:  # still TRIGGERED, unresolved
                    result, exit_price, gain_pct = "OPEN", None, None

                # Same intraday refinement as the live dashboard (see
                # scan_swing_zones), but only for recent trades — refining
                # all of them (2+ extra DB queries each) was turning a full
                # backtest scan into ~35s. Older trades keep the coarser
                # day-level timestamp (still the correct day, just not the
                # exact minute).
                is_recent = (latest_idx - zone["triggered_at"]) <= BACKTEST_REFINE_RECENT_DAYS
                entry_day = candles[zone["triggered_at"]]["open_time"]
                if is_recent:
                    entry_time = await _refine_moment(db, symbol, market, entry_day, _touch_predicate(zone["z"], side))
                else:
                    entry_time = entry_day

                if zone["resolved_at"] is not None and zone["state"] in ("TP_HIT", "SL_HIT"):
                    exit_day = candles[zone["resolved_at"]]["open_time"]
                    if is_recent:
                        exit_time = await _refine_moment(
                            db, symbol, market, exit_day,
                            _resolution_predicate(zone["sl"], zone["tp"], side, zone["state"]),
                        )
                    else:
                        exit_time = exit_day
                elif zone["resolved_at"] is not None:
                    exit_time = candles[zone["resolved_at"]]["open_time"]  # EXPIRED — no finer condition to refine against
                else:
                    exit_time = None

                # Same 4-point timeline as the live dashboard (see
                # scan_swing_zones): anchor (point 1), candidate/Z (point
                # 2), confirmation (point 3). Gated by the same is_recent
                # flag as entry/exit above, for the same performance reason.
                confirm_day = candles[zone["confirmed_at"]]["open_time"]
                reference = zone["swing_extreme"] if confirm_from == "wick" else zone["z"]
                if is_recent:
                    detected_at = await _refine_moment(db, symbol, market, confirm_day, _confirm_predicate(reference, side))
                    anchor_day = candles[zone["anchor_at"]]["open_time"]
                    anchor_time = await _refine_extreme_moment(db, symbol, market, anchor_day, seeking_high=(side == "long"))
                    candidate_day = candles[zone["candidate_at"]]["open_time"]
                    candidate_time = await _refine_extreme_moment(db, symbol, market, candidate_day, seeking_high=(side == "short"))
                else:
                    detected_at = confirm_day
                    anchor_time = candles[zone["anchor_at"]]["open_time"]
                    candidate_time = candles[zone["candidate_at"]]["open_time"]

                trades.append({
                    "symbol": symbol,
                    "direction": "LONG" if side == "long" else "SHORT",
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
        "confirm_from": confirm_from,
        "tp_pct": tp_pct,
        "sl_pct": sl_pct,
        "symbols_scanned": symbols_scanned,
        "trades": trades,
    }
