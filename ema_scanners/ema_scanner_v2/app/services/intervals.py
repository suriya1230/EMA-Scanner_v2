"""
Shared timeframe set for the Universe Collector (REST bulk backfill, see
universe_collector.py) and Live Candle Stream (WebSocket, see
live_stream.py) — both need to agree on which intervals get collected/
streamed per coin. This is a common multi-timeframe set, not literally
every Binance interval, to keep total request/stream volume manageable
across hundreds of coins (772 coins x 15 intervals would mean ~11,600 REST
fetches per Collect Universe run and 15x the WebSocket streams to shard).
"""
UNIVERSE_INTERVALS = ["5m", "15m", "30m", "1h", "2h", "4h", "6h"]
