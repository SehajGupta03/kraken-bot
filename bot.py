import argparse
import asyncio
import os
import sys
import time

import config
from engine import Engine
from execution import LiveGateway, PaperExchange
from kraken_api import KrakenAPI
from latency import Metrics
from market_data import MarketData
from position import Position

LIVE_COUNTDOWN_SECONDS = 10


def confirm_live_trading():
    if os.environ.get("LIVE_TRADING_CONFIRMED") != "yes":
        sys.exit(
            "[LIVE] Refusing to start: set the LIVE_TRADING_CONFIRMED=yes environment "
            "variable to confirm you want to trade with real money."
        )


async def periodic(engine, metrics, interval_s):
    last_stats = time.monotonic()
    while True:
        await asyncio.sleep(1)
        await engine.check_risk(time.time_ns())
        print(f"\r{engine.status_line()}   ", end="", flush=True)
        if time.monotonic() - last_stats >= interval_s:
            last_stats = time.monotonic()
            p = engine.position
            print(f"\n[STATS] fills={p.fills} volume=${p.volume_usd:.2f} fees=${p.fees:.4f}\n{metrics.report()}")


async def run(live, record_path):
    api = KrakenAPI()
    pair_info = api.get_asset_pair_info(config.PAIR)
    metrics = Metrics()
    md = MarketData(
        config.WS_SYMBOL, config.BOOK_DEPTH, pair_info["pair_decimals"],
        pair_info["lot_decimals"], metrics, record_path,
    )

    if live:
        confirm_live_trading()
        balances = api.get_balance()
        cash = balances.get(pair_info["quote"], 0.0)
        position = Position(cash, config.TRADE_LOG_FILE)
        gateway = LiveGateway(config, api, config.WS_SYMBOL, metrics, on_fill=None)
        print(
            f"\n*** LIVE MARKET MAKING -- REAL MONEY ***\n"
            f"{config.PAIR} | {cash:.2f} {pair_info['quote']} available | "
            f"quote size {config.QUOTE_SIZE} | max position {config.MAX_POSITION} | "
            f"loss limit ${config.MAX_LOSS_USD:.2f}\n"
            f"Starting in {LIVE_COUNTDOWN_SECONDS}s -- Ctrl+C now to abort.\n"
        )
        for remaining in range(LIVE_COUNTDOWN_SECONDS, 0, -1):
            print(f"\r{remaining}...", end="", flush=True)
            await asyncio.sleep(1)
        print()
    else:
        position = Position(config.STARTING_CASH, "paper_" + config.TRADE_LOG_FILE)
        gateway = PaperExchange(config, md.book, on_fill=None)
        print(
            f"Paper market making {config.PAIR} | simulated order latency {config.SIM_LATENCY_MS}ms | "
            f"maker fee {config.MAKER_FEE * 1e4:.1f}bp\nNo real orders will be placed. Ctrl+C to stop.\n"
        )

    engine = Engine(config, md.book, gateway, position, metrics, pair_info)
    gateway.on_fill = engine.on_fill

    tasks = [asyncio.create_task(md.run(engine)), asyncio.create_task(periodic(engine, metrics, config.STATS_INTERVAL_S))]
    if live:
        tasks.append(asyncio.create_task(gateway.run()))
    try:
        await asyncio.gather(*tasks)
    finally:
        for t in tasks:
            t.cancel()
        if live:
            await gateway.shutdown()
        mark = md.book.mid() if md.book.is_ready() else 0.0
        print(
            f"\n\nStopped. fills={position.fills} volume=${position.volume_usd:.2f} "
            f"fees=${position.fees:.4f} position={position.qty:.8f} pnl={position.pnl(mark):+.4f}"
        )
        print(metrics.report())
        if live and position.qty > 0:
            print(f"[LIVE] Still holding {position.qty:.8f} bought by the bot -- flatten manually if desired.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Low-latency Kraken market maker")
    parser.add_argument(
        "--live",
        action="store_true",
        help="Quote with REAL MONEY via Kraken's authenticated WebSocket. "
        "Requires LIVE_TRADING_CONFIRMED=yes and KRAKEN_API_KEY/KRAKEN_API_SECRET.",
    )
    parser.add_argument(
        "--record",
        metavar="FILE",
        help="Append every raw market data message (with receive time) to FILE "
        "for offline replay with replay.py.",
    )
    args = parser.parse_args()

    try:
        import uvloop  # Linux/macOS only; a faster drop-in event loop
        uvloop.install()
    except ImportError:
        pass

    try:
        asyncio.run(run(live=args.live, record_path=args.record))
    except KeyboardInterrupt:
        pass
