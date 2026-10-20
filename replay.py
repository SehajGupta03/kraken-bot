"""Replay a session recorded with `bot.py --record FILE` through the exact same
book, strategy and paper-exchange code, using recorded receive times as the
clock. Lets you change config (spreads, alphas, latency, fees) and compare
results on identical market data in seconds instead of re-running live.

    python replay.py session.jsonl
    python replay.py session.jsonl --set MAKER_FEE=0.0 --set SIM_LATENCY_MS=5
"""
import argparse
import asyncio
import time

import config
from engine import Engine
from execution import PaperExchange
from kraken_api import KrakenAPI
from latency import Metrics
from market_data import MarketData
from orderbook import ChecksumMismatch
from position import Position


async def replay(path, pair_info):
    metrics = Metrics()
    md = MarketData(config.WS_SYMBOL, config.BOOK_DEPTH, pair_info["pair_decimals"], pair_info["lot_decimals"], metrics)
    position = Position(config.STARTING_CASH, "replay_" + config.TRADE_LOG_FILE)
    gateway = PaperExchange(config, md.book, on_fill=None)
    engine = Engine(config, md.book, gateway, position, metrics, pair_info, verbose=False)
    gateway.on_fill = engine.on_fill

    messages = 0
    last_risk_ns = 0
    started = time.perf_counter()
    with open(path) as f:
        for line in f:
            now_ns, raw = line.split(" ", 1)
            now_ns = int(now_ns)
            try:
                await md.dispatch(raw, now_ns, time.perf_counter_ns(), engine)
            except ChecksumMismatch as e:
                print(f"checksum mismatch at {now_ns} ({e}); waiting for next snapshot")
            if now_ns - last_risk_ns >= 1_000_000_000:
                await engine.check_risk(now_ns)
                last_risk_ns = now_ns
            messages += 1
    elapsed = time.perf_counter() - started

    mark = md.book.mid() if md.book.is_ready() else 0.0
    print(f"replayed {messages} messages in {elapsed:.2f}s ({messages / max(elapsed, 1e-9):,.0f} msg/s)")
    print(
        f"fills={position.fills} volume=${position.volume_usd:.2f} fees=${position.fees:.4f} "
        f"position={position.qty:.8f} pnl={position.pnl(mark):+.4f}"
    )
    print(metrics.tick_to_quote.summary())


def apply_overrides(overrides):
    for item in overrides:
        key, value = item.split("=", 1)
        if not hasattr(config, key):
            raise SystemExit(f"unknown config key {key}")
        setattr(config, key, type(getattr(config, key))(value))


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("file")
    parser.add_argument("--set", action="append", default=[], metavar="KEY=VALUE", help="override a config value")
    args = parser.parse_args()
    apply_overrides(args.set)
    asyncio.run(replay(args.file, KrakenAPI().get_asset_pair_info(config.PAIR)))
