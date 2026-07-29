"""
Database repository — all DB reads/writes go through here.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from sqlalchemy import delete, func, select, tuple_
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.models import Candle, ImportedSignal
from app.core.config import settings

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
#  Candle Repository  (market+interval aware — spot/futures each store their own candles)
# ─────────────────────────────────────────────────────────────────────────────

class CandleRepository:

    # asyncpg/Postgres hard-caps a single query at 32,767 bind parameters. Each
    # candle row binds 10 columns, so anything past ~3,276 rows in one INSERT
    # blows past that and the whole upsert fails with "the number of query
    # arguments cannot exceed 32767" — silently losing the entire batch (the
    # exception is raised before any commit). Deep on-demand backfills (see
    # ensure_depth in csv_import.py) can easily fetch 10,000+ rows in one
    # call, so this chunks with a comfortable margin below that ceiling.
    _UPSERT_CHUNK_SIZE = 2000

    @staticmethod
    async def upsert_candles(session: AsyncSession, rows: list[dict]) -> int:
        """
        Bulk upsert candles. Each row dict must include 'market' and 'interval'.
        Uses constraint: uq_candle_symbol_market_interval_open_time.
        """
        if not rows:
            return 0
        chunk_size = CandleRepository._UPSERT_CHUNK_SIZE
        for i in range(0, len(rows), chunk_size):
            chunk = rows[i:i + chunk_size]
            stmt = pg_insert(Candle).values(chunk)
            stmt = stmt.on_conflict_do_update(
                constraint="uq_candle_symbol_market_interval_open_time",
                set_={
                    "open":       stmt.excluded.open,
                    "high":       stmt.excluded.high,
                    "low":        stmt.excluded.low,
                    "close":      stmt.excluded.close,
                    "volume":     stmt.excluded.volume,
                    "close_time": stmt.excluded.close_time,
                },
            )
            await session.execute(stmt)
        return len(rows)

    @staticmethod
    async def upsert_single_candle(session: AsyncSession, candle_dict: dict) -> None:
        await CandleRepository.upsert_candles(session, [candle_dict])

    @staticmethod
    async def get_candles(
        session: AsyncSession,
        symbol: str,
        interval: str = "1h",
        limit: int = settings.CANDLES_LIMIT,
        market: str = "futures",
    ) -> list[Candle]:
        """Return the most-recent `limit` candles for symbol+market+interval, sorted oldest→newest."""
        result = await session.execute(
            select(Candle)
            .where(Candle.symbol == symbol, Candle.market == market, Candle.interval == interval)
            .order_by(Candle.open_time.desc())
            .limit(limit)
        )
        return list(reversed(result.scalars().all()))

    @staticmethod
    async def get_open_times(
        session: AsyncSession,
        symbol: str,
        interval: str = "1h",
        market: str = "futures",
    ) -> list[int]:
        """All stored open_time values for symbol+market+interval, ascending —
        used to detect gaps left by a previously interrupted/partial fetch."""
        result = await session.execute(
            select(Candle.open_time)
            .where(Candle.symbol == symbol, Candle.market == market, Candle.interval == interval)
            .order_by(Candle.open_time.asc())
        )
        return list(result.scalars().all())

    @staticmethod
    async def get_latest_open_time(
        session: AsyncSession,
        symbol: str,
        interval: str = "1h",
        market: str = "futures",
    ) -> int | None:
        result = await session.execute(
            select(Candle.open_time)
            .where(Candle.symbol == symbol, Candle.market == market, Candle.interval == interval)
            .order_by(Candle.open_time.desc())
            .limit(1)
        )
        return result.scalar_one_or_none()

    @staticmethod
    async def count_candles(
        session: AsyncSession,
        symbol: str,
        interval: str = "1h",
        market: str = "futures",
    ) -> int:
        result = await session.execute(
            select(func.count())
            .select_from(Candle)
            .where(Candle.symbol == symbol, Candle.market == market, Candle.interval == interval)
        )
        return result.scalar_one()

    @staticmethod
    async def distinct_symbols(session: AsyncSession, market: str = "futures") -> list[str]:
        """All symbols that have at least one stored candle for this market."""
        result = await session.execute(
            select(Candle.symbol).where(Candle.market == market).distinct()
        )
        return list(result.scalars().all())

    @staticmethod
    async def prune_old_candles(
        session: AsyncSession,
        symbol: str,
        interval: str = "1h",
        keep: int = settings.CANDLES_LIMIT,
        market: str = "futures",
    ) -> None:
        result = await session.execute(
            select(Candle.open_time)
            .where(Candle.symbol == symbol, Candle.market == market, Candle.interval == interval)
            .order_by(Candle.open_time.desc())
            .offset(keep)
            .limit(1)
        )
        cutoff = result.scalar_one_or_none()
        if cutoff is not None:
            await session.execute(
                delete(Candle).where(
                    Candle.symbol == symbol,
                    Candle.market == market,
                    Candle.interval == interval,
                    Candle.open_time <= cutoff,
                )
            )


# ─────────────────────────────────────────────────────────────────────────────
#  Imported Signal Repository  (CSV Backtest upload — kept fully separate
#  from the scanner's own `signals` table; see ImportedSignal model docstring)
# ─────────────────────────────────────────────────────────────────────────────

class ImportedSignalRepository:

    @staticmethod
    async def replace_all(session: AsyncSession, rows: list[dict]) -> int:
        """Wipes any previously-imported CSV signals and inserts this new
        batch — each CSV upload is treated as a fresh, standalone dataset
        rather than something that accumulates across uploads."""
        await session.execute(delete(ImportedSignal))
        if not rows:
            return 0
        await session.execute(pg_insert(ImportedSignal).values(rows))
        return len(rows)

    @staticmethod
    async def get_all(session: AsyncSession) -> list[ImportedSignal]:
        result = await session.execute(
            select(ImportedSignal).order_by(ImportedSignal.cross_time.asc())
        )
        return list(result.scalars().all())