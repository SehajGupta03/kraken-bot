import asyncio
import itertools
import time
import uuid

import websockets

from market_data import dumps, loads

WS_AUTH_URL = "wss://ws-auth.kraken.com/v2"


class Order:
    __slots__ = (
        "side", "price", "qty", "filled", "cl_ord_id", "order_id",
        "active_at_ns", "queue_ahead", "next", "cancel_at_ns", "pending",
    )

    def __init__(self, side, price, qty):
        self.side = side
        self.price = price
        self.qty = qty
        self.filled = 0.0
        self.cl_ord_id = str(uuid.uuid4())
        self.order_id = None
        self.active_at_ns = 0        # paper: when the exchange "sees" it
        self.queue_ahead = None      # paper: resting size ahead of us at our price
        self.next = None             # paper: (price, qty, effective_ns) pending amend
        self.cancel_at_ns = None     # paper: when a cancel takes effect
        self.pending = False         # live: a request is in flight, don't stack another

    @property
    def remaining(self):
        return self.qty - self.filled


class Gateway:
    """Keeps at most one working order per side in line with the strategy's
    desired quotes. Shared throttling: cancels are always sent immediately
    (they reduce risk); new prices only go out when the quote has drifted
    by more than REQUOTE_THRESHOLD_BPS and the side hasn't been touched for
    MIN_REQUOTE_INTERVAL_MS. Amend is preferred over cancel/replace -- one
    message instead of two, and no window with no quote on the book."""

    def __init__(self, cfg, on_fill):
        self.cfg = cfg
        self.on_fill = on_fill
        self.orders = {"buy": None, "sell": None}
        self.last_change_ns = {"buy": 0, "sell": 0}
        self.min_interval_ns = cfg.MIN_REQUOTE_INTERVAL_MS * 1_000_000
        self.paused_until_ns = 0

    async def sync(self, side, quote, now_ns):
        """Returns True if an order message was sent."""
        order = self.orders[side]
        if quote is None:
            if order is not None and order.cancel_at_ns is None and not order.pending:
                await self._cancel(order, now_ns)
                return True
            return False
        if now_ns < self.paused_until_ns:
            return False
        if order is None:
            self.orders[side] = order = Order(side, quote.price, quote.qty)
            self.last_change_ns[side] = now_ns
            await self._place(order, now_ns)
            return True
        if order.pending or order.cancel_at_ns is not None:
            return False
        if now_ns - self.last_change_ns[side] < self.min_interval_ns:
            return False
        working = order.next[0] if order.next else order.price
        drift_bps = abs(quote.price - working) / working * 1e4
        if drift_bps < self.cfg.REQUOTE_THRESHOLD_BPS and abs(quote.qty - order.remaining) < 1e-12:
            return False
        self.last_change_ns[side] = now_ns
        await self._amend(order, quote.price, quote.qty, now_ns)
        return True

    async def cancel_all(self, now_ns):
        for side in ("buy", "sell"):
            await self.sync(side, None, now_ns)


