from __future__ import annotations

import json
from dataclasses import asdict
from pathlib import Path

import numpy as np
import pandas as pd

from .config import Config
from .data import DAY, HOUR, read_bars
from .research import Evidence, calibration, exit_bar, label_setups, net_return
from .risk import Position, allocation, day_key, loss_breach
from .signals import aligned_features, make_signal, signal_arrays


def metrics(equity: pd.DataFrame, trades: list[dict], cfg: Config) -> dict:
    nav = equity.equity.to_numpy()
    peak = np.maximum.accumulate(nav)
    ret = float(nav[-1] / cfg.initial_equity - 1)
    dd = float(np.min(nav / peak - 1))
    series = pd.Series(nav, index=pd.to_datetime(equity.timestamp, unit="ms", utc=True)).tz_convert(cfg.competition_timezone)
    daily = series.resample("D").last().pct_change().dropna()
    # Descriptive daily ratios. Exact organizer annualization/downside conventions are unpublished.
    std = float(daily.std(ddof=1)) if len(daily) > 1 else 0.
    downside = float(np.sqrt(np.mean(np.minimum(daily.to_numpy(), 0) ** 2))) if len(daily) else 0.
    sharpe = float(np.sqrt(365) * daily.mean() / std) if std > 1e-12 else None
    sortino = float(np.sqrt(365) * daily.mean() / downside) if downside > 1e-12 else None
    calendar_days = max((equity.timestamp.iloc[-1] - equity.timestamp.iloc[0]) / DAY, 1.)
    annual_return = (max(nav[-1] / cfg.initial_equity, 1e-12)) ** (365 / calendar_days) - 1
    calmar = float(annual_return / abs(dd)) if dd < -1e-12 else None
    orders_by_day: dict[str, int] = {}
    for t in trades:
        for k in ("entry_ts", "exit_ts"):
            d = day_key(int(t[k]), cfg.competition_timezone)
            orders_by_day[d] = orders_by_day.get(d, 0) + 1
    wins = [t["pnl"] for t in trades if t["pnl"] > 0]
    losses = [t["pnl"] for t in trades if t["pnl"] < 0]
    return {
        "return": ret, "max_drawdown": dd, "closed_trades": len(trades),
        "win_rate": len(wins) / len(trades) if trades else None,
        "profit_factor": sum(wins) / -sum(losses) if losses else None,
        "provisional_active_days": sum(n >= cfg.minimum_daily_filled_orders for n in orders_by_day.values()),
        "filled_orders_by_hkt_day": orders_by_day,
        "sharpe_daily_annualized": sharpe, "sortino_daily_annualized": sortino, "calmar_annualized": calmar,
        "fees": sum(t["fees"] for t in trades),
        "notes": "Synthetic market fills on actual Binance candles. Active-day count uses a provisional daily minimum; not confirmed compliance. Short-period annualized ratios are unstable."
    }


