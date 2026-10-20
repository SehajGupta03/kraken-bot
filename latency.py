from collections import deque


class LatencyStats:
    """Fixed-size ring buffer of samples (nanoseconds) with percentile reporting.
    Recording is O(1) with no allocation on the hot path; sorting only
    happens when a report is requested."""

    def __init__(self, name, maxlen=10_000):
        self.name = name
        self.samples = deque(maxlen=maxlen)

    def record(self, ns):
        self.samples.append(ns)

    def summary(self):
        if not self.samples:
            return f"{self.name}: n/a"
        s = sorted(self.samples)
        n = len(s)

        def pct(p):
            return s[min(n - 1, int(p * n))] / 1000  # -> microseconds

        return (
            f"{self.name}: p50={_fmt_us(pct(0.50))} p90={_fmt_us(pct(0.90))} "
            f"p99={_fmt_us(pct(0.99))} max={_fmt_us(s[-1] / 1000)} (n={n})"
        )


def _fmt_us(us):
    return f"{us / 1000:.2f}ms" if us >= 1000 else f"{us:.0f}us"


class Metrics:
    """The three latencies that matter for this bot:

    feed:         exchange matching-engine timestamp -> our socket read.
                  Includes network + Kraken's own fan-out, and is skewed by
                  any difference between our clock and Kraken's (keep NTP on).
    tick_to_quote: our socket read -> quote decision made and order message
                  handed to the socket. Pure in-process cost; the part we control.
    order_rtt:    order message sent -> Kraken's ack (live mode only).
    """

    def __init__(self):
        self.feed = LatencyStats("feed (exch->recv)")
        self.tick_to_quote = LatencyStats("tick->quote")
        self.order_rtt = LatencyStats("order ack RTT")

    def report(self):
        return "\n".join(
            "  " + s.summary() for s in (self.feed, self.tick_to_quote, self.order_rtt)
        )
