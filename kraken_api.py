import base64
import hashlib
import hmac
import os
import time
import urllib.parse

import requests

BASE_URL = "https://api.kraken.com"


class KrakenAPIError(Exception):
    pass


class KrakenAPI:
    """Thin wrapper around Kraken's REST API.

    Public endpoints (OHLC, ticker) need no credentials, which is all
    paper trading uses. Private endpoints are included for when this
    is later switched to live trading, reading credentials from
    environment variables (KRAKEN_API_KEY / KRAKEN_API_SECRET) rather
    than hardcoding them.
    """

    def __init__(self, api_key=None, api_secret=None):
        self.api_key = (api_key or os.environ.get("KRAKEN_API_KEY") or "").strip()
        self.api_secret = (api_secret or os.environ.get("KRAKEN_API_SECRET") or "").strip()

    def _public_request(self, path, params=None):
        response = requests.get(BASE_URL + path, params=params, timeout=10)
        payload = response.json()
        if payload.get("error"):
            raise KrakenAPIError(", ".join(payload["error"]))
        return payload["result"]

    def _sign(self, urlpath, data):
        encoded = (str(data["nonce"]) + urllib.parse.urlencode(data)).encode()
        message = urlpath.encode() + hashlib.sha256(encoded).digest()
        mac = hmac.new(base64.b64decode(self.api_secret), message, hashlib.sha512)
        return base64.b64encode(mac.digest()).decode()

    def _private_request(self, path, data=None):
        if not self.api_key or not self.api_secret:
            raise KrakenAPIError("KRAKEN_API_KEY / KRAKEN_API_SECRET are not set")
        data = dict(data or {})
        data["nonce"] = str(int(time.time() * 1000))
        headers = {
            "API-Key": self.api_key,
            "API-Sign": self._sign(path, data),
        }
        response = requests.post(BASE_URL + path, headers=headers, data=data, timeout=10)
        payload = response.json()
        if payload.get("error"):
            raise KrakenAPIError(", ".join(payload["error"]))
        return payload["result"]

    def get_ohlc(self, pair, interval_minutes):
        """Returns a list of [time, open, high, low, close, vwap, volume, count]."""
        result = self._public_request(
            "/0/public/OHLC", {"pair": pair, "interval": interval_minutes}
        )
        for key, candles in result.items():
            if key != "last":
                return candles
        raise KrakenAPIError(f"No OHLC data returned for pair {pair}")

    def get_ticker_price(self, pair):
        result = self._public_request("/0/public/Ticker", {"pair": pair})
        for key, info in result.items():
            return float(info["c"][0])
        raise KrakenAPIError(f"No ticker data returned for pair {pair}")

    def add_order(self, pair, side, volume, order_type="market", validate=False):
        """Places a REAL order on Kraken. Not used in paper-trading mode.
        validate=True asks Kraken to check the order without executing it."""
        data = {"pair": pair, "type": side, "ordertype": order_type, "volume": str(volume)}
        if validate:
            data["validate"] = "true"
        return self._private_request("/0/private/AddOrder", data)

    def get_balance(self):
        """Wraps the private Balance endpoint. Returns {asset_code: amount}."""
        result = self._private_request("/0/private/Balance")
        return {asset: float(amount) for asset, amount in result.items()}

    def get_asset_pair_info(self, pair):
        """Wraps the public AssetPairs endpoint. Returns the base/quote asset
        codes (as used by get_balance) and the exchange's minimum order size
        and cost for this pair."""
        result = self._public_request("/0/public/AssetPairs", {"pair": pair})
        for _, info in result.items():
            return {
                "base": info["base"],
                "quote": info["quote"],
                "ordermin": float(info.get("ordermin", 0)),
                "costmin": float(info.get("costmin", 0)),
                "lot_decimals": int(info.get("lot_decimals", 8)),
            }
        raise KrakenAPIError(f"No asset pair info returned for {pair}")

    def query_order(self, txid):
        """Wraps the private QueryOrders endpoint for a single order id."""
        result = self._private_request("/0/private/QueryOrders", {"txid": txid})
        return result.get(txid)