import time
from datetime import datetime
from zoneinfo import ZoneInfo

from strategy import MarketMaker, Signals


class Engine:
    """Glue between the feed, the strategy and the order gateway. Every
    book/trade message runs straight through: update signals -> recompute
    quotes -> sync orders, inside the same event-loop callback that read
    the message off the socket. No queues, no threads, no polling."""

    def __init__(self, cfg, book, gateway, position, metrics, pair_info, verbose=True):
        self.cfg = cfg
        self.book = book
        self.gateway = gateway
        self.position = position
        self.metrics = metrics
        self.signals = Signals(cfg)
        self.mm = MarketMaker(cfg, pair_info["tick_size"], pair_info["pair_decimals"], pair_info["ordermin"])
        self.paper = hasattr(gateway, "on_trades")
        self.verbose = verbose
        self.halted = False
        self.quoting = False  # set by check_risk once the book is live and fresh

    async def on_book(self, now_ns, recv_perf_ns):
        if not self.book.is_ready():
            return
        self.signals.on_mid(self.book.mid(), now_ns)
        if self.paper:
            await self.gateway.on_book(now_ns)
        await self._requote(now_ns, recv_perf_ns)

    async def on_trades(self, trades, now_ns, recv_perf_ns):
        self.signals.on_trades(trades, now_ns)
        if self.paper:
            await self.gateway.on_trades(trades, now_ns)
        if self.book.is_ready():
            await self._requote(now_ns, recv_perf_ns)

    async def _requote(self, now_ns, recv_perf_ns):
        if self.halted or not self.quoting or not self.signals.warmed_up():
            return
        bid, ask = self.mm.quotes(self.book, self.signals, self.position.qty, now_ns)
        sent = await self.gateway.sync("buy", bid, now_ns)
        sent |= await self.gateway.sync("sell", ask, now_ns)
        if sent and recv_perf_ns:
            self.metrics.tick_to_quote.record(time.perf_counter_ns() - recv_perf_ns)

    def on_fill(self, side, price, qty, fee):
        self.position.apply_fill(side, price, qty, fee)
        mid = self.book.mid() if self.book.is_ready() else price
        pnl = self.position.pnl(mid)
        if self.verbose:
            print(
                f"\n[FILL] {side.upper()} {qty:.8f} @ {price:.2f} fee={fee:.4f} | "
                f"pos={self.position.qty:.8f} pnl={pnl:+.4f}"
            )
        if pnl <= -self.cfg.MAX_LOSS_USD and not self.halted:
            self.halted = True
            print(f"\n[RISK] loss limit ${self.cfg.MAX_LOSS_USD:.2f} hit -- halting, pulling all quotes")

    async def check_risk(self, now_ns):
        """Periodic checks that don't depend on a market event arriving."""
        if self.book.is_ready() and self.position.pnl(self.book.mid()) <= -self.cfg.MAX_LOSS_USD:
            if not self.halted:
                print(f"\n[RISK] loss limit ${self.cfg.MAX_LOSS_USD:.2f} hit -- halting, pulling all quotes")
            self.halted = True
        stale = not self.book.is_ready() or (now_ns - self.book.last_update_ns) / 1e9 > self.cfg.STALE_BOOK_S
        if stale and self.quoting:
            print("\n[RISK] market data stale -- pulling quotes")
        self.quoting = not stale
        if self.halted or not self.quoting:
            await self.gateway.cancel_all(now_ns)

    def status_line(self):
        if not self.book.is_ready():
            return "waiting for book..."
        bid, _ = self.book.best_bid()
        ask, _ = self.book.best_ask()
        o = self.gateway.orders
        q_bid = f"{o['buy'].price:.1f}" if o["buy"] else "-"
        q_ask = f"{o['sell'].price:.1f}" if o["sell"] else "-"
        last = self.mm.last
        sig = (
            f"imb={last['imb']:+.2f} flow={last['flow']:+.2f} half={last['half_bps']:.1f}bp"
            if last else f"warming up vol ({self.signals.vol_samples}/10)"
        )
        ts = datetime.now(ZoneInfo("America/Los_Angeles")).strftime("%H:%M:%S")
        return (
            f"{ts} mkt {bid:.1f}/{ask:.1f} | ours {q_bid}/{q_ask} | {sig} | "
            f"pos={self.position.qty:.6f} pnl={self.position.pnl(self.book.mid()):+.4f}"
        )
