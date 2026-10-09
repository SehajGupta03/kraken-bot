import asyncio
import json

import websockets

WS_URL = "wss://ws.kraken.com/v2"

async def stream_ohlc(ws_symbol, interval_minutes):
    """Streams live OHLC candles for a pair over Kraken's WebSocket v2 API.

    Yields a dict per message:
      price:        latest known trade price
      new_candle:   True if this message just started a new candle, meaning
                    the previous one has finished
      closed_price: the finished candle's close, only set when new_candle
                    is True (this is what the strategy should react to)

    Reconnects automatically with backoff if the connection drops.
    """
    backoff = 1
    current_begin = None
    last_price = None

    while True:
        try:
            async with websockets.connect(WS_URL, ping_interval=20, ping_timeout=20) as ws:
                await ws.send(
                    json.dumps(
                        {
                            "method": "subscribe",
                            "params": {
                                "channel": "ohlc",
                                "symbol": [ws_symbol],
                                "interval": interval_minutes,
                            },
                        }
                    )
                )
                backoff = 1

                async for raw in ws:
                    msg = json.loads(raw)
                    if msg.get("channel") != "ohlc":
                        continue

                    for candle in msg.get("data", []):
                        begin = candle["interval_begin"]
                        price = float(candle["close"])

                        new_candle = current_begin is not None and begin != current_begin
                        closed_price = last_price if new_candle else None

                        current_begin = begin
                        last_price = price

                        yield {
                            "price": price,
                            "new_candle": new_candle,
                            "closed_price": closed_price,
                        }

        except (websockets.exceptions.ConnectionClosed, OSError) as e:
            print(f"[WS] disconnected ({e}); reconnecting in {backoff}s")
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 30)
