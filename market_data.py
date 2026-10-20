import asyncio
import time
from datetime import datetime

import websockets

from orderbook import ChecksumMismatch, OrderBook

try:
    import orjson as _json  # ~3-5x faster than stdlib json on Kraken's messages

    loads = _json.loads
    dumps = lambda obj: _json.dumps(obj).decode()  # noqa: E731
except ImportError:
    import json as _json

    loads = _json.loads
    dumps = _json.dumps

WS_URL = "wss://ws.kraken.com/v2"


def parse_ts_ns(ts):
    """Kraken RFC3339 timestamp ('2024-01-01T00:00:00.123456Z') -> epoch ns."""
    return int(datetime.fromisoformat(ts).timestamp() * 1e9)


class MarketData:
    """Kraken WebSocket v2 public feed: L2 book + trades for one symbol.

    Maintains `self.book` in place and calls the handler's async
    `on_book(now_ns, recv_perf_ns)` / `on_trades(trades, now_ns, recv_perf_ns)`
    after every message, so the strategy sees each update with no
    batching or polling delay. A checksum mismatch or disconnect tears down
    the socket and resubscribes from a fresh snapshot.

    If `record_path` is set, every raw message is appended with its receive
    time so the session can be replayed offline through replay.py.
    """

    def __init__(self, symbol, depth, price_decimals, qty_decimals, metrics, record_path=None):
        self.symbol = symbol
        self.depth = depth
        self.book = OrderBook(depth, price_decimals, qty_decimals)
        self.metrics = metrics
        self.record_file = open(record_path, "a", buffering=1 << 16) if record_path else None
        self.connected = False

    async def run(self, handler):
        backoff = 1
        while True:
            try:
                # compression=None: permessage-deflate costs CPU on every frame
                # for little bandwidth gain at this message rate.
                async with websockets.connect(
                    WS_URL, ping_interval=20, ping_timeout=20, compression=None, max_queue=None
                ) as ws:
                    await ws.send(dumps({
                        "method": "subscribe",
                        "params": {"channel": "book", "symbol": [self.symbol], "depth": self.depth},
                    }))
                    await ws.send(dumps({
                        "method": "subscribe",
                        "params": {"channel": "trade", "symbol": [self.symbol], "snapshot": False},
                    }))
                    self.connected = True
                    backoff = 1
                    async for raw in ws:
                        recv_perf_ns = time.perf_counter_ns()
                        now_ns = time.time_ns()
                        if self.record_file:
                            self.record_file.write(f"{now_ns} {raw}\n")
                        await self.dispatch(raw, now_ns, recv_perf_ns, handler)
            except ChecksumMismatch as e:
                print(f"\n[MD] book checksum mismatch ({e}); resubscribing")
            except (websockets.exceptions.ConnectionClosed, OSError) as e:
                print(f"\n[MD] disconnected ({e}); reconnecting in {backoff}s")
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 30)
            finally:
                self.connected = False
                self.book.last_update_ns = 0

    async def dispatch(self, raw, now_ns, recv_perf_ns, handler):
        msg = loads(raw)
        channel = msg.get("channel")

        if channel == "book":
            for data in msg["data"]:
                if msg["type"] == "snapshot":
                    self.book.apply_snapshot(data)
                else:
                    self.book.apply_update(data)
                    ts = data.get("timestamp")
                    if ts and self.metrics:
                        self.metrics.feed.record(now_ns - parse_ts_ns(ts))
            self.book.last_update_ns = now_ns
            await handler.on_book(now_ns, recv_perf_ns)

        elif channel == "trade" and msg.get("type") == "update":
            trades = msg["data"]
            if self.metrics and trades:
                self.metrics.feed.record(now_ns - parse_ts_ns(trades[-1]["timestamp"]))
            await handler.on_trades(trades, now_ns, recv_perf_ns)
