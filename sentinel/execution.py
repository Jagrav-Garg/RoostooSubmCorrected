from __future__ import annotations

from dataclasses import asdict

from .api import AmbiguousWrite, ApiError, Roostoo, decimal_string
from .config import Config
from .data import HOUR
from .risk import Position
from .signals import Signal
from .state import Store


def wallet_qty(wallet: dict, pair: str) -> float:
    x = wallet.get(pair.split("/")[0], {})
    return float(x.get("Free", 0)) + float(x.get("Lock", 0))


def validated_quote(ticker: dict, pair: str) -> tuple[float, float]:
    x = ticker.get("Data", {}).get(pair, {})
    bid, ask = float(x.get("MaxBid", 0)), float(x.get("MinAsk", 0))
    if not 0 < bid <= ask:
        raise ApiError(f"Invalid bid/ask for {pair}")
    return bid, ask


class Executor:
    def __init__(self, api: Roostoo, store: Store, cfg: Config, mode: str, metadata: dict, version: str):
        self.api, self.store, self.cfg, self.mode, self.meta, self.version = api, store, cfg, mode, metadata, version
        if mode not in {"paper", "live"}:
            raise ValueError("Mode must be paper or live")
        if store.get("paper_cash") is None and mode == "paper":
            store.set("paper_cash", cfg.initial_equity)

    def snapshot(self, ticker: dict) -> tuple[float, float, dict, list[dict]]:
        cfg = self.cfg
        if self.mode == "paper":
            cash = float(self.store.get("paper_cash"))
            positions = self.store.positions()
            equity = cash
            for p in positions:
                bid, ask = validated_quote(ticker, p.pair)
                px = (bid if p.direction == 1 else ask) * (1 - p.direction * cfg.friction_bps_per_side / 10000)
                equity += p.liquidation_value(px, cfg.fee_per_side)
            return equity, cash, {}, []
        wallet = self.api.balance()
        # Also check for unmanaged shorts when the candidate strategy is long-only.
        shorts = self.api.short_positions()
        usd = wallet.get("USD", {})
        free, locked = float(usd.get("Free", 0)), float(usd.get("Lock", 0))
        collateral = sum(float(s.get("Collateral", 0)) for s in shorts)
        # Do not double-count collateral or silently assume an undocumented accounting convention.
        if shorts and locked + .02 < collateral:
            raise ApiError("Short collateral is not represented in Wallet USD Lock as expected; accounting requires verification")
        equity = free + locked
        for coin, amounts in wallet.items():
            if coin == "USD":
                continue
            qty = float(amounts.get("Free", 0)) + float(amounts.get("Lock", 0))
            if qty <= 0:
                continue
            bid, ask = validated_quote(ticker, coin + "/USD")
            equity += qty * bid * (1 - cfg.fee_per_side)
        for s in shorts:
            bid, ask = validated_quote(ticker, s["Pair"])
            qty = float(s["ShortQty"])
            equity += max(-float(s["Collateral"]), qty * (float(s["EntryPrice"]) - ask)) - qty * ask * cfg.fee_per_side
        return equity, free, wallet, shorts

    def ownership_check(self, ticker: dict, wallet: dict, shorts: list[dict]) -> None:
        if self.mode == "paper":
            return
        positions = self.store.positions()
        managed = {(p.pair, p.direction): p for p in positions}
        for coin, amounts in wallet.items():
            if coin == "USD":
                continue
            pair = coin + "/USD"
            qty = float(amounts.get("Free", 0)) + float(amounts.get("Lock", 0))
            if qty <= 0:
                continue
            bid, ask = validated_quote(ticker, pair)
            if qty * bid < self.cfg.minimum_order_notional:
                continue
            p = managed.get((pair, 1))
            if p is None:
                raise ApiError(f"Unmanaged holding {pair}; preserve the old bot/state until an audited handover")
            precision = int(self.meta["TradePairs"][pair]["AmountPrecision"])
            dust_allowance = self.cfg.minimum_order_notional / bid if qty >= p.quantity else 0.
            if abs(qty - p.quantity) > max(2 * 10 ** -precision, p.quantity * .0001, dust_allowance):
                raise ApiError(f"Holding/journal mismatch for {pair}; no new mutations")
        for s in shorts:
            p = managed.get((s["Pair"], -1))
            if p is None or abs(float(s["ShortQty"]) - p.quantity) > max(1e-8, p.quantity * .0001):
                raise ApiError(f"Unmanaged short or journal mismatch for {s['Pair']}")
        for p in positions:
            if p.direction == 1 and wallet_qty(wallet, p.pair) * p.entry < self.cfg.minimum_order_notional:
                raise ApiError(f"Journal contains a missing long holding: {p.pair}")
            if p.direction == -1 and not any(s["Pair"] == p.pair for s in shorts):
                raise ApiError(f"Journal contains a missing short: {p.pair}")

    def _stamp(self) -> dict:
        return {"mode": self.mode, "config_hash": self.cfg.fingerprint, "commit": self.version}

    def open(self, s: Signal, notional: float, ticker: dict, wallet: dict, now: int) -> bool:
        if self.store.unfinished():
            return False
        bid, ask = validated_quote(ticker, s.pair)
        entry = ask if s.direction == 1 else bid
        meta = self.meta["TradePairs"][s.pair]
        qty_string = decimal_string(notional / entry, int(meta["AmountPrecision"]))
        if float(qty_string) * entry <= max(float(meta["MiniOrder"]), self.cfg.minimum_order_notional):
            return False
        collateral = decimal_string(notional, 2)
        payload = {**self._stamp(), "action": "open", "signal": asdict(s), "quantity": qty_string, "collateral": collateral, "before_qty": wallet_qty(wallet, s.pair), "quote_entry": entry, "request_timestamp": now}
        key = self.store.intent(f"{self.mode}|open|{s.pair}|{s.direction}|{s.timestamp}", payload, now)
        if key is None:
            return False
        self.store.event("order_intent", {"intent": key, "pair": s.pair, "action": "open", "direction": s.direction, "notional": notional, **self._stamp()}, now)
        if self.mode == "paper":
            entry *= 1 + s.direction * self.cfg.friction_bps_per_side / 10000
            qty = float(qty_string)
            amount = qty * entry
            fee = amount * self.cfg.fee_per_side
            p = Position(s.pair, s.strategy, s.direction, qty, entry, now, entry * (1 - s.direction * self.cfg.stop_atr * s.atr_fraction), entry * (1 + s.direction * self.cfg.target_atr * s.atr_fraction), amount if s.direction == -1 else 0., fee, key)
            cash = float(self.store.get("paper_cash")) - amount - fee
            # Paper cash and position update are also atomic across process failures.
            with self.store.db:
                self.store.db.execute("UPDATE kv SET value=? WHERE key='paper_cash'", (str(cash),))
                self.store.complete(key, self.store.positions() + [p], {"pair": s.pair, "action": "open", "direction": s.direction, "quantity": qty, "price": entry, "fee": fee, **self._stamp()}, key, now)
            return True
        try:
            response = self.api.place(s.pair, "BUY", qty_string) if s.direction == 1 else self.api.short_open(s.pair, collateral)
        except AmbiguousWrite as e:
            self.store.update_intent(key, "unknown")
            self.store.event("write_status_unknown", {"intent": key, "error": str(e), "action": "entries_and_writes_paused_pending_reconciliation"}, now)
            return False
        except ApiError as e:
            self.store.update_intent(key, "rejected", {"error": str(e)})
            self.store.event("order_rejected", {"intent": key, "error": str(e)}, now)
            return False
        self.store.update_intent(key, "acknowledged", response)
        # A failed follow-up read cannot turn an accepted order into a rejection.
        try:
            return self.reconcile()
        except ApiError as e:
            self.store.event("reconciliation_read_failed", {"intent": key, "error": str(e)}, now)
            return False

    def close(self, p: Position, reason: str, ticker: dict, wallet: dict, now: int) -> bool:
        if self.store.unfinished():
            return False
        bid, ask = validated_quote(ticker, p.pair)
        px = bid if p.direction == 1 else ask
        precision = int(self.meta["TradePairs"][p.pair]["AmountPrecision"])
        free_qty = float(wallet.get(p.pair.split("/")[0], {}).get("Free", 0)) if self.mode == "live" else p.quantity
        qty_string = decimal_string(min(p.quantity, free_qty) if p.direction == 1 else p.quantity, precision)
        if float(qty_string) <= 0:
            raise ApiError(f"No free sellable holding for {p.pair}")
        payload = {**self._stamp(), "action": "close", "position": p.to_dict(), "quantity": qty_string, "before_qty": wallet_qty(wallet, p.pair), "reason": reason, "request_timestamp": now}
        key = self.store.intent(f"{self.mode}|close|{p.server_id}|{now}", payload, now)
        if key is None:
            return False
        self.store.event("order_intent", {"intent": key, "pair": p.pair, "action": "close", "reason": reason, **self._stamp()}, now)
        if self.mode == "paper":
            px *= 1 - p.direction * self.cfg.friction_bps_per_side / 10000
            cash = float(self.store.get("paper_cash")) + p.liquidation_value(px, self.cfg.fee_per_side)
            with self.store.db:
                self.store.db.execute("UPDATE kv SET value=? WHERE key='paper_cash'", (str(cash),))
                self.store.complete(key, [q for q in self.store.positions() if q.pair != p.pair], {"pair": p.pair, "action": "close", "direction": p.direction, "quantity": p.quantity, "price": px, "reason": reason, **self._stamp()}, key, now)
            self.store.set("cooldown_" + p.pair, now + self.cfg.cooldown_hours * HOUR)
            return True
        try:
            response = self.api.place(p.pair, "SELL", qty_string) if p.direction == 1 else self.api.short_close(p.pair)
        except AmbiguousWrite as e:
            self.store.update_intent(key, "unknown")
            self.store.event("write_status_unknown", {"intent": key, "error": str(e)}, now)
            return False
        except ApiError as e:
            self.store.update_intent(key, "rejected", {"error": str(e)})
            self.store.event("order_rejected", {"intent": key, "error": str(e)}, now)
            return False
        self.store.update_intent(key, "acknowledged", response)
        try:
            return self.reconcile()
        except ApiError as e:
            self.store.event("reconciliation_read_failed", {"intent": key, "error": str(e)}, now)
            return False

    def reconcile(self) -> bool:
        pending = self.store.unfinished()
        if not pending:
            return True
        if self.mode == "paper":
            # A paper write performs no remote mutation, so an interrupted intent can be rejected.
            for row in pending:
                self.store.update_intent(row["id"], "rejected", {"reason": "interrupted_paper_transaction"})
            return True
        wallet = self.api.balance()
        shorts = self.api.short_positions()
        for row in pending:
            p, response = row["payload"], row["response"] or {}
            pair = p.get("signal", p.get("position"))["pair"]
            direction = p.get("signal", p.get("position"))["direction"]
            action = p["action"]
            detail = response.get("OrderDetail", {})
            server_id = detail.get("OrderID")
            if server_id is not None:
                orders = self.api.query_orders(order_id=server_id)
            else:
                # Unknown writes have no idempotency key on the exchange. Paginate the relevant
                # order history instead of resubmitting, and require a unique matching order.
                orders = []
                for page in range(10):
                    chunk = self.api.query_orders(limit=100, offset=page * 100)
                    orders.extend(chunk)
                    if len(chunk) < 100:
                        break
                side = ("BUY" if direction == 1 else "SHORT_OPEN") if action == "open" else ("SELL" if direction == 1 else "SHORT_CLOSE")
                orders = [o for o in orders if o.get("Pair") == pair and o.get("Side") == side and abs(int(o.get("CreateTimestamp", 0)) - int(p["request_timestamp"])) <= 60_000]
                if direction == 1:
                    precision = int(self.meta["TradePairs"][pair]["AmountPrecision"])
                    orders = [o for o in orders if abs(float(o.get("Quantity", 0)) - float(p["quantity"])) <= 10 ** -precision]
            # A direct acknowledged short response plus authoritative position state is sufficient.
            if direction == -1 and action == "open" and response.get("Status") == "OPEN":
                orders = [{"OrderID": response["ID"], "Status": "FILLED", "FinishTimestamp": response.get("CreateTimestamp", row["created_ms"])}]
            if direction == -1 and action == "close" and response.get("Success") and "ClosedQty" in response:
                orders = [{"OrderID": "short-close-" + row["id"], "Status": "FILLED", "FinishTimestamp": row["created_ms"]}]
            if len(orders) != 1:
                self.store.event("reconciliation_blocked", {"intent": row["id"], "matching_orders": len(orders), "reason": "ambiguous_matching_history_no_resubmit"})
                continue
            o = orders[0]
            if o.get("Status") in {"CANCELED", "REJECTED"} and float(o.get("FilledQuantity", 0)) == 0:
                self.store.update_intent(row["id"], "rejected", o)
                continue
            if o.get("Status") != "FILLED":
                self.store.event("reconciliation_wait", {"intent": row["id"], "status": o.get("Status")})
                continue
            positions = self.store.positions()
            timestamp = int(o.get("FinishTimestamp") or o.get("CreateTimestamp") or row["created_ms"])
            if action == "open":
                s = Signal(**p["signal"])
                if direction == 1:
                    qty = wallet_qty(wallet, pair) - float(p["before_qty"])
                    entry = float(o.get("FilledAverPrice") or detail.get("FilledAverPrice") or o.get("Price") or p["quote_entry"])
                    fee = float(o.get("CommissionChargeValue", detail.get("CommissionChargeValue", 0)))
                    if o.get("CommissionCoin", detail.get("CommissionCoin")) == pair.split("/")[0]:
                        fee *= entry
                    collateral = 0.
                    server_id = str(o["OrderID"])
                else:
                    matches = [q for q in shorts if q["Pair"] == pair]
                    if len(matches) != 1:
                        continue
                    q = matches[0]
                    qty, entry, collateral = float(q["ShortQty"]), float(q["EntryPrice"]), float(q["Collateral"])
                    fee = float(response.get("OpenFee", qty * entry * self.cfg.fee_per_side))
                    server_id = str(q["ID"])
                if qty <= 0 or any(q.pair == pair for q in positions):
                    continue
                position = Position(pair, s.strategy, direction, qty, entry, timestamp, entry * (1 - direction * self.cfg.stop_atr * s.atr_fraction), entry * (1 + direction * self.cfg.target_atr * s.atr_fraction), collateral, fee, server_id)
                positions.append(position)
            else:
                old = Position(**p["position"])
                if direction == 1:
                    remaining = wallet_qty(wallet, pair)
                    if remaining > old.quantity + 1e-8:
                        continue
                    positions = [q for q in positions if q.pair != pair]
                    if remaining * old.entry >= self.cfg.minimum_order_notional:
                        old.quantity = remaining
                        positions.append(old)
                else:
                    positions = [q for q in positions if q.pair != pair]
                    for q in shorts:
                        if q["Pair"] == pair:
                            old.quantity, old.collateral = float(q["ShortQty"]), float(q["Collateral"])
                            positions.append(old)
                self.store.set("cooldown_" + pair, timestamp + self.cfg.cooldown_hours * HOUR)
            fill = {"pair": pair, "action": action, "direction": direction, "order": o, **self._stamp()}
            self.store.complete(row["id"], positions, fill, str(o["OrderID"]), timestamp)
            self.store.event("fill_reconciled", {"intent": row["id"], "pair": pair, "action": action, "order_id": str(o["OrderID"]), **self._stamp()}, timestamp)
        return not self.store.unfinished()
