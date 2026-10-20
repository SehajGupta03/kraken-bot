import zlib
from bisect import bisect_left, insort


class ChecksumMismatch(Exception):
    pass


class OrderBook:
    """L2 book maintained from Kraken WebSocket v2 snapshot + incremental updates.

    Each side is a dict (price -> qty) for O(1) level updates plus a sorted
    price list for ordered access. Bid prices are stored negated so both
    lists sort ascending with the best level at index 0. At depth 10 a
    bisect insert into a 10-element list is cheaper than any tree structure.
    """

    def __init__(self, depth, price_decimals, qty_decimals):
        self.depth = depth
        self.price_fmt = f"{{:.{price_decimals}f}}"
        self.qty_fmt = f"{{:.{qty_decimals}f}}"
        self.bids = {}
        self.asks = {}
        self._bid_keys = []  # negated prices, ascending == best first
        self._ask_keys = []
        self.last_update_ns = 0
        # False until a snapshot arrives and again after a checksum failure;
        # updates are meaningless until the next snapshot rebuilds the book.
        self.valid = False

    def apply_snapshot(self, data):
        self.bids.clear()
        self.asks.clear()
        self._bid_keys.clear()
        self._ask_keys.clear()
        self.valid = True
        self.apply_update(data)

    def apply_update(self, data):
        if not self.valid:
            return
        for level in data.get("bids", ()):
            self._set(self.bids, self._bid_keys, -level["price"], level["price"], level["qty"])
        for level in data.get("asks", ()):
            self._set(self.asks, self._ask_keys, level["price"], level["price"], level["qty"])
        self._truncate(self.bids, self._bid_keys, negate=True)
        self._truncate(self.asks, self._ask_keys, negate=False)

        checksum = data.get("checksum")
        if checksum is not None:
            computed = self.checksum()
            if checksum != computed:
                self.valid = False
                raise ChecksumMismatch(f"expected {checksum}, computed {computed}")

    @staticmethod
    def _set(levels, keys, key, price, qty):
        if qty == 0:
            if levels.pop(price, None) is not None:
                i = bisect_left(keys, key)
                if i < len(keys) and keys[i] == key:
                    keys.pop(i)
        else:
            if price not in levels:
                insort(keys, key)
            levels[price] = qty

    def _truncate(self, levels, keys, negate):
        # Kraken stops sending updates for levels pushed out of the
        # subscribed depth, so they must be dropped or they go stale.
        while len(keys) > self.depth:
            key = keys.pop()
            del levels[-key if negate else key]

    def checksum(self):
        """CRC32 over the top 10 asks (ascending) then top 10 bids (descending),
        each level as price+qty with the decimal point and leading zeros removed."""
        parts = []
        for key in self._ask_keys[:10]:
            parts.append(self._fmt(key, self.asks[key]))
        for key in self._bid_keys[:10]:
            parts.append(self._fmt(-key, self.bids[-key]))
        return zlib.crc32("".join(parts).encode())

    def _fmt(self, price, qty):
        p = self.price_fmt.format(price).replace(".", "").lstrip("0")
        q = self.qty_fmt.format(qty).replace(".", "").lstrip("0")
        return p + q

    # ------------------------------------------------------------ accessors

    def best_bid(self):
        if not self._bid_keys:
            return None, 0.0
        price = -self._bid_keys[0]
        return price, self.bids[price]

    def best_ask(self):
        if not self._ask_keys:
            return None, 0.0
        price = self._ask_keys[0]
        return price, self.asks[price]

    def is_ready(self):
        return self.valid and bool(self._bid_keys and self._ask_keys)

    def mid(self):
        bid, _ = self.best_bid()
        ask, _ = self.best_ask()
        return (bid + ask) / 2

    def microprice(self):
        """Size-weighted mid: leans towards the side with *less* resting size,
        since that side is more likely to be consumed next. A better short-term
        fair value than the plain mid on a 1-tick-wide book."""
        bid, bid_qty = self.best_bid()
        ask, ask_qty = self.best_ask()
        total = bid_qty + ask_qty
        if total <= 0:
            return (bid + ask) / 2
        return (bid * ask_qty + ask * bid_qty) / total

    def imbalance(self, levels):
        """(bid size - ask size) / total over the top `levels`. In [-1, 1]."""
        bid_qty = sum(self.bids[-k] for k in self._bid_keys[:levels])
        ask_qty = sum(self.asks[k] for k in self._ask_keys[:levels])
        total = bid_qty + ask_qty
        return 0.0 if total <= 0 else (bid_qty - ask_qty) / total

    def qty_at(self, side, price):
        return (self.bids if side == "buy" else self.asks).get(price, 0.0)
