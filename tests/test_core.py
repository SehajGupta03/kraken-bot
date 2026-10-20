import asyncio
import os
import sys
import tempfile
import types
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from execution import PaperExchange  # noqa: E402
from orderbook import ChecksumMismatch, OrderBook  # noqa: E402
from position import Position  # noqa: E402
from strategy import Quote  # noqa: E402

MS = 1_000_000


def lvl(price, qty):
    return {"price": price, "qty": qty}


def make_book():
    book = OrderBook(depth=3, price_decimals=1, qty_decimals=8)
    book.apply_snapshot({
        "bids": [lvl(100.0, 1.0), lvl(99.9, 2.0), lvl(99.8, 3.0)],
        "asks": [lvl(100.1, 1.5), lvl(100.2, 2.5), lvl(100.3, 3.5)],
    })
    return book


class OrderBookTest(unittest.TestCase):
    def test_update_delete_and_truncate(self):
        book = make_book()
        book.apply_update({"bids": [lvl(100.05, 0.5), lvl(99.9, 0)]})
        self.assertEqual(book.best_bid(), (100.05, 0.5))
        self.assertNotIn(99.9, book.bids)
        book.apply_update({"bids": [lvl(100.07, 0.1), lvl(100.06, 0.1)]})
        # depth 3: worst bid levels fall out of the book
        self.assertEqual(sorted(book.bids), [100.05, 100.06, 100.07])

    def test_checksum_roundtrip_and_mismatch(self):
        book = make_book()
        good = book.checksum()
        book.apply_update({"asks": [lvl(100.1, 1.5)], "checksum": good})  # no-op update, valid
        with self.assertRaises(ChecksumMismatch):
            book.apply_update({"asks": [lvl(100.1, 1.0)], "checksum": good})
        self.assertFalse(book.is_ready())

    def test_microprice_leans_to_thin_side(self):
        # Bid side is thinner (1.0 vs 1.5), so it's likelier to be consumed next: microprice < mid.
        book = make_book()
        self.assertLess(book.microprice(), book.mid())


class PaperExchangeTest(unittest.TestCase):
    def setUp(self):
        self.cfg = types.SimpleNamespace(
            MIN_REQUOTE_INTERVAL_MS=0, REQUOTE_THRESHOLD_BPS=0.0, SIM_LATENCY_MS=10, MAKER_FEE=0.001
        )
        self.book = make_book()
        self.fills = []
        self.ex = PaperExchange(self.cfg, self.book, lambda *f: self.fills.append(f))

    def run_async(self, coro):
        return asyncio.run(coro)

    def sell_trade(self, price, qty):
        return [{"side": "sell", "price": price, "qty": qty}]

    def test_order_not_live_until_latency_elapses(self):
        self.run_async(self.ex.sync("buy", Quote(100.0, 0.1), 0))
        self.run_async(self.ex.on_trades(self.sell_trade(99.9, 5.0), 5 * MS))
        self.assertEqual(self.fills, [])
        self.run_async(self.ex.on_trades(self.sell_trade(99.9, 5.0), 11 * MS))
        self.assertEqual(len(self.fills), 1)

    def test_queue_position_at_touch(self):
        # Joining a 1.0 BTC bid: the first 1.0 traded at our price fills others.
        self.run_async(self.ex.sync("buy", Quote(100.0, 0.1), 0))
        self.run_async(self.ex.on_trades(self.sell_trade(100.0, 0.6), 20 * MS))
        self.assertEqual(self.fills, [])
        self.run_async(self.ex.on_trades(self.sell_trade(100.0, 0.45), 21 * MS))
        side, price, qty, fee = self.fills[0]
        self.assertEqual((side, price), ("buy", 100.0))
        self.assertAlmostEqual(qty, 0.05)

    def test_cancel_is_not_instant(self):
        self.run_async(self.ex.sync("buy", Quote(100.0, 0.1), 0))
        self.run_async(self.ex.on_book(20 * MS))  # activate
        self.run_async(self.ex.sync("buy", None, 20 * MS))
        # A trade through our price 5ms after cancelling still picks us off.
        self.run_async(self.ex.on_trades(self.sell_trade(99.5, 1.0), 25 * MS))
        self.assertEqual(len(self.fills), 1)
        self.run_async(self.ex.on_trades(self.sell_trade(99.5, 1.0), 40 * MS))
        self.assertEqual(len(self.fills), 1)


class PositionTest(unittest.TestCase):
    def test_round_trip_pnl(self):
        with tempfile.TemporaryDirectory() as d:
            p = Position(100.0, os.path.join(d, "log.csv"))
            p.apply_fill("buy", 100.0, 0.5, 0.05)
            p.apply_fill("sell", 102.0, 0.5, 0.05)
            self.assertEqual(p.qty, 0.0)
            self.assertAlmostEqual(p.pnl(0.0), 1.0 - 0.10)


if __name__ == "__main__":
    unittest.main()
