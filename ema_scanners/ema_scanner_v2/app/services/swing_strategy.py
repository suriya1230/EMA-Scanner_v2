"""
Swing Zone Retest Strategy — detects percentage-confirmed swing points on
each coin's 1d candles, projects them forward as price zones, and reports
which coins currently have a live (ARMED or TRIGGERED) LONG and/or SHORT
zone near price.

BUY (swing low) and SELL (swing high) are two FULLY INDEPENDENT searches
over the same candle history — neither waits its turn on the other. Each
is a chain of THREE points — "0 candle" -> "candle 1" (the anchor) ->
"Z candle" (the candidate) — where each point is reached by chasing a
running extreme away from the PREVIOUS point until the move between them
clears SWING_THRESHOLD, exactly the same mechanical pattern applied
twice in a row:

  - "0 candle": the highest CONFIRMED local swing-high pivot (SELL) /
    lowest confirmed swing-low pivot (BUY) seen since the search last
    reset — a pivot needs PIVOT_STRENGTH candles on both sides with
    strictly lower highs / higher lows to confirm (same definition as
    the frontend's own findRecentSwingLow/High helper). This is NOT
    simply "whichever pivot is most recent" — a pivot that's WEAKER than
    the one already tracked (a Lower High for SELL, a Higher Low for
    BUY) is skipped entirely; the 0 candle only ever advances to a
    pivot that's a genuine new Higher High / Lower Low. It keeps
    advancing that way for as long as nothing downstream has CONFIRMED
    yet (see below), then resets fresh (forgetting every prior pivot)
    once a cycle resolves. A pivot only becomes recognizable
    PIVOT_STRENGTH candles after it forms (once the confirming candles
    on its right exist), so there's an inherent short recognition lag,
    but no look-ahead relative to the candle currently being scanned.
  - "candle 1" (anchor): starting from the current 0 candle, track the
    running opposite extreme (a low for BUY, a high for SELL) forward,
    extending to any new such extreme as it forms (Rule 4.1) — the
    moment the move from the 0 candle into it first reaches
    SWING_THRESHOLD, that point locks in as candle 1.
  - "Z candle" (candidate): starting from candle 1, the exact same
    chase runs again in the opposite direction (a high for BUY, a low
    for SELL) until ITS move away from candle 1 first reaches
    SWING_THRESHOLD ("PRIMED").

Nothing in this chain is a fixed one-time value until a zone actually
CONFIRMS off it: for as long as no zone has confirmed yet, EVERY new,
MORE EXTREME 0 candle (a genuine Higher High for SELL, Lower Low for
BUY — never a weaker Lower High / Higher Low) immediately restarts the
whole 0->1->Z chase from scratch, discarding whatever not-yet-confirmed
candle 1/Z progress existed against the old one — priming a candidate is
only an ELIGIBILITY gate, not a freeze. Only once a zone actually
CONFIRMS does the entire chain (0 candle, candle 1, Z) freeze permanently
for the rest of this cycle.

BUY side:
  1. The current 0 candle is the lowest confirmed pivot LOW seen since
     the search last reset (a pivot that's merely more recent but HIGHER
     than the current one — a Higher Low — is skipped entirely, not
     used). A running high is chased forward from it (Rule 4.1): every
     time this running high reaches a NEW record, re-check whether the
     rise from the 0 candle into it clears SWING_THRESHOLD — if so,
     (re-)lock candle 1 onto that new record, discarding any not-yet-
     confirmed Z-candidate progress measured against whatever candle 1
     used to be. This keeps happening for as long as NOTHING has actually
     CONFIRMED yet (see step 3) — a coin that keeps making even lower
     lows before ever confirming a reversal doesn't get stuck on the
     first, shallower one just because it happened to prime first (the
     exact same rule the 0 candle itself follows one level up).
  2. From whatever candle 1 currently is, a running low is chased forward
     the same way; the moment the fall from candle 1 into it first
     reaches SWING_THRESHOLD, it's "PRIMED" as the Z candle. Priming is
     only an eligibility gate — it does NOT freeze anything (see step 1):
     an even lower pivot low, OR an even higher high forming off the
     current 0 candle, can still re-lock candle 1 and restart the Z
     search from scratch right up until step 3 actually confirms.
  3. Once primed, the moment a later candle's CLOSE (not wick — the
     recovery must actually hold through the close, not just spike past
     it intrabar) recovers >= SWING_THRESHOLD above the candidate's own
     low, it's CONFIRMED: Z = candidate.low, a tradeable LONG zone arms —
     and only NOW does the entire chain (0 candle, candle 1, Z) freeze
     permanently for the rest of this cycle.
  4. This is the ONLY Z this anchor will ever produce — no further
     candidate search runs from this anchor, no matter what price does
     afterward. Entry is the first later candle whose CLOSE touches Z
     (not a wick — same close-based rule as every other stage, SL/TP are
     the only wick-based check in the whole file), however many
     candles/days that takes, UNLESS the zone expires first (an ARMED
     zone unretested for more than max_zone_age candles/days expires —
     see DEFAULT_MAX_ZONE_AGE). SL = Z - 5%, TP = Z + 10% (both
     configurable), checked via wick.
  5. Once this zone fully RESOLVES (TP/SL/expiry), the search goes back
     to a fresh 0-candle search (step 1), forgetting every pivot tracked
     during the just-finished cycle.

SELL side is the exact mirror: 0 candle = the HIGHEST confirmed pivot
high seen since the search last reset (skipping any merely-more-recent
Lower High), candle 1 = a running low chased from it until the drop
clears SWING_THRESHOLD, Z = a running high chased from candle 1 until
the rise clears SWING_THRESHOLD ("PRIMED" — still just an eligibility
gate, not a freeze; an even higher pivot high can still restart
everything up until confirmation), confirms via a later CLOSE falling
>= SWING_THRESHOLD below Z — which is what freezes the entire chain —
Z = candidate.close, entry = a later candle's CLOSE touching Z, however
long that takes, SL = Z + 5%, TP = Z - 10%, then a fresh 0-candle search
once this cycle resolves.

So every swing point needs THREE qualifying legs, ALL close-based: 0
candle -> candle 1, candle 1 -> Z (checked via "primed"), and Z ->
confirming candle — each independently >= SWING_THRESHOLD. Pivot
detection (the 0 candle itself) is also close-based. SL and TP are the
only wick-based checks anywhere in this module.

A later candle can never extend/confirm a point off itself in the same
iteration that just created it (same-day exclusion / anti-look-ahead) —
enforced by `continue`-ing immediately after any such update.

LONG and SHORT each keep their own independent live trade AND their own
independent detection search — a coin can hold one live position of each
side at once, confirming completely on its own schedule. Both keep
advancing (close-based entry touch, then wick-based stop and wick-based
target) every subsequent candle.

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
# showing a setup that's sat untouched for months as if it were still a
# fresh, actionable watch item. Only affects ARMED zones — an already-
# TRIGGERED (open) trade has no age cap, and a resolved TP_HIT/SL_HIT is
# unaffected either way. Overridable per-request via the API's
# `max_zone_age` query param (ge=0, "0 disables").
DEFAULT_MAX_ZONE_AGE = 60
# Candles required on BOTH sides of a swing high/low for it to count as a
# confirmed local pivot ("0 candle") — same definition/default as the
# frontend's own findRecentSwingLow/High helper (app/page.js), so a pivot
# only becomes recognizable this many candles after it actually forms
# (once the confirming candles on its right exist).
PIVOT_STRENGTH = 2
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
    # Zone level Z = the candidate candle's own CLOSE — every stage of
    # detection (pivots, both legs, Z itself, entry) is close-based; only
    # SL/TP resolution stays wick-based (see _advance_zone). This is the
    # same value as "swing_extreme" below; the two fields are kept
    # separate in the output because "swing_extreme" is documented/
    # displayed as a distinct reference column, even though they're now
    # numerically equal.
    z = extreme["close"]
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
        "anchor_price": anchor["close"],
        "candidate_at": extreme_idx,      # candle index of the swing low/high itself (diagram point 2)
        "confirmed_at": idx,              # candle index of the confirming candle (diagram point 3)
        "triggered_at": None,             # candle index of the entry touch (diagram point 4)
        "resolved_at": None,  # candle index where TP_HIT/SL_HIT/EXPIRED happened
    }


def _advance_zone(zone: dict | None, c: dict, idx: int, max_zone_age: int | None) -> None:
    """Advances one live trade (LONG or SHORT, mutated in place) using this
    candle — entry touch is CLOSE-based (a candle's close trading at/past Z,
    not just an intrabar wick), but SL/TP resolution stays WICK-based (an
    intrabar "price trades at/below the stop level" touch, not a candle
    close). No-op if there's no zone or it's already resolved."""
    if zone is None or zone["state"] not in LIVE_STATES:
        return
    is_long = zone["direction"] == "LONG"
    z, sl, tp = zone["z"], zone["sl"], zone["tp"]

    if is_long:
        if zone["state"] == "ARMED" and c["close"] <= z:
            zone["state"] = "TRIGGERED"
            zone["triggered_at"] = idx
        if zone["state"] == "TRIGGERED":
            if c["low"] <= sl:
                zone["state"] = "SL_HIT"
                zone["resolved_at"] = idx
            elif c["high"] >= tp:
                zone["state"] = "TP_HIT"
                zone["resolved_at"] = idx
        # No ARMED-state SL/TP check here: entry is now CLOSE-based, so a
        # candle can wick through both Z and SL/TP without its close ever
        # reaching Z — that's a zone that was never actually entered, not
        # a stop-out, so it stays ARMED and keeps waiting.
    else:
        if zone["state"] == "ARMED" and c["close"] >= z:
            zone["state"] = "TRIGGERED"
            zone["triggered_at"] = idx
        if zone["state"] == "TRIGGERED":
            if c["high"] >= sl:
                zone["state"] = "SL_HIT"
                zone["resolved_at"] = idx
            elif c["low"] <= tp:
                zone["state"] = "TP_HIT"
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
    """Replays one coin's full candle history and returns EVERY zone ever
    confirmed on EITHER side (not just the current live ones) — each dict's
    "state" reflects wherever its lifecycle actually ended up (TP_HIT,
    SL_HIT, EXPIRED, or still ARMED/TRIGGERED if it's the side's last one
    and still live). LONG and SHORT are two FULLY INDEPENDENT searches, not
    a single alternating chain — see module docstring for the full rules."""
    n = len(candles)
    if n < 2:
        return []

    long_zones = _run_side_history(candles, True, max_zone_age, sl_pct, tp_pct, swing_threshold)
    short_zones = _run_side_history(candles, False, max_zone_age, sl_pct, tp_pct, swing_threshold)
    return long_zones + short_zones


def _is_pivot_low(candles: list[dict], i: int, strength: int) -> bool:
    """True if candles[i]'s CLOSE is strictly lower than `strength` candles'
    closes on BOTH sides of it — a confirmed local swing low. Close-based,
    like every other stage of detection (SL/TP are the only wick-based
    check in the whole file). Caller must ensure i-strength >= 0 and
    i+strength < len(candles)."""
    for k in range(1, strength + 1):
        if candles[i]["close"] >= candles[i - k]["close"] or candles[i]["close"] >= candles[i + k]["close"]:
            return False
    return True


def _is_pivot_high(candles: list[dict], i: int, strength: int) -> bool:
    """Mirror of _is_pivot_low for swing highs."""
    for k in range(1, strength + 1):
        if candles[i]["close"] <= candles[i - k]["close"] or candles[i]["close"] <= candles[i + k]["close"]:
            return False
    return True


def _run_side_history(
    candles: list[dict], is_long: bool, max_zone_age: int | None,
    sl_pct: float, tp_pct: float, swing_threshold: float,
) -> list[dict]:
    """Runs ONE direction's search independently of the other — BUY and
    SELL don't alternate or wait on each other. Each is a 3-point chain —
    0 candle -> candle 1 (anchor) -> Z candle (candidate) — where each
    point is reached by chasing a running extreme away from the previous
    one, extending to any new such extreme (Rule 4.1), until the move
    between them clears swing_threshold (the exact same mechanical
    pattern run twice: 0->1, then 1->Z). The 0 candle is the highest
    CONFIRMED local swing-high pivot (SELL) / lowest confirmed swing-low
    pivot (BUY) seen since the search last reset (see _is_pivot_low/High)
    — NOT simply whichever pivot is most recent: a pivot that's WEAKER
    than the one already tracked (a Lower High for SELL, a Higher Low for
    BUY) is skipped entirely. Candle 1 follows the exact same "keep
    re-basing until confirmed" rule one level down: every time the
    running extreme chased from the 0 candle reaches a new record, it
    re-checks whether the leg into it clears swing_threshold and, if so,
    (re-)locks candle 1 onto that new record — for as long as no zone has
    actually CONFIRMED yet, either a fresher 0 candle OR a deeper/higher
    candle-1 candidate off the SAME 0 candle immediately restarts the Z
    search from scratch, discarding whatever not-yet-confirmed progress
    existed (priming is only an ELIGIBILITY gate at both stages, never a
    freeze). Only once a zone actually CONFIRMS does the entire chain
    (0 candle, candle 1, Z) freeze permanently, and that Z is the ONLY one
    this anchor will ever produce — no further candidate search runs off
    it, it just waits (until TP/SL/expiry) for a retest. Once resolved,
    the search goes back to a fresh 0-candle search, forgetting every
    pivot tracked during the just-finished cycle. Returns every zone this
    side ever confirmed, chronologically."""
    n = len(candles)
    pivot_type = "low" if is_long else "high"

    def leg_pct(anchor: dict, candidate: dict) -> float:
        # Stage 2 leg (candle 1 -> Z), CLOSE-based: for BUY, candle 1 is a
        # HIGH and Z a LOW (a fall); for SELL, candle 1 is a LOW and Z a
        # HIGH (a rise). Denominator is always the earlier point (candle 1).
        ref = anchor["close"]
        extreme = candidate["close"]
        if not ref:
            return 0.0
        return (ref - extreme) / ref if is_long else (extreme - ref) / ref

    def zero_leg_pct(zero: dict, stage1_pt: dict) -> float:
        # Stage 1 leg (0 candle -> candle 1), CLOSE-based — the OPPOSITE
        # direction from stage 2 above: for BUY, the 0 candle is a LOW and
        # candle 1 a HIGH (a rise); for SELL, the 0 candle is a HIGH and
        # candle 1 a LOW (a fall). Denominator is always the earlier point
        # (the 0 candle).
        ref = zero["close"]
        extreme = stage1_pt["close"]
        if not ref:
            return 0.0
        return (extreme - ref) / ref if is_long else (ref - extreme) / ref

    cycle_pivot_low_idx: int | None = None   # lowest confirmed pivot low seen since the last reset
    cycle_pivot_high_idx: int | None = None  # highest confirmed pivot high seen since the last reset

    zero_idx: int | None = None            # this cycle's current 0 candle (a confirmed pivot)
    stage1_extreme: dict | None = None     # chasing candle 1 from the 0 candle
    stage1_extreme_idx: int | None = None

    anchor: dict | None = None             # candle 1, once stage 1 primes
    anchor_idx: int | None = None
    last_cycle_anchor_idx: int | None = None  # candle 1 the most recently RESOLVED cycle used
    pending_zero_at: int | None = None     # this cycle's own 0 candle, captured once candle 1 locks
    pending_zero_price: float | None = None

    candidate: dict | None = None          # chasing Z from candle 1
    candidate_idx: int | None = None
    primed = False

    zone_confirmed_this_cycle = False  # freezes the whole chain once a zone actually CONFIRMS
    zone: dict | None = None
    history: list[dict] = []

    for idx in range(1, n):
        c = candles[idx]

        was_live = zone is not None and zone["state"] in LIVE_STATES
        _advance_zone(zone, c, idx, max_zone_age)
        if was_live and zone is not None and zone["state"] not in LIVE_STATES:
            # Cycle just resolved — go back to a fresh 0-candle search;
            # the next cycle's pivot tracking starts over from scratch too
            # (a new "highest/lowest pivot of THIS swing", not carried
            # over from before).
            last_cycle_anchor_idx = anchor_idx
            cycle_pivot_low_idx, cycle_pivot_high_idx = None, None
            zero_idx = None
            stage1_extreme, stage1_extreme_idx = None, None
            anchor, anchor_idx = None, None
            pending_zero_at, pending_zero_price = None, None
            candidate, candidate_idx = None, None
            primed = False
            zone_confirmed_this_cycle = False

        # Recognize a freshly-CONFIRMED fractal pivot — a pivot at
        # idx-PIVOT_STRENGTH only becomes recognizable once idx exists
        # (needs PIVOT_STRENGTH confirming candles on its right). Only
        # accepted if it's MORE extreme than whatever's already tracked
        # this cycle — a "Lower High" (SELL) / "Higher Low" (BUY) pivot
        # is skipped entirely, so the 0 candle only ever advances to a
        # genuine new Higher High / Lower Low, never backslides to a
        # weaker, merely-more-recent one.
        check_idx = idx - PIVOT_STRENGTH
        if check_idx - PIVOT_STRENGTH >= 0:
            if _is_pivot_low(candles, check_idx, PIVOT_STRENGTH):
                if cycle_pivot_low_idx is None or candles[check_idx]["close"] < candles[cycle_pivot_low_idx]["close"]:
                    cycle_pivot_low_idx = check_idx
            if _is_pivot_high(candles, check_idx, PIVOT_STRENGTH):
                if cycle_pivot_high_idx is None or candles[check_idx]["close"] > candles[cycle_pivot_high_idx]["close"]:
                    cycle_pivot_high_idx = check_idx

        current_zero_idx = cycle_pivot_low_idx if is_long else cycle_pivot_high_idx

        if not zone_confirmed_this_cycle and current_zero_idx is not None and current_zero_idx != zero_idx:
            # A fresher pivot than whatever's currently backing this
            # search — restart the ENTIRE 0->1->Z chase from it. Nothing
            # downstream has confirmed yet, so nothing is lost that
            # actually mattered (priming candle 1 or Z is only an
            # eligibility gate, not a commitment).
            if last_cycle_anchor_idx is None or current_zero_idx != last_cycle_anchor_idx:
                zero_idx = current_zero_idx
                stage1_extreme, stage1_extreme_idx = None, None
                anchor, anchor_idx = None, None
                candidate, candidate_idx = None, None
                primed = False
            continue

        if zero_idx is None:
            continue  # no confirmed pivot yet to serve as a 0 candle

        if not zone_confirmed_this_cycle and idx > zero_idx:
            # STAGE 1 — chase candle 1 from the 0 candle, extending to any
            # new, more extreme point (Rule 4.1) for as long as NOTHING has
            # actually CONFIRMED yet — exactly the same "keep re-basing
            # until confirmed, not just until primed" rule the 0 candle
            # itself follows. A candle that doesn't extend this iteration
            # falls through to stage 2 below instead (it might still work
            # as a Z candidate against whatever candle 1 currently is).
            zero = candles[zero_idx]
            is_new_stage1 = stage1_extreme is None or (
                c["close"] > stage1_extreme["close"] if is_long else c["close"] < stage1_extreme["close"]
            )
            if is_new_stage1:
                stage1_extreme, stage1_extreme_idx = c, idx
                if zero_leg_pct(zero, stage1_extreme) >= swing_threshold and not (
                    last_cycle_anchor_idx is not None and stage1_extreme_idx == last_cycle_anchor_idx
                ):
                    # (Re-)lock candle 1 onto this deeper/higher extreme —
                    # discarding any not-yet-confirmed Z-search progress
                    # measured against whatever candle 1 used to be.
                    anchor, anchor_idx = stage1_extreme, stage1_extreme_idx
                    pending_zero_at, pending_zero_price = zero_idx, zero["close"]
                    candidate, candidate_idx = None, None
                    primed = False
                continue

        if anchor is None:
            continue  # stage 1 hasn't primed even once yet

        if idx <= anchor_idx:
            continue  # Z can never be at or before its own candle 1

        if zone_confirmed_this_cycle:
            continue  # only ONE Z per anchor cycle — just wait for it to resolve (top of loop)

        # STAGE 2 — chase Z from candle 1, CLOSE-based (same mechanics as
        # before, just close instead of wick).
        is_new_candidate = candidate is None or (
            c["close"] < candidate["close"] if is_long else c["close"] > candidate["close"]
        )
        if is_new_candidate:
            candidate, candidate_idx = c, idx
            primed = leg_pct(anchor, candidate) >= swing_threshold
            continue

        if not primed:
            continue

        cand_ref = candidate["close"]
        move_pct = (
            (c["close"] - cand_ref) / cand_ref if is_long
            else (cand_ref - c["close"]) / cand_ref
        ) if cand_ref else 0.0
        if not (cand_ref and move_pct >= swing_threshold):
            continue

        new_zone = _make_zone(
            anchor, anchor_idx, candidate, candidate_idx,
            pivot_type, move_pct, idx, c["close"], sl_pct, tp_pct,
        )
        # "0 candle" — this cycle's own pivot, captured when candle 1
        # locked (see stage 1 above).
        new_zone["zeroth_at"] = pending_zero_at
        new_zone["zeroth_price"] = pending_zero_price
        history.append(new_zone)
        zone = new_zone
        # The whole chain (0 candle, candle 1, Z) is now permanently
        # frozen for the rest of this cycle — a zone has actually
        # CONFIRMED off it, not merely primed. This is also the ONLY Z
        # this anchor will ever produce — no further candidate search
        # runs until this exact zone resolves (top of loop), no matter
        # how long that takes; it just waits for a retest.
        zone_confirmed_this_cycle = True

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
    # Entry is CLOSE-based, same as every other detection stage (SL/TP
    # resolution is the only wick-based check — see _resolution_predicate).
    if direction == "LONG":
        return lambda row: row["close"] <= z
    return lambda row: row["close"] >= z


async def _refine_extreme_moment(
    db: AsyncSession, symbol: str, market: str, day_open_time: int, seeking_high: bool,
) -> int:
    """Pinpoints WHEN during a day its own extreme (the opposing-side
    anchor, or the swing low/high candidate) was actually printed — using
    whichever finer interval tier is available, picking the row with the
    max close (seeking_high=True) or min close (seeking_high=False),
    matching close-based detection. Falls back to the day's own open_time
    if no finer data covers it."""
    rows = await _fetch_finest_available_rows(db, symbol, market, day_open_time)
    if not rows:
        return day_open_time
    best = max(rows, key=lambda r: r["close"]) if seeking_high else min(rows, key=lambda r: r["close"])
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

        # "0 candle" (diagram point 0) — this zone's own most-recently-
        # confirmed local swing pivot that validated its candle 1 (see
        # _is_pivot_low/High in swing_strategy.py), a trough for LONG, a
        # peak for SHORT — opposite seeking_high from this zone's own
        # anchor, since it's the opposite type of extreme.
        zeroth_day = candles[zone["zeroth_at"]]["open_time"]
        zeroth_time = await _refine_extreme_moment(db, symbol, market, zeroth_day, seeking_high=(direction == "SHORT"))

    return {
        "symbol": symbol,
        "direction": direction,
        "state": zone["state"],
        "zeroth_time": zeroth_time,       # diagram point 0 — this zone's own confirming pivot
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
        zeroth_time = candles[zone["zeroth_at"]]["open_time"]
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

            # "0 candle" (diagram point 0) — same confirming-pivot
            # derivation as the live dashboard.
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
