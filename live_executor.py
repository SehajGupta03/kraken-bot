import csv
import os
import time
from datetime import datetime, timezone
from zoneinfo import ZoneInfo


class LiveExecutor:
    """Places REAL orders on Kraken. Mirrors Portfolio's interface
    (has_position, equity, buy, sell) so bot.py's loop doesn't need to know
    whether it's paper or live. Balance/position are always re-read from
    Kraken itself after every trade -- never trusted from local state,
    unlike Portfolio's simulated cash."""

    def __init__(self, api, pair, max_trade_usd, max_loss_usd, trade_log_file):
        self.api = api
        self.pair = pair
        self.max_trade_usd = max_trade_usd
        self.max_loss_usd = max_loss_usd
        self.trade_log_file = trade_log_file

        pair_info = api.get_asset_pair_info(pair)
        self.base_asset = pair_info["base"]
        self.quote_asset = pair_info["quote"]
        self.ordermin = pair_info["ordermin"]
        self.costmin = pair_info["costmin"]
        self.lot_decimals = pair_info["lot_decimals"]

        self.avg_entry_price = 0.0
        self.realized_loss = 0.0
        self.cash = 0.0
        self.position_qty = 0.0
        self.refresh_balance()

    def refresh_balance(self):
        balances = self.api.get_balance()
        self.cash = balances.get(self.quote_asset, 0.0)
        self.position_qty = balances.get(self.base_asset, 0.0)

    def has_position(self):
        return self.position_qty > 0

    def equity(self, price):
        return self.cash + self.position_qty * price

    def save(self):
        # No local state to persist -- balance/position are always read
        # straight from Kraken, which is the only source of truth in live mode.
        pass

    def trading_halted(self):
        return self.realized_loss >= self.max_loss_usd

    def buy(self, price, spend, fee_rate):
        if self.trading_halted():
            print(f"[LIVE] Loss cap (${self.max_loss_usd:.2f}) reached -- refusing to buy.")
            return None

        spend = min(spend, self.max_trade_usd, self.cash)
        volume = round(spend / price, self.lot_decimals)
        if volume < self.ordermin or volume * price < self.costmin:
            print(
                f"[LIVE] Sized buy (${spend:.2f} -> {volume} {self.base_asset}) is below "
                f"the exchange minimum (ordermin={self.ordermin}, costmin={self.costmin}); skipping."
            )
            return None

        order = self.api.add_order(self.pair, "buy", volume)
        filled = self._await_fill(order)
        if filled is None:
            return None

        self.avg_entry_price = filled["price"]
        self.refresh_balance()
        self._log_trade("BUY", filled)
        return filled["vol_exec"]

    def sell(self, price, fee_rate):
        # Loss cap only blocks new buys; closing an existing position is
        # always allowed so the bot can never trap you in a position.
        if self.position_qty <= 0:
            return None

        volume = round(self.position_qty, self.lot_decimals)
        if volume < self.ordermin:
            print(f"[LIVE] Position ({volume} {self.base_asset}) is below ordermin; can't sell via API.")
            return None

        order = self.api.add_order(self.pair, "sell", volume)
        filled = self._await_fill(order)
        if filled is None:
            return None

        proceeds = filled["vol_exec"] * filled["price"] - filled["fee"]
        cost_basis = filled["vol_exec"] * self.avg_entry_price
        pnl = proceeds - cost_basis
        if pnl < 0:
            self.realized_loss += -pnl

        self.refresh_balance()
        self._log_trade("SELL", filled)
        return filled["vol_exec"]

    def _await_fill(self, order_response, timeout=30, poll_interval=1.0):
        txids = order_response.get("txid") or []
        if not txids:
            print("[LIVE] AddOrder returned no txid; treating as failed.")
            return None
        txid = txids[0]

        deadline = time.time() + timeout
        while time.time() < deadline:
            info = self.api.query_order(txid)
            if info and info.get("status") == "closed":
                return {
                    "txid": txid,
                    "vol_exec": float(info["vol_exec"]),
                    "price": float(info["price"]),
                    "fee": float(info["fee"]),
                }
            if info and info.get("status") in ("canceled", "expired"):
                print(f"[LIVE] Order {txid} ended as '{info['status']}' without filling.")
                return None
            time.sleep(poll_interval)

        print(f"[LIVE] Order {txid} didn't confirm filled within {timeout}s -- check Kraken manually.")
        return None

    def _log_trade(self, side, filled):
        is_new = not os.path.exists(self.trade_log_file)
        with open(self.trade_log_file, "a", newline="") as f:
            writer = csv.writer(f)
            if is_new:
                writer.writerow(["timestamp", "side", "txid", "price", "qty", "fee", "Cash"])
            writer.writerow(
                [
                    datetime.now(ZoneInfo("America/Los_Angeles")).isoformat(),
                    side,
                    filled["txid"],
                    f"{filled['price']:.4f}",
                    f"{filled['vol_exec']:.8f}",
                    f"{filled['fee']:.4f}",
                    f"{self.cash}",
                ]
            )


def run_live_test(api, config):
    """Places one minimum-size buy, then immediately sells it back, to prove
    signing/permissions/order-fill handling all work before trusting the
    bot to run the RSI loop unattended with real money."""
    executor = LiveExecutor(api, config.PAIR, config.LIVE_MAX_TRADE_USD, config.LIVE_MAX_LOSS_USD, "live_test_trades.csv")

    print(f"[LIVE TEST] Balance: {executor.cash:.2f} {executor.quote_asset}, "
          f"{executor.position_qty:.8f} {executor.base_asset}")

    if executor.has_position():
        print("[LIVE TEST] Already holding a position -- aborting so this test doesn't touch it.")
        return

    price = api.get_ticker_price(config.PAIR)
    test_spend = max(executor.ordermin * price, executor.costmin) * 1.05
    print(f"[LIVE TEST] Buying ~${test_spend:.2f} worth of {config.PAIR} @ {price:.2f} ...")

    qty = executor.buy(price, test_spend, 0)
    if qty is None:
        print("[LIVE TEST] Buy failed or was skipped -- stopping before attempting a sell.")
        return
    print(f"[LIVE TEST] Bought {qty:.8f} {executor.base_asset}. Selling it back ...")

    sell_qty = executor.sell(price, 0)
    if sell_qty is None:
        print("[LIVE TEST] Sell failed -- you are holding a small open position, check Kraken manually.")
        return

    print(f"[LIVE TEST] Sold {sell_qty:.8f} {executor.base_asset}. Round trip complete.")
