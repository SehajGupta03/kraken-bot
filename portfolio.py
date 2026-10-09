import csv
import json
import os
from datetime import datetime, timezone


class Portfolio:
    """Simulated (paper) portfolio: no real orders are ever placed."""

    def __init__(self, starting_cash, state_file, trade_log_file):
        self.state_file = state_file
        self.trade_log_file = trade_log_file
        self.cash = starting_cash
        self.position_qty = 0.0
        self.avg_entry_price = 0.0
        self.last_candle_time = None
        self.profit = 0
        self._load()

    def _load(self):
        if os.path.exists(self.state_file):
            with open(self.state_file, "r") as f:
                state = json.load(f)
            self.cash = state["cash"]
            self.position_qty = state["position_qty"]
            self.avg_entry_price = state["avg_entry_price"]
            self.last_candle_time = state["last_candle_time"]
            # self.profit = state["profit"]

    def save(self):
        with open(self.state_file, "w") as f:
            json.dump(
                {
                    "cash": self.cash,
                    "position_qty": self.position_qty,
                    "avg_entry_price": self.avg_entry_price,
                    "last_candle_time": self.last_candle_time,
                    "profit": self.profit,
                },
                f,
                indent=2,
            )

    def equity(self, price):
        return self.cash + self.position_qty * price

    def has_position(self):
        return self.position_qty > 0

    def buy(self, price, spend, fee_rate):
        # cap so spend + fee can never exceed available cash
        spend = min(spend, self.cash / (1 + fee_rate))
        if spend <= 0:
            return None
        fee = spend * fee_rate
        qty = spend / price
        self.cash -= (spend + fee)
        self.avg_entry_price = price
        self.position_qty += qty
        self._log_trade("BUY", price, qty, fee)
        return qty

    def sell(self, price, fee_rate):
        qty = self.position_qty
        if qty <= 0:
            return None
        proceeds = qty * price
        fee = proceeds * fee_rate
        # sell only if the profit > fee
        profit = proceeds - self.avg_entry_price*qty - fee
        if(profit <= 0): return None
        self.profit += profit
        self.cash += proceeds - fee
        self.position_qty = 0.0
        self.avg_entry_price = 0.0
        self._log_trade("SELL", price, qty, fee)
        return qty

    def _log_trade(self, side, price, qty, fee):
        is_new = not os.path.exists(self.trade_log_file)
        with open(self.trade_log_file, "a", newline="") as f:
            writer = csv.writer(f)
            if is_new:
                writer.writerow(["timestamp", "side", "price", "qty", "fee", "cash_after"])
            writer.writerow(
                [
                    datetime.now(timezone.utc).isoformat(),
                    side,
                    f"{price:.4f}",
                    f"{qty:.8f}",
                    f"{fee:.4f}",
                    f"{self.cash:.2f}",
                ]
            )
