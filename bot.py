import argparse
import asyncio
import os
import sys
import time
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import config
from kraken_api import KrakenAPI
from kraken_ws import stream_ohlc
from live_executor import LiveExecutor, run_live_test
from portfolio import Portfolio
from sizing import calculate_spend
from strategy import compute_rsi, generate_signal

DEMO_INTERVAL_MINUTES = 1
DEMO_RSI_OVERSOLD = 45
DEMO_RSI_OVERBOUGHT = 55

HISTORY_LIMIT = 500
LIVE_COUNTDOWN_SECONDS = 10


def confirm_live_trading():
    if os.environ.get("LIVE_TRADING_CONFIRMED") != "yes":
        sys.exit(
            "[LIVE] Refusing to start: set the LIVE_TRADING_CONFIRMED=yes environment "
            "variable to confirm you want to trade with real money."
        )


async def run(demo=False, live=False):
    interval_minutes = DEMO_INTERVAL_MINUTES if demo else config.INTERVAL_MINUTES
    rsi_oversold = DEMO_RSI_OVERSOLD if demo else config.RSI_OVERSOLD
    rsi_overbought = DEMO_RSI_OVERBOUGHT if demo else config.RSI_OVERBOUGHT

    api = KrakenAPI()

    if live:
        confirm_live_trading()
        portfolio = LiveExecutor(
            api, config.PAIR, config.LIVE_MAX_TRADE_USD, config.LIVE_MAX_LOSS_USD, config.TRADE_LOG_FILE
        )
        print(
            f"\n*** LIVE TRADING -- REAL MONEY ***\n"
            f"Pair: {config.PAIR} | Balance: {portfolio.cash:.2f} {portfolio.quote_asset}, "
            f"{portfolio.position_qty:.8f} {portfolio.base_asset}\n"
            f"Caps: max {config.LIVE_MAX_TRADE_USD:.2f} per trade, "
            f"{config.LIVE_MAX_LOSS_USD:.2f} max realized loss before new buys are blocked.\n"
            f"Starting in {LIVE_COUNTDOWN_SECONDS}s -- Ctrl+C now to abort.\n"
        )
        for remaining in range(LIVE_COUNTDOWN_SECONDS, 0, -1):
            print(f"\r{remaining}...", end="", flush=True)
            time.sleep(1)
        print()
    else:
        state_file = "demo_" + config.STATE_FILE if demo else config.STATE_FILE
        trade_log_file = "demo_" + config.TRADE_LOG_FILE if demo else config.TRADE_LOG_FILE
        portfolio = Portfolio(config.STARTING_CASH, state_file, trade_log_file)

    # One-time REST call to seed RSI history. WebSocket snapshots only cover
    # recent trades and can have gaps on a low-volume pair, so this guarantees
    # enough contiguous candles before switching to WebSocket-only updates.
    candles = api.get_ohlc(config.PAIR, interval_minutes)
    closes = [float(c[4]) for c in candles]

    if not live:
        print(f"Paper trading {config.PAIR} over WebSocket | starting equity: {portfolio.cash:.2f}")
        if demo:
            print(
                f"[DEMO MODE] thresholds loosened to {rsi_oversold}/{rsi_overbought} so a signal "
                "fires sooner. Still 100% simulated, using a separate demo_ state file."
            )
        print("No real orders will be placed. Press Ctrl+C to stop.\n")

    try:
        async for event in stream_ohlc(config.WS_SYMBOL, interval_minutes):
            price = event["price"]

            if not event["new_candle"]:
                timestamp = datetime.now(ZoneInfo("America/Los_Angeles")).strftime("%H:%M:%S")
                print(
                    f"\r{timestamp} live price={price:.2f} equity={portfolio.equity(price):.2f}   ",
                    end="",
                    flush=True,
                )
                continue

            closed_price = event["closed_price"]
            closes.append(closed_price)
            closes = closes[-HISTORY_LIMIT:]

            rsi = compute_rsi(closes, config.RSI_PERIOD)
            signal = generate_signal(rsi, portfolio.has_position(), rsi_oversold, rsi_overbought)
            # signal = generate_signal(portfolio.has_position())

            if signal == "BUY":
                spend = calculate_spend(portfolio, closed_price, config)
                qty = portfolio.buy(closed_price, spend, config.KRAKEN_TAKER_FEE)
                if qty is not None:
                    print(f"\n[TRADE] BUY {qty:.6f} {config.PAIR} @ {closed_price:.2f} (spent {spend:.2f})")
            elif signal == "SELL":
                qty = portfolio.sell(closed_price, config.KRAKEN_TAKER_FEE)
                if(qty is not None):
                    print(f"\n[TRADE] SELL {qty:.6f} {config.PAIR} @ {closed_price:.2f}")

            portfolio.save()

            rsi_display = f"{rsi:.1f}" if rsi is not None else "n/a"
            timestamp = datetime.now(ZoneInfo("America/Los_Angeles")).strftime("%Y-%m-%d %H:%M:%S")
            print(
                f"\n{timestamp} | candle close={closed_price:.2f} rsi={rsi_display} "
                f"cash={portfolio.cash:.2f} position={portfolio.position_qty:.6f} "
                f"equity={portfolio.equity(closed_price):.2f}"
            )
    finally:
        portfolio.save()
        if live:
            print(f"Realized loss this session: {portfolio.realized_loss:.2f} (cap {config.LIVE_MAX_LOSS_USD:.2f})")
            print("\nStopped. Live balance is whatever Kraken currently shows -- no local state to lose.")
        else:
            print("Profit: ", portfolio.profit)
            print("\nStopped. Portfolio state saved.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--demo",
        action="store_true",
        help="Use 1-minute candles and loosened RSI thresholds (45/55) so a "
        "simulated buy/sell fires within minutes instead of waiting for a real "
        "oversold/overbought swing. Still paper trading, using separate demo_ state files.",
    )
    parser.add_argument(
        "--live",
        action="store_true",
        help="Trade with REAL MONEY on Kraken. Requires LIVE_TRADING_CONFIRMED=yes "
        "in the environment. Incompatible with --demo.",
    )
    parser.add_argument(
        "--live-test",
        action="store_true",
        help="Place one minimum-size real buy then immediately sell it back, to "
        "validate credentials/permissions/fill-handling before running --live "
        "unattended. Also requires LIVE_TRADING_CONFIRMED=yes.",
    )
    args = parser.parse_args()

    if args.live and args.demo:
        sys.exit("--live and --demo are mutually exclusive.")

    if args.live_test:
        confirm_live_trading()
        run_live_test(KrakenAPI(), config)
    else:
        try:
            asyncio.run(run(demo=args.demo, live=args.live))
        except KeyboardInterrupt:
            pass
