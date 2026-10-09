from __future__ import annotations

import json
import signal as process_signal
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict
from pathlib import Path

import numpy as np
import pandas as pd

from .api import ApiError, Roostoo
from .config import Config
from .data import DAY, HOUR, LiveData
from .execution import Executor, validated_quote
from .research import calibration, evidence_json, label_setups
from .risk import allocation, day_key, loss_breach, quote_exit_reason
from .signals import aligned_features, make_signal, signal_arrays
from .state import Store


def code_version() -> str:
    try:
        r = subprocess.run(["git", "rev-parse", "HEAD"], capture_output=True, text=True, timeout=2, check=True)
        status = subprocess.run(["git", "status", "--porcelain", "--untracked-files=normal"], capture_output=True, text=True, timeout=2, check=True)
        return r.stdout.strip() + ("+dirty" if status.stdout.strip() else "")
    except (OSError, subprocess.SubprocessError):
        return "uncommitted-package"


class InstanceLock:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.file = path.open("a+b")
        try:
            import fcntl
            fcntl.flock(self.file, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.kind = "posix"
        except ImportError:
            import msvcrt
            self.file.seek(0)
            self.file.write(b"0")
            self.file.flush()
            self.file.seek(0)
            msvcrt.locking(self.file.fileno(), msvcrt.LK_NBLCK, 1)
            self.kind = "windows"
        except OSError:
            self.file.close()
            raise RuntimeError("Another bot owns this state directory; never run concurrent instances") from None

    def close(self):
        self.file.close()


def activity_report(fills: list[dict], cfg: Config, now_ms: int) -> dict:
    start = int(pd.Timestamp(cfg.competition_start).timestamp() * 1000)
    end = int(pd.Timestamp(cfg.competition_end).timestamp() * 1000)
    counts = {}
    for f in fills:
        ts = int(f["timestamp_ms"])
        if not start <= ts < end:
            continue
        d = day_key(ts, cfg.competition_timezone)
        counts[d] = counts.get(d, 0) + 1
    days = sum(v >= cfg.minimum_daily_filled_orders for v in counts.values())
    today = pd.Timestamp(day_key(now_ms, cfg.competition_timezone))
    last_day = pd.Timestamp(day_key(end - 1, cfg.competition_timezone))
    days_remaining = max(0, (last_day - today).days + 1) if now_ms < end else 0
    today_qualifies = counts.get(str(today.date()), 0) >= cfg.minimum_daily_filled_orders
    possible = days + days_remaining - int(today_qualifies and days_remaining > 0)
    return {"filled_orders_by_hkt_day": counts, "provisional_active_days": days, "required_active_days": cfg.required_active_days, "maximum_possible_from_now": possible, "schedule_at_risk": possible < cfg.required_active_days,
            "daily_minimum_used": cfg.minimum_daily_filled_orders,
            "warning": "The organizer says enough trades without a number. This is a provisional activity count, not a compliance guarantee. No quota-only trades are generated."}


def public_preflight(cfg: Config, authenticated=False) -> dict:
    api = Roostoo(cfg.roostoo_base_url, cfg.request_spacing_seconds)
    now = api.sync_time()
    info, ticker = api.exchange_info(), api.ticker()
    results = []
    for p in cfg.pairs:
        meta = info.get("TradePairs", {}).get(p, {})
        try:
            bid, ask = validated_quote(ticker, p)
            spread = (ask - bid) / ((ask + bid) / 2) * 10000
            result = {"pair": p, "tradable": meta.get("CanTrade") is True, "asset_type": meta.get("AssetType", "not_specified"), "amount_precision": meta.get("AmountPrecision"), "price_precision": meta.get("PricePrecision"), "minimum_notional": meta.get("MiniOrder"), "spread_bps": spread, "quote_volume_24h": ticker["Data"][p].get("UnitTradeValue")}
        except ApiError as e:
            result = {"pair": p, "error": str(e)}
        results.append(result)
    report = {"server_time_utc": pd.to_datetime(now, unit="ms", utc=True).isoformat(), "exchange_running": info.get("IsRunning"), "configured_pairs": results, "public_initial_wallet": info.get("InitialWallet"), "competition_initial_equity_configured": cfg.initial_equity, "note": "The unauthenticated InitialWallet is generic; size from actual signed balances. This preflight NEVER places trades."}
    if authenticated:
        wallet, shorts, pending = api.balance(), api.short_positions(), api.query_orders(pending_only=True)
        report["account"] = {"free_usd": wallet.get("USD", {}).get("Free", 0), "locked_usd": wallet.get("USD", {}).get("Lock", 0), "nonzero_holdings": [c for c, v in wallet.items() if c != "USD" and float(v.get("Free", 0)) + float(v.get("Lock", 0)) > 0], "open_short_pairs": [s["Pair"] for s in shorts], "pending_orders": len(pending), "short_endpoint_readable": True, "short_trade_permission": "not inferred from a read; validate competition permission before enabling shorts"}
    return report


class Runner:
    def __init__(self, cfg: Config, mode: str):
        self.cfg, self.mode = cfg, mode
        root = Path(cfg.runtime_dir)
        self.lock = InstanceLock(root / "bot.lock")  # shared by modes: prevent accidentally running both
        self.store = Store(root / f"{mode}.sqlite3")
        self.api = Roostoo(cfg.roostoo_base_url, cfg.request_spacing_seconds)
        self.api.sync_time()
        self.meta = self.api.exchange_info()
        for p in cfg.pairs:
            if self.meta.get("TradePairs", {}).get(p, {}).get("CanTrade") is not True:
                raise ApiError(f"Configured pair unavailable: {p}")
        if mode == "live" and not self.api.authenticated:
            raise ApiError("Live mode requires keys in the private EC2 environment")
        self.exec = Executor(self.api, self.store, cfg, mode, self.meta, code_version())
        self.feed = LiveData(cfg.data_base_url, cfg.data_dir, cfg.request_spacing_seconds)
        self.frames, self.book, self.masks = {}, {}, {}
        self.pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="public-market-data")
        self.future = None
        self.feed_hour = None
        self.feed_retry_after = 0.
        self.stopping = False
        self.store.event("startup", {"mode": mode, "config_hash": cfg.fingerprint, "commit": code_version(), "shorts_enabled": cfg.enable_shorts})

    def _prepare_feed(self, now: int):
        bars = {p: self.feed.update(p, now, self.cfg.calibration_days + 20) for p in self.cfg.pairs}
        frames = aligned_features(bars)
        book = calibration(label_setups(frames, self.cfg), now // DAY * DAY, self.cfg)
        masks = {p: signal_arrays(f, frames["BTC/USD"], self.cfg) for p, f in frames.items()}
        return frames, book, masks

    def refresh(self, now: int, blocking=False):
        if blocking:
            self.frames, self.book, self.masks = self._prepare_feed(now)
            self.feed_hour = now // HOUR
            return
        if self.future is not None and self.future.done():
            try:
                self.frames, self.book, self.masks = self.future.result()
                out = Path(self.cfg.runtime_dir) / f"{self.mode}_evidence.json"
                out.write_text(json.dumps(evidence_json(self.book), indent=2, allow_nan=False))
                self.store.event("evidence_updated", {"accepted_cells": sum(e.accepted for e in self.book.values()), "total_cells": len(self.book)})
            except Exception as e:
                self.store.event("market_data_failed", {"error": f"{type(e).__name__}: {e}", "action": "new_entries_paused; risk monitoring continues"})
                self.feed_hour = None
                self.feed_retry_after = time.monotonic() + 300
                self.frames = {}
            self.future = None
        if self.future is None and self.feed_hour != now // HOUR and time.monotonic() >= self.feed_retry_after:
            self.future = self.pool.submit(self._prepare_feed, now)
            self.feed_hour = now // HOUR

    def _entry_count(self, now: int) -> int:
        return sum(f.get("action") == "open" and day_key(f["timestamp_ms"], self.cfg.competition_timezone) == day_key(now, self.cfg.competition_timezone) for f in self.store.fills())

    def cycle(self, allow_entries=True):
        cfg = self.cfg
        if time.monotonic() - self.api.last_sync > 1800:
            self.api.sync_time()
        now = self.api.now_ms()
        ticker = self.api.ticker()
        if abs(now - int(ticker.get("ServerTime", 0))) > 120000:
            raise ApiError("Roostoo ticker timestamp stale")
        if not self.exec.reconcile():
            self.store.event("writes_paused", {"reason": "unresolved_remote_order"}, now)
            return
        eq, cash, wallet, shorts = self.exec.snapshot(ticker)
        self.exec.ownership_check(ticker, wallet, shorts)
        risk = self.store.get("risk", {"day": day_key(now, cfg.competition_timezone), "day_start": eq, "peak": eq, "permanent_halt": None, "daily_halt": False})
        if risk["day"] != day_key(now, cfg.competition_timezone):
            risk.update({"day": day_key(now, cfg.competition_timezone), "day_start": eq, "daily_halt": False})
        risk["peak"] = max(risk["peak"], eq)
        breach = loss_breach(eq, risk["day_start"], risk["peak"], cfg)
        if breach == "daily_loss":
            risk["daily_halt"] = True
        elif breach:
            risk["permanent_halt"] = breach
        start = int(pd.Timestamp(cfg.competition_start).timestamp() * 1000)
        end = int(pd.Timestamp(cfg.competition_end).timestamp() * 1000)
        # Exact end time is provisional. Close five minutes before the configured cutoff.
        finishing = now >= end - 5 * 60 * 1000
        positions = self.store.positions()
        marked_gross = sum(p.quantity * max(p.entry, sum(validated_quote(ticker, p.pair)) / 2) for p in positions)
        for p in positions:
            bid, ask = validated_quote(ticker, p.pair)
            price = bid if p.direction == 1 else ask
            why = risk["permanent_halt"] or ("daily_loss" if risk["daily_halt"] else None) or ("competition_end" if finishing else None) or ("gross_cap" if marked_gross > eq * cfg.max_gross_weight else None) or quote_exit_reason(p, price, now, cfg)
            if why:
                self.exec.close(p, str(why), ticker, wallet, now)
                if self.store.unfinished():
                    break
                eq, cash, wallet, shorts = self.exec.snapshot(ticker)
        self.store.set("risk", risk)
        self.refresh(now)
        can_enter = allow_entries and start <= now < end - cfg.max_hold_hours * HOUR and not risk["permanent_halt"] and not risk["daily_halt"] and not self.store.unfinished() and self.frames
        if can_enter and self._entry_count(now) < cfg.max_entries_per_day:
            choices = []
            for pair, f in self.frames.items():
                row = f.iloc[-1]
                execution_ts = int(row.timestamp) + HOUR
                # An hourly signal may enter only in its first ten minutes, never chase an old bar.
                if not 0 <= now - execution_ts <= 10 * 60 * 1000:
                    continue
                for (strategy, direction), mask in self.masks[pair].items():
                    if mask[-1]:
                        s = make_signal(pair, strategy, direction, row)
                        ev = self.book.get(s.key)
                        if ev and ev.accepted:
                            choices.append((s, ev))
                        else:
                            self.store.event("signal_rejected", {"pair": pair, "strategy": strategy, "direction": direction, "reason": ev.reason if ev else "no_samples"}, now)
            choices.sort(key=lambda x: (x[1].mean_lcb, x[0].score), reverse=True)
            for s, ev in choices:
                if self._entry_count(now) >= cfg.max_entries_per_day or self.store.unfinished():
                    break
                if now < int(self.store.get("cooldown_" + s.pair, 0)):
                    continue
                # Refresh the executable quote/account before sizing, after the data worker
                # or earlier candidate executions may have consumed time.
                ticker = self.api.ticker()
                if abs(self.api.now_ms() - int(ticker.get("ServerTime", 0))) > 120000:
                    raise ApiError("Entry quote became stale")
                eq, cash, wallet, shorts = self.exec.snapshot(ticker)
                bid, ask = validated_quote(ticker, s.pair)
                mid = (bid + ask) / 2
                if (ask - bid) / mid * 10000 > cfg.max_spread_bps:
                    continue
                if float(ticker["Data"][s.pair].get("UnitTradeValue", 0)) < cfg.min_quote_volume_24h:
                    continue
                # Concurrent reference quote, not a comparison to an hour-old candle close.
                reference = self.feed.reference_price(s.pair)
                if abs(mid / reference - 1) * 10000 > cfg.max_basis_bps:
                    self.store.event("basis_rejected", {"pair": s.pair, "basis_bps": (mid / reference - 1) * 10000}, now)
                    continue
                positions = self.store.positions()
                corrs = {}
                for p in positions:
                    corr = self.frames[s.pair].ret1.tail(720).corr(self.frames[p.pair].ret1.tail(720))
                    corrs[p.pair] = float(corr) if np.isfinite(corr) else 1.
                marks = {p.pair: sum(validated_quote(ticker, p.pair)) / 2 for p in positions}
                notional, reason = allocation(s, ask if s.direction == 1 else bid, eq, cash, positions, cfg, corrs, marks)
                if notional > 0:
                    self.store.event("signal_admitted", {"pair": s.pair, "strategy": s.strategy, "direction": s.direction, "historical_win_rate": ev.win_rate, "historical_mean_net": ev.mean_net, "historical_mean_lcb": ev.mean_lcb, "sample_size": ev.n, "notional": notional}, now)
                    self.exec.open(s, notional, ticker, wallet, self.api.now_ms())
                    eq, cash, wallet, shorts = self.exec.snapshot(ticker)
        self.store.event("portfolio", {"mode": self.mode, "equity_liquidation_estimate": eq, "competition_return": eq / cfg.initial_equity - 1, "positions": len(self.store.positions()), "entry_halt": risk["permanent_halt"] or ("daily_loss" if risk["daily_halt"] else None), "feed_ready": bool(self.frames)}, now)
        self.store.set("activity", activity_report(self.store.fills(), cfg, now))

    def run(self, once=False):
        def stop(signum, frame):
            self.stopping = True
        process_signal.signal(process_signal.SIGTERM, stop)
        process_signal.signal(process_signal.SIGINT, stop)
        if once:
            # One-shot mode is for verifying data and decisions; paper unless explicitly live.
            self.refresh(self.api.now_ms(), blocking=True)
        while not self.stopping:
            before = time.monotonic()
            try:
                self.cycle()
            except (ApiError, OSError, ValueError, KeyError) as e:
                self.store.event("loop_error", {"error": f"{type(e).__name__}: {e}", "action": "no_entry; next_loop_retries_reads"})
            if once:
                break
            # Short waits keep SIGTERM responsive; request/quote polling remains low frequency.
            remaining = max(0., self.cfg.poll_seconds - (time.monotonic() - before))
            while remaining > 0 and not self.stopping:
                wait = min(1., remaining)
                time.sleep(wait)
                remaining -= wait
        self.pool.shutdown(wait=False, cancel_futures=True)
        self.store.close()
        self.lock.close()
