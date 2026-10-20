import math
from dataclasses import dataclass


@dataclass
class Quote:
    price: float
    qty: float


class Signals:
    """Short-horizon state updated incrementally on every market event:
    O(1) per update, no history windows to rescan."""

    def __init__(self, cfg):
        self.flow_halflife_ns = cfg.FLOW_HALFLIFE_S * 1e9
        self.vol_sample_ns = cfg.VOL_SAMPLE_S * 1e9
        self.vol_halflife_s = cfg.VOL_HALFLIFE_S

        self.buy_flow = 0.0
        self.sell_flow = 0.0
        self.last_flow_ns = 0

        self.var_per_s = None  # EWMA variance of log mid returns, per second
        self.vol_samples = 0
        self.last_vol_mid = None
        self.last_vol_ns = 0

    def on_trades(self, trades, now_ns):
        self._decay_flow(now_ns)
        for t in trades:
            if t["side"] == "buy":
                self.buy_flow += t["qty"]
            else:
                self.sell_flow += t["qty"]

    def _decay_flow(self, now_ns):
        if self.last_flow_ns:
            k = 0.5 ** ((now_ns - self.last_flow_ns) / self.flow_halflife_ns)
            self.buy_flow *= k
            self.sell_flow *= k
        self.last_flow_ns = now_ns

    def flow_imbalance(self, now_ns):
        """Signed aggressor volume, decayed to now. In [-1, 1]."""
        self._decay_flow(now_ns)
        total = self.buy_flow + self.sell_flow
        return 0.0 if total <= 0 else (self.buy_flow - self.sell_flow) / total

    def on_mid(self, mid, now_ns):
        if self.last_vol_mid is None:
            self.last_vol_mid, self.last_vol_ns = mid, now_ns
            return
        elapsed_ns = now_ns - self.last_vol_ns
        if elapsed_ns < self.vol_sample_ns:
            return
        dt = elapsed_ns / 1e9
        r = math.log(mid / self.last_vol_mid)
        x = r * r / dt
        if self.var_per_s is None:
            self.var_per_s = x
        else:
            alpha = 1 - 0.5 ** (dt / self.vol_halflife_s)
            self.var_per_s += alpha * (x - self.var_per_s)
        self.vol_samples += 1
        self.last_vol_mid, self.last_vol_ns = mid, now_ns

    def warmed_up(self):
        return self.vol_samples >= 10

    def sigma_bps(self, horizon_s):
        return 0.0 if self.var_per_s is None else math.sqrt(self.var_per_s * horizon_s) * 1e4


class MarketMaker:
    """Inventory-aware market maker in the spirit of Avellaneda & Stoikov (2008),
    with a microstructure fair value instead of the plain mid.

    1. fair value   = microprice shifted by book-imbalance and trade-flow alphas
    2. reservation  = fair value skewed against current inventory, so the
                      bot leans towards getting flat instead of accumulating
    3. half spread  = fee floor + required edge + a volatility term, so
                      quotes widen when the market is moving fast
    4. quotes       = reservation +/- half spread, snapped to tick, clamped
                      to never cross the book (post-only)
    5. toxicity     = pull the side the signals say is about to be run over

    Spot only: the ask is limited to inventory the bot itself bought,
    so it never sells coins it didn't acquire.
    """

    def __init__(self, cfg, tick, price_decimals, ordermin):
        self.cfg = cfg
        self.tick = tick
        self.price_decimals = price_decimals
        self.ordermin = ordermin
        self.fee_floor_bps = max(cfg.MIN_HALF_SPREAD_BPS, cfg.MAKER_FEE * 1e4 + cfg.MIN_EDGE_BPS)
        self.last = {}  # most recent decision inputs, for the status line

    def quotes(self, book, signals, position_qty, now_ns):
        cfg = self.cfg
        best_bid, _ = book.best_bid()
        best_ask, _ = book.best_ask()

        imb = book.imbalance(cfg.IMBALANCE_LEVELS)
        flow = signals.flow_imbalance(now_ns)
        alpha_bps = cfg.ALPHA_IMBALANCE_BPS * imb + cfg.ALPHA_FLOW_BPS * flow
        fair = book.microprice() * (1 + alpha_bps / 1e4)

        inv = (position_qty - cfg.TARGET_POSITION) / cfg.MAX_POSITION
        reservation = fair * (1 - inv * cfg.INVENTORY_SKEW_BPS / 1e4)

        half_bps = self.fee_floor_bps + cfg.VOL_SPREAD_MULT * signals.sigma_bps(cfg.QUOTE_HORIZON_S)

        bid_px = self._floor_tick(reservation * (1 - half_bps / 1e4))
        ask_px = self._ceil_tick(reservation * (1 + half_bps / 1e4))
        bid_px = min(bid_px, round(best_ask - self.tick, self.price_decimals))
        ask_px = max(ask_px, round(best_bid + self.tick, self.price_decimals))

        signal = 0.5 * imb + 0.5 * flow
        self.last = {"fair": fair, "res": reservation, "half_bps": half_bps, "imb": imb, "flow": flow}

        bid = ask = None
        bid_qty = min(cfg.QUOTE_SIZE, cfg.MAX_POSITION - position_qty)
        if bid_qty >= self.ordermin and signal > -cfg.TOXIC_SIGNAL:
            bid = Quote(bid_px, bid_qty)
        ask_qty = min(cfg.QUOTE_SIZE, position_qty)
        if ask_qty >= self.ordermin and signal < cfg.TOXIC_SIGNAL:
            ask = Quote(ask_px, ask_qty)
        return bid, ask

    def _floor_tick(self, px):
        return round(math.floor(px / self.tick + 1e-9) * self.tick, self.price_decimals)

    def _ceil_tick(self, px):
        return round(math.ceil(px / self.tick - 1e-9) * self.tick, self.price_decimals)
