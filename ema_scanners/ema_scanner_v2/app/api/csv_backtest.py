"""
CSV Backtest API — upload a CSV of coin signals (possibly mixing many
timeframes), fetch each coin's Binance history into Postgres at whatever
timeframe each signal uses, and read it all back for display.

POST /api/csv-import    — upload a .csv file, replaces the current import
GET  /api/csv-signals   — signals from the most recently uploaded CSV
GET  /api/csv-candles/{symbol} — candles for a CSV-imported (symbol, interval)
                                  pair; unlike /api/candles/{symbol}, accepts
                                  ANY timeframe (5m/30m/etc.), not just the
                                  scanner's fixed set.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, File, HTTPException, Query, UploadFile
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.database import get_db
from app.services.csv_import import VALID_BINANCE_INTERVALS, ensure_depth, import_csv
from app.services.repository import CandleRepository, ImportedSignalRepository

router = APIRouter(prefix="/api", tags=["csv-backtest"])


@router.post("/csv-import")
async def upload_csv(file: UploadFile = File(...)):
    if not file.filename.lower().endswith(".csv"):
        raise HTTPException(status_code=400, detail="Please upload a .csv file.")
    raw = await file.read()
    return await import_csv(raw)


@router.get("/csv-signals")
async def get_csv_signals(db: AsyncSession = Depends(get_db)):
    signals = await ImportedSignalRepository.get_all(db)
    return [
        {
            "symbol": s.symbol,
            "signal_type": s.signal_type,
            "interval": s.interval,
            "cross_time": s.cross_time.isoformat(),
            "cross_price": s.cross_price,
            "ema_fast": s.ema_fast,
            "ema_mid": s.ema_mid,
            "ema_slow": s.ema_slow,
            "score": s.score,
        }
        for s in signals
    ]


@router.post("/csv-candles/{symbol}/ensure-depth")
async def ensure_candle_depth(
    symbol: str,
    interval: str = Query(...),
    days: int = Query(..., ge=1, le=90),
):
    """Called when a user opens a coin's Backtest Details page and picks a
    Window (7d/14d/30d) — extends that ONE (symbol, interval) pair's stored
    history backward if it doesn't already cover `days`, fetching directly
    from Binance. See ensure_depth()'s docstring for why this is on-demand
    rather than done for every coin during the bulk CSV import."""
    symbol = symbol.upper()
    if interval not in VALID_BINANCE_INTERVALS:
        raise HTTPException(status_code=400, detail=f"Invalid interval '{interval}'.")
    return await ensure_depth(symbol, interval, days)


@router.get("/csv-candles/{symbol}")
async def get_csv_candles(
    symbol: str,
    interval: str = Query(...),
    limit: int = Query(1000, ge=1, le=20_000),
    db: AsyncSession = Depends(get_db),
):
    """Same shape as /api/candles/{symbol}, but accepts any Binance-valid
    timeframe — the scanner's endpoint is deliberately restricted to its own
    fixed set of intervals, which would reject a CSV's 5m/30m signals."""
    symbol = symbol.upper()
    if interval not in VALID_BINANCE_INTERVALS:
        raise HTTPException(status_code=400, detail=f"Invalid interval '{interval}'.")

    candles = await CandleRepository.get_candles(
        db, symbol=symbol, interval=interval, market="futures", limit=limit
    )
    if not candles:
        raise HTTPException(status_code=404, detail=f"No candles found for {symbol} ({interval}).")

    return [
        [c.open_time, str(c.open), str(c.high), str(c.low), str(c.close), str(c.volume), c.close_time]
        for c in candles
    ]
