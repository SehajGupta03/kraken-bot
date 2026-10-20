import csv
import os
from datetime import datetime, timezone


class Position:
    """Inventory and PnL built purely from this bot's own fills, identical in
    paper and live mode. In live mode this deliberately ignores any coins
    already in the account, so the bot can only ever sell what it bought."""

    def __init__(self, starting_cash, trade_log_file):
        self.starting_cash = starting_cash
        self.cash = starting_cash
        self.qty = 0.0
        self.avg_cost = 0.0
        self.fees = 0.0
        self.volume_usd = 0.0
        self.fills = 0
        self.trade_log_file = trade_log_file

    def apply_fill(self, side, price, qty, fee):
        notional = price * qty
        if side == "buy":
            self.avg_cost = (self.avg_cost * self.qty + notional) / (self.qty + qty)
            self.qty += qty
            self.cash -= notional + fee
        else:
            self.qty -= qty
            self.cash += notional - fee
            if self.qty <= 1e-12:
                self.qty, self.avg_cost = 0.0, 0.0
        self.fees += fee
        self.volume_usd += notional
        self.fills += 1
        self._log(side, price, qty, fee)

    def pnl(self, mark):
        return self.cash + self.qty * mark - self.starting_cash

    def _log(self, side, price, qty, fee):
        is_new = not os.path.exists(self.trade_log_file)
        with open(self.trade_log_file, "a", newline="") as f:
            writer = csv.writer(f)
            if is_new:
                writer.writerow(["timestamp", "side", "price", "qty", "fee", "position", "cash"])
            writer.writerow([
                datetime.now(timezone.utc).isoformat(),
                side,
                f"{price:.4f}",
                f"{qty:.8f}",
                f"{fee:.6f}",
                f"{self.qty:.8f}",
                f"{self.cash:.4f}",
            ])