class PaperExchange(Gateway):
    """Simulated exchange that replays our quotes against the real public
    feed, modelling the two things that make naive paper trading lie:

    latency:  every place/amend/cancel takes SIM_LATENCY_MS to reach the
              exchange; until then the *old* state of the order is what can
              trade (so slow cancels get picked off, as in real life).
    queue:    a new or repriced order joins the back of the queue at its
              price. It only fills at that price once the resting size
              that was ahead of it has traded away. Trades strictly through
              our price fill us immediately.
    """

    def __init__(self, cfg, book, on_fill):
        super().__init__(cfg, on_fill)
        self.book = book
        self.latency_ns = cfg.SIM_LATENCY_MS * 1_000_000
        self.dying = []  # cancelled orders still live until the cancel lands

    async def _place(self, order, now_ns):
        order.active_at_ns = now_ns + self.latency_ns

    async def _amend(self, order, price, qty, now_ns):
        # order.price stays at the resting price until the amend lands.
        order.next = (price, qty, now_ns + self.latency_ns)

    async def _cancel(self, order, now_ns):
        order.cancel_at_ns = now_ns + self.latency_ns
        self.orders[order.side] = None
        self.dying.append(order)

    def _live_orders(self, now_ns):
        self.dying = [o for o in self.dying if now_ns < o.cancel_at_ns and o.remaining > 1e-12]
        for o in itertools.chain(self.orders.values(), self.dying):
            if o is None or now_ns < o.active_at_ns:
                continue
            if o.queue_ahead is None:
                o.queue_ahead = self.book.qty_at(o.side, o.price)
            if o.next and now_ns >= o.next[2]:
                o.price, new_qty, _ = o.next
                o.qty = o.filled + new_qty
                o.next = None
                o.queue_ahead = self.book.qty_at(o.side, o.price)  # repricing loses priority
            yield o

    async def on_book(self, now_ns):
        for o in self._live_orders(now_ns):
            # Size ahead of us can only shrink (cancels/fills in front of us);
            # anything added at our level lands behind us.
            o.queue_ahead = min(o.queue_ahead, self.book.qty_at(o.side, o.price))

    async def on_trades(self, trades, now_ns):
        live = list(self._live_orders(now_ns))
        for t in trades:
            for o in live:
                price = o.price
                if o.remaining <= 1e-12:
                    continue
                # Our bid is hit by aggressive sells, our ask lifted by aggressive buys.
                if o.side == "buy" and t["side"] == "sell" and t["price"] <= price:
                    through = t["price"] < price
                elif o.side == "sell" and t["side"] == "buy" and t["price"] >= price:
                    through = t["price"] > price
                else:
                    continue
                qty = t["qty"]
                if not through:
                    available = qty - o.queue_ahead
                    o.queue_ahead = max(0.0, o.queue_ahead - qty)
                    if available <= 1e-12:  # float residue, not a real fill
                        continue
                    qty = available
                fill = min(qty, o.remaining)
                o.filled += fill
                fee = fill * price * self.cfg.MAKER_FEE
                self.on_fill(o.side, price, fill, fee)
                if o.remaining <= 1e-12 and self.orders[o.side] is o:
                    self.orders[o.side] = None


