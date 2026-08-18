"""
Universe Collector API — bulk-seeds 1h candle history for every Binance
USDT-M Futures perpetual with 24h volume above a threshold, independent of
the CSV upload flow. Lets a full historical dataset be built up front so
different strategies can be tried/backtested against any qualifying coin.

POST /api/collect-universe — scans + fetches, returns a summary once done
                              (min-volume used / symbols scanned / stored /
                              errors). Can take a while for hundreds of coins.
                              Also runs automatically on a timer (see
                              universe_collector.py's scheduler) — this route
                              and that scheduler share one lock, so a manual
                              click while a scheduled run is mid-flight
                              returns 409 instead of overlapping it.
GET  /api/universe-summary  — every stored coin's EMA 7/25/99 trend plus
                              overall Bullish/Bearish/Neutral/stored counts,
                              computed from DB data only (no Binance calls).
                              Powers the frontend Home page.
GET  /api/live-stream-status — whether the Live Candle Stream (see
                              app/services/live_stream.py) is currently
                              connected, and how many WebSocket connections
                              it's split across.
"""
from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.database import get_db
from app.services.live_stream import restart_live_stream, status as live_stream_status
from app.services.universe_collector import run_collect_universe_guarded
from app.services.universe_summary import summarize_universe

router = APIRouter(prefix="/api", tags=["universe"])


@router.post("/collect-universe")
async def collect_universe_endpoint(
    min_volume_usdt: Optional[float] = Query(
        None, ge=0,
        description="Minimum 24h quote volume in USDT. Defaults to MIN_VOLUME_USDT_SIGNAL from .env (1,000,000).",
    ),
):
    result = await run_collect_universe_guarded(min_volume_usdt)
    if result is None:
        raise HTTPException(status_code=409, detail="Universe collection already in progress (scheduled or manual) — try again shortly.")
    # Re-subscribe the live WebSocket stream so any newly-stored symbols
    # start getting real-time updates without needing a backend restart.
    await restart_live_stream()
    return result


@router.get("/universe-summary")
async def universe_summary_endpoint(
    interval: str = Query("1h"),
    market: str = Query("futures"),
    db: AsyncSession = Depends(get_db),
):
    return await summarize_universe(db, market=market, interval=interval)


@router.get("/live-stream-status")
async def live_stream_status_endpoint():
    return live_stream_status()
