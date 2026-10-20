# Kraken low-latency market maker

An event-driven market maker for Kraken spot, written in Python asyncio. It reads Kraken's L2 order book and trade stream over WebSocket v2, recomputes quotes on every update, and manages post-only limit orders over Kraken's authenticated WebSocket. No REST calls in the hot path.

## Strategy

An inventory-aware market maker in the style of Avellaneda & Stoikov (2008), with a microstructure fair value:

| Step | What it does |
|---|---|
| Fair value | Microprice (size-weighted touch), shifted by **order-book imbalance** over the top 5 levels and **trade-flow imbalance** (EWMA of signed aggressor volume) |
| Reservation price | Fair value skewed against inventory, so the bot leans back towards flat instead of accumulating |
| Half spread | `fee + required edge + k·σ(horizon)`: never quotes inside the fee hurdle, and widens when the market speeds up |
| Toxicity filter | Pulls the side the signals say is about to be run over (adverse-selection protection) |
| Execution | One post-only order per side, **amended** in place rather than cancel/replace, throttled to respect Kraken's rate counter |

## Latency design

```
socket read ─► orjson parse ─► L2 book update + CRC32 checksum ─► signals (O(1) incremental)
           ─► quotes ─► amend/add over persistent auth WebSocket
```

- A single event loop with no queues, threads or polling. Each message runs from parse to order send inside the callback that read it.
- The book uses dict + bisect arrays at depth 10, so each update is O(1) or O(log n). The book is CRC32-checked against Kraken on every update and resubscribes on mismatch.
- All signals (flow EWMA, EWMA volatility) update incrementally, with no rolling windows to rescan.
- WebSocket compression is disabled and JSON is parsed with orjson. uvloop is used where it's available (Linux/macOS).
- Order entry runs over WebSocket v2 (`add_order` / `amend_order` / `cancel_order`). Amends keep one message per requote.
- Instrumentation reports p50/p90/p99 for three latencies:
  - **feed**: exchange timestamp to socket read.
  - **tick→quote**: socket read to order sent. This is the in-process cost, about 40–60µs median.
  - **order ack RTT**: order sent to Kraken's acknowledgement.

## Realistic paper trading

The paper exchange replays your quotes against the real public tape and models the two effects that make naive backtests lie:

- **Latency:** places, amends and cancels take `SIM_LATENCY_MS` to land. A cancel that's in flight can still get picked off.
- **Queue position:** a new or repriced order joins the back of the queue at its price. It fills at the touch only after the size ahead of it has traded. Trades through the price fill it immediately.

## Risk controls

- Post-only orders only, so the bot never pays taker fees.
- Position cap and spot-only inventory: the bot sells only coins it bought.
- Kill switch on realized + unrealized loss.
- Quotes are pulled when the book is stale or a checksum fails.
- Kraken's `cancel_all_orders_after` dead man's switch, refreshed every 20s, plus `cancel_all` on start and on shutdown.

## Usage

```bash
pip install -r requirements.txt
python bot.py                                  # paper trading on live data
python bot.py --record session.jsonl           # also record raw feed for replay
python replay.py session.jsonl --set MAKER_FEE=0.001 --set SIM_LATENCY_MS=5
python -m unittest discover -s tests
LIVE_TRADING_CONFIRMED=yes KRAKEN_API_KEY=... KRAKEN_API_SECRET=... python bot.py --live
```

`replay.py` drives the same book, strategy and paper-exchange code from a recording, so you can tune parameters on identical data at about 20k messages/s.

## Honest economics

At Kraken's entry-tier spot maker fee (25bp), a round trip costs 50bp, while the BTC/USD spread is roughly 0.01bp. The strategy enforces that fee floor, so with default settings it quotes far from the touch and fills rarely. It becomes viable at lower fee tiers, on wider-spread pairs (set `PAIR` / `WS_SYMBOL`), or ported to Kraken Futures, where maker fees are about 2bp. Use `replay.py` to find out where the edge is before trading real money.
