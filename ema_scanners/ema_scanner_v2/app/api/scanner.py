"""
Scanner API endpoints — CSV-only mode.

GET  /api/signals              — Recent signals (filtered) — reads the old
                                  `signals` table directly; empty unless it
                                  still holds pre-existing data, since nothing
                                  writes to it anymore.
GET  /api/candles/{symbol}     — Candles from DB (for backtest — never hits
                                  Binance).

Kept only so the old per-coin Details/Backtest/Backtest-Summary pages don't
error out if ever reopened. Both routes are plain PostgreSQL reads with no
dependency on any live scanner/collector service.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from fastapi import Depends

from app.db.database import get_db
from app.models.models import Signal
from app.schemas.schemas import SignalOut
from app.services.repository import CandleRepository

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api", tags=["scanner"])

VALID_INTERVALS = {"1m", "15m", "1h", "2h", "4h", "6h"}
VALID_MARKETS = {"futures", "spot"}


# ─── Signals ──────────────────────────────────────────────────────────────────

@router.get("/signals", response_model=list[SignalOut])
async def get_signals(
    symbol: Optional[str] = Query(None),
    signal_type: Optional[str] = Query(None, description="BUY or SELL"),
    interval: str = Query("1h", description="1m | 15m | 1h | 2h | 4h | 6h"),
    market: str = Query("futures", description="futures | spot"),
    limit: int = Query(50, ge=1, le=10000),
    days: Optional[int] = Query(None, description="Filter signals from last N days (e.g. 7)"),
    db: AsyncSession = Depends(get_db),
):
    """
    Returns signals ordered by cross_time descending.
    Use ?interval=2h to get signals for that timeframe (default 1h).
    Use ?market=spot for spot-market signals (default futures).
    Use ?days=7 to get signals from last 7 days.
    Use ?symbol=BTCUSDT to filter by symbol.
    Use ?signal_type=BUY or SELL to filter by type.
    """
    if interval not in VALID_INTERVALS:
        raise HTTPException(status_code=400, detail=f"Invalid interval '{interval}'.")
    if market not in VALID_MARKETS:
        raise HTTPException(status_code=400, detail=f"Invalid market '{market}'.")

    stmt = (
        select(Signal)
        .where(Signal.interval == interval, Signal.market == market)
        .order_by(Signal.cross_time.desc())
        .limit(limit)
    )

    if symbol:
        stmt = stmt.where(Signal.symbol == symbol.upper())
    if signal_type:
        stmt = stmt.where(Signal.signal_type == signal_type.upper())
    if days:
        from datetime import timedelta
        cutoff = datetime.now(tz=timezone.utc) - timedelta(days=days)
        stmt = stmt.where(Signal.cross_time >= cutoff)

    result = await db.execute(stmt)
    signals = result.scalars().all()
    return [SignalOut.model_validate(s) for s in signals]


# ─── Candles (serves frontend backtest — reads from DB, never hits Binance) ───

@router.get("/candles/{symbol}")
async def get_candles(
    symbol: str,
    interval: str = Query("1h", description="1m | 15m | 1h | 2h | 4h | 6h"),
    market: str = Query("futures", description="futures | spot"),
    limit: int = Query(900, ge=1, le=5000),
    db: AsyncSession = Depends(get_db),
):
    """
    Returns stored candles for a symbol+market+interval directly from
    PostgreSQL.
    """
    symbol = symbol.upper()

    if interval not in VALID_INTERVALS:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid interval '{interval}'. Must be one of: {', '.join(sorted(VALID_INTERVALS))}",
        )
    if market not in VALID_MARKETS:
        raise HTTPException(status_code=400, detail=f"Invalid market '{market}'.")

    candles = await CandleRepository.get_candles(
        db, symbol=symbol, interval=interval, market=market, limit=limit
    )

    if not candles:
        raise HTTPException(
            status_code=404,
            detail=f"No candles found for {symbol} ({market} {interval}).",
        )

    return [
        [
            c.open_time,
            str(c.open),
            str(c.high),
            str(c.low),
            str(c.close),
            str(c.volume),
            c.close_time,
        ]
        for c in candles
    ]
