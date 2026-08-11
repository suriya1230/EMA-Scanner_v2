"""
Universe Summary — for every coin with candles stored via the Universe
Collector (see universe_collector.py), computes an EMA 7/25/99 trend
(Bullish/Bearish/Neutral) purely from what's already in the DB — no Binance
calls. Powers the frontend's Home page: the full list of scanned coins plus
how many are stored/Bullish/Bearish/Neutral overall.
"""
from __future__ import annotations

import pandas as pd
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.services.repository import CandleRepository


def _compute_trend(closes: list[float]) -> tuple[str | None, float | None, float | None, float | None]:
    """Returns (trend, ema_short, ema_mid, ema_long). trend is None when
    there isn't yet enough history to trust EMA_LONG (e.g. a recently
    listed coin)."""
    if len(closes) < settings.EMA_LONG:
        return None, None, None, None
    s = pd.Series(closes)
    ema_short = float(s.ewm(span=settings.EMA_SHORT, adjust=False).mean().iloc[-1])
    ema_mid   = float(s.ewm(span=settings.EMA_MID,   adjust=False).mean().iloc[-1])
    ema_long  = float(s.ewm(span=settings.EMA_LONG,  adjust=False).mean().iloc[-1])
    if ema_short > ema_mid > ema_long:
        trend = "Bullish"
    elif ema_short < ema_mid < ema_long:
        trend = "Bearish"
    else:
        trend = "Neutral"
    return trend, ema_short, ema_mid, ema_long


async def summarize_universe(
    db: AsyncSession, market: str = "futures", interval: str = "1h",
) -> dict:
    closes_by_symbol = await CandleRepository.get_closes_by_symbol(db, interval=interval, market=market)

    bullish = bearish = neutral = insufficient = 0
    coins = []
    for symbol, closes in closes_by_symbol.items():
        trend, ema_short, ema_mid, ema_long = _compute_trend(closes)
        if trend is None:
            insufficient += 1
        elif trend == "Bullish":
            bullish += 1
        elif trend == "Bearish":
            bearish += 1
        else:
            neutral += 1

        coins.append({
            "symbol": symbol,
            "trend": trend,  # Bullish | Bearish | Neutral | None (insufficient data)
            "price": closes[-1],
            "ema_short": ema_short,
            "ema_mid": ema_mid,
            "ema_long": ema_long,
            "candle_count": len(closes),
        })

    coins.sort(key=lambda c: c["symbol"])

    return {
        "market": market,
        "interval": interval,
        "symbols_stored": len(closes_by_symbol),
        "bullish": bullish,
        "bearish": bearish,
        "neutral": neutral,
        "insufficient_data": insufficient,
        "coins": coins,
    }
