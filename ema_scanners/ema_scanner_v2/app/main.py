"""
EMA Scanner — FastAPI Application

CSV-only mode: the app no longer auto-fetches Binance candles or auto-detects
EMA crossovers for a tracked symbol universe. All signals come from CSV
uploads (see app/api/csv_backtest.py, app/services/csv_import.py), which
fetch/store their own candle history per (symbol, interval) pair on demand.

The `/api/signals` and `/api/candles/{symbol}` routes in app/api/scanner.py
are kept as plain DB reads (no dependency on any removed service) so the
old per-coin Details/Backtest/Backtest-Summary pages don't error out if
ever reopened — they just show empty/stale data since nothing writes to
the `signals` table anymore.
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app.core.config import settings
from app.db.database import init_db

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

    logger.info("EMA Scanner is ready. 🚀")

    yield  # ── App is running ──

    logger.info("Shutting down...")


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


# ─── Root ─────────────────────────────────────────────────────────────────────

@app.get("/", include_in_schema=False)
async def root():
    return {"service": "EMA Scanner", "docs": "/docs", "status": "/api/status"}


@app.get("/health", include_in_schema=False)
async def health():
    return JSONResponse({"status": "ok"})