class Backtester:
    def __init__(self, cfg: Config, directory: str | Path | None = None):
        self.cfg = cfg
        bars = {p: read_bars(directory or cfg.data_dir, p) for p in cfg.pairs}
        self.frames = aligned_features(bars)
        self.btc = self.frames["BTC/USD"]
        self.timestamps = self.btc.timestamp.to_numpy(dtype=np.int64)
        self.arrays = {p: {k: f[k].to_numpy() for k in ("open", "high", "low", "close", "atr_fraction", "qv24", "adx", "efficiency", "z")} for p, f in self.frames.items()}
        self.masks = {p: signal_arrays(f, self.btc, cfg) for p, f in self.frames.items()}
        self.outcomes = label_setups(self.frames, cfg)
        self.calibration_cache = {}

    def evidence_at(self, t: int):
        # UTC-day frozen calibration; all outcomes used are known at that day's START.
        cutoff = int(t // DAY * DAY)
        if cutoff not in self.calibration_cache:
            self.calibration_cache[cutoff] = calibration(self.outcomes, cutoff, self.cfg)
        return self.calibration_cache[cutoff]

    def episode(self, start_ms: int, end_ms: int, require_evidence: bool = True) -> tuple[dict, pd.DataFrame, list[dict]]:
        cfg = self.cfg
        idx = np.flatnonzero((self.timestamps >= start_ms) & (self.timestamps < end_ms))
        if len(idx) < 2 or int(idx[0]) <= cfg.warmup_bars:
            raise ValueError("Episode has insufficient data or prehistory")
        if int(self.timestamps[idx[0]]) != start_ms or int(self.timestamps[idx[-1]]) + HOUR != end_ms:
            raise ValueError("Episode boundaries must have complete hourly data")
        cash = cfg.initial_equity
        positions: list[Position] = []
        cooldown: dict[str, int] = {}
        trades = []
        equity_rows = [{"timestamp": start_ms, "equity": cash, "gross_weight": 0.}]
        peak, day_start = cash, cash
        current_day = day_key(start_ms, cfg.competition_timezone)
        entries_today = 0
        permanent_halt = None
        daily_halt = False
        rejected = {}
        slip = cfg.friction_bps_per_side / 10000
        def quote_equity(i: int, at_close: bool):
            column = "close" if at_close else "open"
            return cash + sum(p.liquidation_value(self.arrays[p.pair][column][i] * (1 - p.direction * slip), cfg.fee_per_side) for p in positions)
        def close(p: Position, i: int, px: float, why: str, timestamp: int):
            nonlocal cash
            px *= 1 - p.direction * slip
            cash += p.liquidation_value(px, cfg.fee_per_side)
            fees = p.entry_fee + p.quantity * px * cfg.fee_per_side
            pnl = p.notional * net_return(p.entry, px, p.direction, cfg.fee_per_side)
            trades.append({"pair": p.pair, "strategy": p.strategy, "direction": p.direction, "entry_ts": p.entry_ts, "exit_ts": min(timestamp, end_ms - 1), "entry": p.entry, "exit": px, "notional": p.notional, "pnl": pnl, "fees": fees, "reason": why})
            positions.remove(p)
            cooldown[p.pair] = timestamp + cfg.cooldown_hours * HOUR
        for i in idx:
            i = int(i)
            t = int(self.timestamps[i])
            equity_open = quote_equity(i, False)
            d = day_key(t, cfg.competition_timezone)
            if d != current_day:
                current_day, day_start, entries_today, daily_halt = d, equity_open, 0, False
            peak = max(peak, equity_open)
            breach = loss_breach(equity_open, day_start, peak, cfg)
            if breach:
                if breach == "daily_loss":
                    daily_halt = True
                else:
                    permanent_halt = breach
                for p in positions.copy():
                    close(p, i, float(self.arrays[p.pair]["open"][i]), breach, t)
            candidates = []
            if not permanent_halt and not daily_halt and i > 0:
                book = self.evidence_at(t) if require_evidence else {}
                for p, masks in self.masks.items():
                    for (strategy, direction), mask in masks.items():
                        if mask[i - 1]:
                            s = make_signal(p, strategy, direction, self.frames[p].iloc[i - 1])
                            ev = book.get(s.key)
                            if require_evidence and (ev is None or not ev.accepted):
                                why = ev.reason if ev else "no_completed_samples"
                                rejected[why] = rejected.get(why, 0) + 1
                            else:
                                candidates.append((s, ev))
                candidates.sort(key=lambda x: ((x[1].mean_lcb or 0.) if x[1] else 0., x[0].score), reverse=True)
                for s, ev in candidates:
                    if entries_today >= cfg.max_entries_per_day or t < cooldown.get(s.pair, 0):
                        continue
                    # No chase of a gap between signal close and next candle opening.
                    raw_entry = float(self.arrays[s.pair]["open"][i])
                    prior_close = float(self.arrays[s.pair]["close"][i - 1])
                    if abs(raw_entry / prior_close - 1) > cfg.max_basis_bps / 10000:
                        continue
                    entry = raw_entry * (1 + s.direction * slip)
                    eq = quote_equity(i, False)
                    correlations = {}
                    for p in positions:
                        x = self.frames[s.pair].ret1.iloc[max(1, i - 720):i]
                        y = self.frames[p.pair].ret1.iloc[max(1, i - 720):i]
                        corr = x.corr(y)
                        correlations[p.pair] = float(corr) if np.isfinite(corr) else 1.
                    marks = {p.pair: float(self.arrays[p.pair]["open"][i]) for p in positions}
                    notional, why = allocation(s, entry, eq, cash, positions, cfg, correlations, marks)
                    if notional <= 0:
                        rejected[why] = rejected.get(why, 0) + 1
                        continue
                    qty = notional / entry
                    fee = notional * cfg.fee_per_side
                    cash -= notional + fee
                    positions.append(Position(s.pair, s.strategy, s.direction, qty, entry, t, entry * (1 - s.direction * cfg.stop_atr * s.atr_fraction), entry * (1 + s.direction * cfg.target_atr * s.atr_fraction), notional if s.direction == -1 else 0., fee))
                    entries_today += 1
            # New positions can hit a barrier within their entry candle. Same-bar ambiguity is adverse.
            for p in positions.copy():
                a = self.arrays[p.pair]
                bar = {x: a[x][i] for x in ("open", "high", "low", "close")}
                force = t + HOUR - p.entry_ts >= cfg.max_hold_hours * HOUR or i == int(idx[-1])
                hit = exit_bar(p.entry, p.stop, p.target, p.direction, bar, force)
                if hit:
                    close(p, i, hit[0], "episode_end" if i == int(idx[-1]) and hit[1] == "time" else hit[1], t + HOUR)
            equity_close = quote_equity(i, True)
            peak = max(peak, equity_close)
            breach = loss_breach(equity_close, day_start, peak, cfg)
            if breach:
                if breach == "daily_loss":
                    daily_halt = True
                else:
                    permanent_halt = breach
                for p in positions.copy():
                    close(p, i, float(self.arrays[p.pair]["close"][i]), breach, t + HOUR)
                equity_close = cash
            # Marked gross exposure must obey the cap too; sell rather than adding leverage.
            marked_gross = sum(p.quantity * max(p.entry, float(self.arrays[p.pair]["close"][i])) for p in positions)
            if positions and marked_gross > equity_close * cfg.max_gross_weight:
                for p in positions.copy():
                    close(p, i, float(self.arrays[p.pair]["close"][i]), "gross_cap", t + HOUR)
                equity_close = cash
            equity_rows.append({"timestamp": t + HOUR, "equity": equity_close, "gross_weight": sum(p.notional for p in positions) / max(equity_close, 1.)})
        df = pd.DataFrame(equity_rows)
        summary = metrics(df, trades, cfg)
        summary.update({"start_utc": pd.to_datetime(start_ms, unit="ms", utc=True).isoformat(), "end_utc": pd.to_datetime(end_ms, unit="ms", utc=True).isoformat(), "terminal_halt": permanent_halt, "rejected_signals": rejected, "config_hash": cfg.fingerprint, "require_evidence": require_evidence})
        return summary, df, trades

    def episodes(self, start: str, end: str, days: int = 14, require_evidence: bool = True) -> tuple[list[dict], pd.DataFrame, list[dict]]:
        a, b = pd.Timestamp(start, tz="UTC"), pd.Timestamp(end, tz="UTC")
        summaries, curves, trades = [], [], []
        while a + pd.Timedelta(days=days) <= b:
            nxt = a + pd.Timedelta(days=days)
            summary, curve, ts = self.episode(int(a.timestamp() * 1000), int(nxt.timestamp() * 1000), require_evidence)
            summaries.append(summary)
            curve["episode"] = len(summaries)
            curves.append(curve)
            for t in ts:
                t["episode"] = len(summaries)
            trades.extend(ts)
            a = nxt
        if not summaries:
            raise ValueError("No complete evaluation episodes")
        return summaries, pd.concat(curves, ignore_index=True), trades


def run_backtest(cfg: Config, start: str, end: str, output: str, episode_days: int = 14, require_evidence: bool = True) -> dict:
    bt = Backtester(cfg)
    summaries, curve, trades = bt.episodes(start, end, episode_days, require_evidence)
    returns = np.asarray([s["return"] for s in summaries])
    report = {"config_hash": cfg.fingerprint, "start": start, "end_exclusive": end, "episode_days": episode_days,
              "episodes": summaries, "aggregate": {"episode_count": len(summaries), "median_return": float(np.median(returns)), "mean_return": float(returns.mean()), "worst_return": float(returns.min()), "best_return": float(returns.max()), "fraction_over_one_percent": float(np.mean(returns > .01)), "fraction_positive": float(np.mean(returns > 0)), "fraction_with_eight_provisional_active_days": float(np.mean([s["provisional_active_days"] >= cfg.required_active_days for s in summaries])), "total_trades": len(trades), "win_rate": sum(t["pnl"] > 0 for t in trades) / len(trades) if trades else None},
              "assumptions": {"fee_per_side": cfg.fee_per_side, "friction_bps_per_side": cfg.friction_bps_per_side, "calibration": "UTC-day frozen; only already-completed labels", "data": "Binance hourly USDT spot proxy, not historical Roostoo executions", "intrabar": "stop before target; gap losses at opening price; 60-second live stop latency not reproduced", "selection_bias": "current liquid-coin whitelist; historical survivorship and researcher selection remain", "no_evidence_mode": not require_evidence}}
    dest = Path(output)
    dest.mkdir(parents=True, exist_ok=True)
    (dest / "summary.json").write_text(json.dumps(report, indent=2, allow_nan=False))
    curve.to_csv(dest / "equity.csv", index=False)
    pd.DataFrame(trades).to_csv(dest / "trades.csv", index=False)
    return report
