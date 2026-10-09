from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd

from .config import Config
from .data import HOUR, validate_frame


@dataclass(frozen=True)
class Signal:
    pair: str
    strategy: str
    direction: int  # +1 = long; -1 = collateralized 1x short, never a spot oversell
    timestamp: int  # first possible execution time, AFTER the signal candle closes
    atr_fraction: float
    score: float
    quote_volume_24h: float

    @property
    def key(self) -> tuple[str, str, int]:
        return self.pair, self.strategy, self.direction


def features(bars: pd.DataFrame) -> pd.DataFrame:
    validate_frame(bars, contiguous=True)
    f = bars.copy()
    c, h, l = f.close, f.high, f.low
    f["ema12"] = c.ewm(span=12, adjust=False, min_periods=12).mean()
    f["ema48"] = c.ewm(span=48, adjust=False, min_periods=48).mean()
    f["ema192"] = c.ewm(span=192, adjust=False, min_periods=192).mean()
    f["slow_slope"] = f.ema192 / f.ema192.shift(24) - 1
    f["ema_slope"] = f.ema48 / f.ema48.shift(6) - 1
    tr = pd.concat([h - l, (h - c.shift()).abs(), (l - c.shift()).abs()], axis=1).max(axis=1)
    f["atr"] = tr.ewm(alpha=1 / 14, adjust=False, min_periods=14).mean()
    f["atr_fraction"] = f.atr / c
    f["ret1"] = c.pct_change()
    f["ret24"] = c.pct_change(24)
    f["ret48"] = c.pct_change(48)
    f["efficiency"] = (c - c.shift(24)).abs() / c.diff().abs().rolling(24).sum().replace(0, np.nan)
    dm_up, dm_down = h.diff(), -l.diff()
    plus = pd.Series(np.where((dm_up > dm_down) & (dm_up > 0), dm_up, 0.), index=f.index)
    minus = pd.Series(np.where((dm_down > dm_up) & (dm_down > 0), dm_down, 0.), index=f.index)
    plus = 100 * plus.ewm(alpha=1 / 14, adjust=False).mean() / f.atr
    minus = 100 * minus.ewm(alpha=1 / 14, adjust=False).mean() / f.atr
    dx = 100 * (plus - minus).abs() / (plus + minus).replace(0, np.nan)
    f["adx"] = dx.ewm(alpha=1 / 14, adjust=False, min_periods=14).mean()
    mid = c.rolling(48).mean()
    f["z"] = (c - mid) / c.rolling(48).std().replace(0, np.nan)
    f["high24"] = h.rolling(24).max().shift(1)
    f["low24"] = l.rolling(24).min().shift(1)
    f["qv24"] = f.quote_volume.rolling(24).sum()
    return f


def atr_multiplier(strategy: str) -> float:
    return 2.5 if strategy == "swing_trend" else 1.


def signal_arrays(f: pd.DataFrame, btc: pd.DataFrame, cfg: Config) -> dict[tuple[str, int], np.ndarray]:
    if not np.array_equal(f.timestamp.to_numpy(), btc.timestamp.to_numpy()):
        raise ValueError("Pair and BTC candle grids must match exactly")
    good = (f.index >= cfg.warmup_bars) & (f.qv24 >= cfg.min_quote_volume_24h) & f.atr_fraction.between(cfg.min_atr_fraction, cfg.max_atr_fraction)
    edge_ok = (cfg.target_atr * f.atr_fraction >= cfg.min_target_cost_multiple * cfg.roundtrip_cost)
    # Do not chase an exceptionally large last candle.
    good &= f.ret1.abs() <= 1.5 * f.atr_fraction
    trend_up = (btc.close > btc.ema48) & (btc.ema_slope > 0) & (f.close > f.ema48) & (f.ema12 > f.ema48) & (f.ema_slope > 0) & (f.adx >= 20) & (f.efficiency >= .20)
    trend_down = (btc.close < btc.ema48) & (btc.ema_slope < 0) & (f.close < f.ema48) & (f.ema12 < f.ema48) & (f.ema_slope < 0) & (f.adx >= 20) & (f.efficiency >= .20)
    range_ok = (btc.adx < 22) & (btc.efficiency < .30) & (btc.ret24.abs() < .025) & (f.adx < 25) & (f.efficiency < .30)
    masks = {
        ("pullback", 1): trend_up & (f.close > f.ema12) & (f.close.shift() <= f.ema12.shift()) & (f.ret1 > 0),
        ("pullback", -1): trend_down & (f.close < f.ema12) & (f.close.shift() >= f.ema12.shift()) & (f.ret1 < 0),
        ("breakout", 1): trend_up & (f.close > f.high24) & (f.close.shift() <= f.high24.shift()),
        ("breakout", -1): trend_down & (f.close < f.low24) & (f.close.shift() >= f.low24.shift()),
        ("range_reversion", 1): range_ok & (f.z.shift() < -2) & (f.ret1 > 0) & (f.z < -.8),
        ("range_reversion", -1): range_ok & (f.z.shift() > 2) & (f.ret1 < 0) & (f.z > .8),
        ("swing_trend", 1): (btc.ema48 > btc.ema192) & (btc.slow_slope > 0) & (btc.ret48 > 0) & (f.ema48 > f.ema192) & (f.close > f.ema48) & (f.slow_slope > 0) & (f.ret48 > 0) & ((f.timestamp + HOUR) % (6 * HOUR) == 0),
        ("swing_trend", -1): (btc.ema48 < btc.ema192) & (btc.slow_slope < 0) & (btc.ret48 < 0) & (f.ema48 < f.ema192) & (f.close < f.ema48) & (f.slow_slope < 0) & (f.ret48 < 0) & ((f.timestamp + HOUR) % (6 * HOUR) == 0),
    }
    return {key: (mask & good & ((cfg.target_atr * f.atr_fraction * atr_multiplier(key[0]) >= cfg.min_target_cost_multiple * cfg.roundtrip_cost) if key[0] == "swing_trend" else edge_ok)).fillna(False).to_numpy() for key, mask in masks.items() if key[0] in cfg.strategies and (key[1] == 1 or cfg.enable_shorts)}


def make_signal(pair: str, strategy: str, direction: int, row: pd.Series) -> Signal:
    # This is a heuristic ordering score, explicitly NOT a probability of profit.
    score = float(row.adx / 100 + row.efficiency) if strategy != "range_reversion" else float(abs(row.z) / 3)
    return Signal(pair, strategy, direction, int(row.timestamp) + HOUR, float(row.atr_fraction) * atr_multiplier(strategy), score, float(row.qv24))


def aligned_features(bars: dict[str, pd.DataFrame]) -> dict[str, pd.DataFrame]:
    # Never compress a missing hour into adjacency by intersecting and proceeding.
    start = max(int(v.timestamp.iloc[0]) for v in bars.values())
    end = min(int(v.timestamp.iloc[-1]) for v in bars.values())
    out = {}
    for p, df in bars.items():
        x = df[(df.timestamp >= start) & (df.timestamp <= end)].reset_index(drop=True)
        out[p] = features(x)
    reference = out["BTC/USD"].timestamp.to_numpy()
    if any(not np.array_equal(v.timestamp.to_numpy(), reference) for v in out.values()):
        raise ValueError("Unequal candle grids; repair the source data")
    return out
