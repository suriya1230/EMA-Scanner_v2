"""
Swing Zone Retest Strategy API — see app/services/swing_strategy.py for the
full rules. Read-only: computed live from stored 1d candles on every call,
nothing written back to the DB (detection is fully mechanical and replayed
from candle history, so there's no separate zone state to persist).

GET /api/swing-zones    — every coin >= min_volume_usdt (default $5M) with a
                          currently live (ARMED/TRIGGERED) long or short zone.
GET /api/swing-backtest — every historical trade (a zone that actually got
                          entered) across all qualifying coins, resolved as
                          WIN/LOSS/OPEN. Powers the Swing Strategy page's
                          Backtest Summary.
"""
from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.database import get_db
from app.services.swing_strategy import (
    DEFAULT_CONFIRM_FROM,
    DEFAULT_MAX_ZONE_AGE,
    DEFAULT_MIN_VOLUME_USDT,
    DEFAULT_SL_PCT,
    DEFAULT_TP_PCT,
    scan_swing_backtest,
    scan_swing_zones,
)

router = APIRouter(prefix="/api", tags=["swing-strategy"])

# sl_pct/tp_pct are accepted as whole percent values (e.g. 5 means 5%), not
# fractions, then divided by 100 before reaching the service layer — the RR
# preset buttons on the frontend (1:2 5%/10%, 1:5 2%/10%, 1:3 2%/6%) send
# these directly.


@router.get("/swing-zones")
async def swing_zones_endpoint(
    market: str = Query("futures"),
    min_volume_usdt: float = Query(DEFAULT_MIN_VOLUME_USDT, ge=0),
    confirm_from: str = Query(DEFAULT_CONFIRM_FROM, pattern="^(wick|close)$"),
    max_zone_age: Optional[int] = Query(
        DEFAULT_MAX_ZONE_AGE, ge=0,
        description="Expire an ARMED zone after N candles (days) unused. Pass 0 to disable.",
    ),
    sl_pct: float = Query(DEFAULT_SL_PCT * 100, gt=0, description="Stop-loss distance from Z, as a percent (e.g. 5 = 5%)."),
    tp_pct: float = Query(DEFAULT_TP_PCT * 100, gt=0, description="Take-profit distance from Z, as a percent (e.g. 10 = 10%)."),
    db: AsyncSession = Depends(get_db),
):
    return await scan_swing_zones(
        db, market=market, min_volume_usdt=min_volume_usdt,
        confirm_from=confirm_from, max_zone_age=max_zone_age or None,
        sl_pct=sl_pct / 100, tp_pct=tp_pct / 100,
    )


@router.get("/swing-backtest")
async def swing_backtest_endpoint(
    market: str = Query("futures"),
    min_volume_usdt: float = Query(DEFAULT_MIN_VOLUME_USDT, ge=0),
    confirm_from: str = Query(DEFAULT_CONFIRM_FROM, pattern="^(wick|close)$"),
    max_zone_age: Optional[int] = Query(
        DEFAULT_MAX_ZONE_AGE, ge=0,
        description="Expire an ARMED zone after N candles (days) unused. Pass 0 to disable.",
    ),
    sl_pct: float = Query(DEFAULT_SL_PCT * 100, gt=0, description="Stop-loss distance from Z, as a percent (e.g. 5 = 5%)."),
    tp_pct: float = Query(DEFAULT_TP_PCT * 100, gt=0, description="Take-profit distance from Z, as a percent (e.g. 10 = 10%)."),
    db: AsyncSession = Depends(get_db),
):
    return await scan_swing_backtest(
        db, market=market, min_volume_usdt=min_volume_usdt,
        confirm_from=confirm_from, max_zone_age=max_zone_age or None,
        sl_pct=sl_pct / 100, tp_pct=tp_pct / 100,
    )
