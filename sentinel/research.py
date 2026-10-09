from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Iterable

import numpy as np
import pandas as pd

from .config import Config
from .data import DAY, HOUR
from .signals import Signal, atr_multiplier, make_signal, signal_arrays


def exit_bar(entry: float, stop: float, target: float, direction: int, bar, force: bool = False) -> tuple[float, str] | None:
    o, h, l, c = (float(bar[k]) for k in ("open", "high", "low", "close"))
    if direction == 1:
        if o <= stop:
            return o, "stop_gap"
        if l <= stop:
            return stop, "stop"
        if h >= target:
            return target, "target"
    else:
        if o >= stop:
            return o, "stop_gap"
        if h >= stop:
            return stop, "stop"
        if l <= target:
            return target, "target"
    # A stop takes priority when both barriers are inside one candle.
    return (c, "time") if force else None


def net_return(entry: float, exit_price: float, direction: int, fee: float) -> float:
    # Return per dollar of entry notional; short collateral is entry notional at 1x.
    return direction * (exit_price / entry - 1) - fee * (1 + exit_price / entry)


@dataclass(frozen=True)
class Outcome:
    pair: str
    strategy: str
    direction: int
    entry_ts: int
    known_ts: int
    net: float
    reason: str
    entry_price: float
    exit_price: float

    @property
    def key(self) -> tuple[str, str, int]:
        return self.pair, self.strategy, self.direction


def label_setups(frames: dict[str, pd.DataFrame], cfg: Config) -> list[Outcome]:
    result = []
    btc = frames["BTC/USD"]
    slip = cfg.friction_bps_per_side / 10000
    for pair, f in frames.items():
        a = {k: f[k].to_numpy() for k in ["timestamp", "open", "high", "low", "close", "atr_fraction"]}
        for (strategy, direction), mask in signal_arrays(f, btc, cfg).items():
            next_available = 0
            for i in np.flatnonzero(mask):
                j = int(i) + 1
                if j < next_available or j >= len(f):
                    continue
                entry = float(a["open"][j]) * (1 + direction * slip)
                distance = a["atr_fraction"][i] * atr_multiplier(strategy)
                stop = entry * (1 - direction * cfg.stop_atr * distance)
                target = entry * (1 + direction * cfg.target_atr * distance)
                for k in range(j, min(j + cfg.max_hold_hours, len(f))):
                    bar = {x: a[x][k] for x in ("open", "high", "low", "close")}
                    hit = exit_bar(entry, stop, target, direction, bar, k == j + cfg.max_hold_hours - 1)
                    if hit:
                        price, reason = hit
                        price *= 1 - direction * slip
                        known = int(a["timestamp"][k]) + HOUR
                        result.append(Outcome(pair, strategy, direction, int(a["timestamp"][j]), known, net_return(entry, price, direction, cfg.fee_per_side), reason, entry, price))
                        next_available = k + 1 + cfg.cooldown_hours
                        break
    return sorted(result, key=lambda x: (x.known_ts, x.entry_ts, x.pair, x.strategy, x.direction))


@dataclass(frozen=True)
class Evidence:
    n: int
    blocks: int
    win_rate: float
    mean_net: float
    mean_lcb: float | None
    accepted: bool
    reason: str


def evidence(sample: Iterable[Outcome], cfg: Config, seed: int = 20261009) -> Evidence:
    rows = list(sample)
    if not rows:
        return Evidence(0, 0, 0., 0., None, False, "no_completed_samples")
    returns = np.asarray([x.net for x in rows])
    groups: dict[int, list[float]] = {}
    for x in rows:
        groups.setdefault(x.entry_ts // (cfg.bootstrap_block_hours * HOUR), []).append(x.net)
    n, b = len(rows), len(groups)
    win, mean = float(np.mean(returns > 0)), float(np.mean(returns))
    if n < cfg.calibration_min_trades or b < cfg.calibration_min_blocks:
        return Evidence(n, b, win, mean, None, False, "insufficient_history")
    sums = np.asarray([sum(v) for v in groups.values()])
    counts = np.asarray([len(v) for v in groups.values()])
    rng = np.random.default_rng(seed)
    draws = rng.integers(0, b, (cfg.bootstrap_replicates, b))
    boot = sums[draws].sum(axis=1) / counts[draws].sum(axis=1)
    lcb = float(np.quantile(boot, cfg.bootstrap_quantile))
    ok = win >= cfg.calibration_min_win_rate and mean >= cfg.calibration_min_net_mean and lcb > cfg.min_net_edge_lcb
    return Evidence(n, b, win, mean, lcb, ok, "passed" if ok else "weak_net_edge_or_hit_rate")


def calibration(outcomes: list[Outcome], now_ms: int, cfg: Config) -> dict[tuple[str, str, int], Evidence]:
    grouped: dict[tuple[str, str, int], list[Outcome]] = {}
    start = now_ms - cfg.calibration_days * DAY
    for x in outcomes:
        # Labels whose exit candle is not closed yet cannot enter calibration.
        if start <= x.entry_ts and x.known_ts <= now_ms:
            grouped.setdefault(x.key, []).append(x)
    return {key: evidence(rows, cfg) for key, rows in grouped.items()}


def evidence_json(book: dict[tuple[str, str, int], Evidence]) -> list[dict]:
    return [{"pair": k[0], "strategy": k[1], "direction": k[2], **asdict(v)} for k, v in sorted(book.items())]
