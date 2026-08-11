"""
EMA Scanner — FastAPI Application

Two independent ways candle data gets into the DB:
  1. CSV uploads (see app/api/csv_backtest.py, app/services/csv_import.py) —
     fetch/store their own candle history per (symbol, interval) pair
     on-demand, for whatever coins/timeframes appear in an uploaded CSV.
  2. The Universe Collector (see app/api/universe.py, app/services/
     universe_collector.py) — bulk-seeds 1h candles for every coin with 24h
     volume above a threshold, independent of any CSV. Once seeded, the
     Live Candle Stream (app/services/live_stream.py) keeps those same
     coins' 1h candles updated in real time over a Binance WebSocket
     connection, started in this file's lifespan and restarted whenever
     Collect Universe finishes (to pick up newly-stored symbols).

There is no auto-detected EMA-crossover *signal* generation any more — the
`/api/signals` and `/api/candles/{symbol}` routes in app/api/scanner.py are
kept as plain DB reads (no dependency on any removed service) so the old
per-coin Details/Backtest/Backtest-Summary pages don't error out if ever
reopened — they just show empty/stale data since nothing writes to the
`signals` table any more. EMA *trend* classification for the Universe
Collector's coins (Bullish/Bearish/Neutral) is computed on read by
app/services/universe_summary.py from stored candles, not stored itself.
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app.core.config import settings
from app.db.database import init_db
from app.services.live_stream import start_live_stream, stop_live_stream

# ─── Logging ──────────────────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.DEBUG if settings.DEBUG else logging.INFO,
    format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)


# ─── Lifespan ─────────────────────────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("=" * 60)
    logger.info("EMA Scanner starting up...")
    logger.info("=" * 60)

    await init_db()
    logger.info("Database tables created/verified.")

    await start_live_stream()

    logger.info("EMA Scanner is ready. 🚀")

    yield  # ── App is running ──

    logger.info("Shutting down...")
    await stop_live_stream()


# ─── App ──────────────────────────────────────────────────────────────────────

app = FastAPI(
    title="EMA Scanner API",
    description="Real-time Binance Spot + USDT Futures EMA crossover scanner.",
    version="2.0.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ─── Routers ──────────────────────────────────────────────────────────────────

from app.api.scanner import router as scanner_router  # noqa: E402
app.include_router(scanner_router)

from app.api.csv_backtest import router as csv_backtest_router  # noqa: E402
app.include_router(csv_backtest_router)

from app.api.universe import router as universe_router  # noqa: E402
app.include_router(universe_router)


# ─── Root ─────────────────────────────────────────────────────────────────────

@app.get("/", include_in_schema=False)
async def root():
    return {"service": "EMA Scanner", "docs": "/docs", "status": "/api/status"}


@app.get("/health", include_in_schema=False)
async def health():
    return JSONResponse({"status": "ok"})
