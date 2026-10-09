from __future__ import annotations

import hashlib
import hmac
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from decimal import Decimal, ROUND_DOWN


class ApiError(RuntimeError):
    """Definitive rejection or a failed read. Secrets and headers are never included."""


class AmbiguousWrite(RuntimeError):
    """A mutation might have executed: reconcile; NEVER automatically resend it."""


def decimal_string(value: float | str, precision: int) -> str:
    q = Decimal(1).scaleb(-precision)
    return format(Decimal(str(value)).quantize(q, rounding=ROUND_DOWN), "f")


def canonical(params: dict) -> str:
    # Slash must remain literal, matching the organizer's example signature.
    return urllib.parse.urlencode(sorted((str(k), str(v)) for k, v in params.items()), safe="/")


def signature(params: dict, secret: str) -> str:
    return hmac.new(secret.encode(), canonical(params).encode(), hashlib.sha256).hexdigest()


class Roostoo:
    def __init__(self, base_url="https://mock-api.roostoo.com", spacing=2., api_key=None, secret=None):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key if api_key is not None else os.getenv("ROOSTOO_API_KEY", "")
        self.secret = secret if secret is not None else os.getenv("ROOSTOO_SECRET_KEY", "")
        self.spacing = spacing
        self.next_request = 0.
        self.offset_ms = 0
        self.last_sync = 0.

    @property
    def authenticated(self) -> bool:
        return bool(self.api_key and self.secret)

    def now_ms(self) -> int:
        return int(time.time() * 1000) + self.offset_ms

    def sync_time(self) -> int:
        before = int(time.time() * 1000)
        d = self._request("GET", "/v3/serverTime", timestamp=False)
        after = int(time.time() * 1000)
        self.offset_ms = int(d["ServerTime"]) - (before + after) // 2
        self.last_sync = time.monotonic()
        return int(d["ServerTime"])

    def _request(self, method: str, endpoint: str, params: dict | None = None, signed=False, timestamp=True, mutation=False) -> dict:
        if signed and not self.authenticated:
            raise ApiError("Missing Roostoo credentials in private environment")
        tries = 1 if mutation else 2
        for attempt in range(tries):
            delay = self.next_request - time.monotonic()
            if delay > 0:
                time.sleep(delay)
            self.next_request = time.monotonic() + self.spacing
            payload = dict(params or {})
            if timestamp:
                payload["timestamp"] = str(self.now_ms())
            body = canonical(payload)
            headers = {"User-Agent": "roostoo-sentinel/0.1", "Content-Type": "application/x-www-form-urlencoded"}
            if signed:
                headers.update({"RST-API-KEY": self.api_key, "MSG-SIGNATURE": signature(payload, self.secret)})
            url = self.base_url + endpoint + (("?" + body) if method == "GET" and body else "")
            req = urllib.request.Request(url, data=body.encode() if method == "POST" else None, headers=headers, method=method)
            try:
                with urllib.request.urlopen(req, timeout=15) as r:
                    obj = json.loads(r.read())
                if not isinstance(obj, dict):
                    raise ValueError("Unexpected API response shape")
            except urllib.error.HTTPError as e:
                if mutation and (e.code >= 500 or e.code in {408, 429}):
                    raise AmbiguousWrite(f"{endpoint}: HTTP {e.code}; write status unknown") from None
                if e.code == 429:
                    raise ApiError(f"{endpoint}: rate limited; wait for the next loop") from None
                if attempt + 1 == tries:
                    raise ApiError(f"{endpoint}: HTTP {e.code}") from None
                continue
            except (urllib.error.URLError, TimeoutError, OSError, ValueError) as e:
                if mutation:
                    raise AmbiguousWrite(f"{endpoint}: {type(e).__name__}; write status unknown") from None
                if attempt + 1 == tries:
                    raise ApiError(f"{endpoint}: transport or malformed-response failure") from None
                continue
            if obj.get("Success") is False:
                # Empty order results are explicitly normalized only when the schema agrees.
                if endpoint == "/v3/query_order" and (obj.get("ErrMsg") == "no order matched" or ("OrderMatched" in obj and obj["OrderMatched"] == [])):
                    return {**obj, "OrderMatched": []}
                message = str(obj.get("ErrMsg", "rejected"))
                for sensitive in (self.api_key, self.secret):
                    if sensitive:
                        message = message.replace(sensitive, "[redacted]")
                raise ApiError(f"{endpoint}: {message[:160]}")
            if mutation and obj.get("Success") is not True:
                raise AmbiguousWrite(f"{endpoint}: missing explicit acknowledgement; write status unknown")
            return obj
        raise ApiError("Read retry budget exhausted")

    def exchange_info(self):
        return self._request("GET", "/v3/exchangeInfo", timestamp=False)

    def ticker(self):
        return self._request("GET", "/v3/ticker")

    def balance(self):
        return self._request("GET", "/v3/balance", signed=True)["Wallet"]

    def short_positions(self):
        return self._request("GET", "/v6/short_positions", signed=True).get("Positions", [])

    def query_orders(self, order_id=None, limit=100, offset=0, pending_only=False):
        params = {"order_id": str(order_id)} if order_id is not None else {"limit": str(limit), "offset": str(offset), "pending_only": "TRUE" if pending_only else "FALSE"}
        return self._request("POST", "/v3/query_order", params, signed=True).get("OrderMatched", [])

    def place(self, pair: str, side: str, quantity: str):
        if side not in {"BUY", "SELL"}:
            raise ValueError("Spot endpoint accepts BUY or SELL only")
        return self._request("POST", "/v3/place_order", {"pair": pair, "side": side, "type": "MARKET", "quantity": quantity}, signed=True, mutation=True)

    def place_limit(self, pair: str, side: str, quantity: str, price: str):
        if side not in {"BUY", "SELL"} or Decimal(price) <= 0 or Decimal(quantity) <= 0:
            raise ValueError("Invalid spot limit order")
        return self._request("POST", "/v3/place_order", {"pair": pair, "side": side, "type": "LIMIT", "quantity": quantity, "price": price}, signed=True, mutation=True)

    def cancel_order(self, order_id):
        if order_id is None:
            raise ValueError("Cancellation must name one owned order")
        return self._request("POST", "/v3/cancel_order", {"order_id": str(order_id)}, signed=True, mutation=True)

    def short_open(self, pair: str, collateral: str):
        # 1x collateral sizing is defined by Roostoo. Never send quantity/side to this endpoint.
        return self._request("POST", "/v6/short_open", {"pair": pair, "collateral": collateral}, signed=True, mutation=True)

    def short_close(self, pair: str):
        return self._request("POST", "/v6/short_close", {"pair": pair}, signed=True, mutation=True)