class LiveGateway(Gateway):
    """Order entry over Kraken's authenticated WebSocket v2 -- no REST in the
    hot path. One persistent TLS connection carries add/amend/cancel
    requests and the executions feed that reports fills back.

    Safety:
      - every quote is post_only, so it can never take liquidity / pay taker
      - cancel_all on connect and on shutdown
      - cancel_all_orders_after (dead man's switch): if this process hangs
        or loses connectivity, Kraken cancels every resting order itself
    """

    def __init__(self, cfg, api, symbol, metrics, on_fill):
        super().__init__(cfg, on_fill)
        self.api = api
        self.symbol = symbol
        self.metrics = metrics
        self.ws = None
        self.token = None
        self.req_ids = itertools.count(1)
        self.inflight = {}  # req_id -> (send perf_ns, method, order)
        self.seen_exec_ids = set()
        self.ready = asyncio.Event()

    async def run(self):
        backoff = 1
        while True:
            try:
                self.token = self.api.get_ws_token()
                async with websockets.connect(
                    WS_AUTH_URL, ping_interval=20, ping_timeout=20, compression=None, max_queue=None
                ) as ws:
                    self.ws = ws
                    await self._send("cancel_all", {})
                    await ws.send(dumps({
                        "method": "subscribe",
                        "params": {"channel": "executions", "token": self.token,
                                   "snap_orders": False, "snap_trades": False},
                    }))
                    heartbeat = asyncio.create_task(self._dead_man_switch())
                    self.ready.set()
                    backoff = 1
                    try:
                        async for raw in ws:
                            self._handle(loads(raw))
                    finally:
                        heartbeat.cancel()
            except (websockets.exceptions.ConnectionClosed, OSError) as e:
                print(f"\n[LIVE] order connection lost ({e}); reconnecting in {backoff}s")
            finally:
                # Kraken's dead man's switch will cancel anything left resting;
                # forget local state and start clean on reconnect.
                self.ready.clear()
                self.ws = None
                self.orders = {"buy": None, "sell": None}
                self.inflight.clear()
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 30)

    async def _dead_man_switch(self):
        while True:
            await self._send("cancel_all_orders_after", {"timeout": self.cfg.DEAD_MAN_TIMEOUT_S})
            await asyncio.sleep(self.cfg.DEAD_MAN_TIMEOUT_S / 3)

    async def _send(self, method, params, order=None):
        if self.ws is None:
            return
        req_id = next(self.req_ids)
        params["token"] = self.token
        self.inflight[req_id] = (time.perf_counter_ns(), method, order)
        await self.ws.send(dumps({"method": method, "params": params, "req_id": req_id}))

    async def sync(self, side, quote, now_ns):
        if not self.ready.is_set():
            return False
        return await super().sync(side, quote, now_ns)

    async def _place(self, order, now_ns):
        order.pending = True
        await self._send("add_order", {
            "order_type": "limit", "side": order.side, "symbol": self.symbol,
            "limit_price": order.price, "order_qty": order.qty,
            "post_only": True, "cl_ord_id": order.cl_ord_id,
        }, order)

    async def _amend(self, order, price, qty, now_ns):
        order.pending = True
        await self._send("amend_order", {
            "cl_ord_id": order.cl_ord_id, "limit_price": price,
            "order_qty": order.filled + qty, "post_only": True,
        }, order)
        order.price = price
        order.qty = order.filled + qty

    async def _cancel(self, order, now_ns):
        order.pending = True
        order.cancel_at_ns = now_ns
        await self._send("cancel_order", {"cl_ord_id": [order.cl_ord_id]}, order)

    def _handle(self, msg):
        if "req_id" in msg and "method" in msg:
            self._handle_response(msg)
        elif msg.get("channel") == "executions" and msg.get("type") == "update":
            for ex in msg["data"]:
                self._handle_execution(ex)

    def _handle_response(self, msg):
        sent = self.inflight.pop(msg["req_id"], None)
        if sent is None:
            return
        sent_ns, method, order = sent
        if method in ("add_order", "amend_order", "cancel_order"):
            self.metrics.order_rtt.record(time.perf_counter_ns() - sent_ns)
        if order is not None:
            order.pending = False
        if msg.get("success"):
            if method == "add_order":
                order.order_id = msg["result"].get("order_id")
            return

        error = msg.get("error", "")
        print(f"\n[LIVE] {method} rejected: {error}")
        if "Rate limit" in error:
            self.paused_until_ns = time.time_ns() + int(self.cfg.RATE_LIMIT_BACKOFF_S * 1e9)
        if order is not None and method == "add_order":
            # e.g. post-only would have crossed: the order doesn't exist.
            self._forget(order)
        elif order is not None and method == "cancel_order":
            order.cancel_at_ns = None  # likely already filled; executions will confirm

    def _handle_execution(self, ex):
        order = self._find(ex.get("cl_ord_id"), ex.get("order_id"))
        last_qty = float(ex.get("last_qty") or 0)
        exec_id = ex.get("exec_id")
        if last_qty > 0 and exec_id not in self.seen_exec_ids:
            self.seen_exec_ids.add(exec_id)
            if ex.get("fee_usd_equiv") is not None:
                fee = float(ex["fee_usd_equiv"])
            else:
                fee = sum(float(f["qty"]) for f in ex.get("fees", ()))
            side = ex.get("side") or (order.side if order else None)
            if order is not None:
                order.filled += last_qty
            self.on_fill(side, float(ex["last_price"]), last_qty, fee)
        if order is not None and ex.get("order_status") in ("filled", "canceled", "expired"):
            self._forget(order)

    def _find(self, cl_ord_id, order_id):
        for o in self.orders.values():
            if o is not None and (o.cl_ord_id == cl_ord_id or (order_id and o.order_id == order_id)):
                return o
        return None

    def _forget(self, order):
        if self.orders.get(order.side) is order:
            self.orders[order.side] = None

    async def shutdown(self):
        if self.ws is not None:
            try:
                await self._send("cancel_all", {})
                await asyncio.sleep(0.5)  # give the ack a moment to arrive
            except websockets.exceptions.ConnectionClosed:
                pass
