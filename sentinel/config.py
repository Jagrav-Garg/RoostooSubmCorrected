from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path


@dataclass(frozen=True)
class Config:
    pairs: list[str] = field(default_factory=lambda: ["BTC/USD", "ETH/USD", "BNB/USD", "SOL/USD", "XRP/USD", "LINK/USD", "AVAX/USD", "AAVE/USD", "SUI/USD", "LTC/USD"])
    strategies: list[str] = field(default_factory=lambda: ["pullback", "breakout", "range_reversion"])
    enable_shorts: bool = False
    initial_equity: float = 100000.
    risk_per_trade: float = .0015
    max_open_risk: float = .0045
    max_position_weight: float = .20
    max_gross_weight: float = .50
    max_correlated_weight: float = .30
    correlation_threshold: float = .75
    max_positions: int = 3
    max_entries_per_day: int = 4
    daily_loss_limit: float = .0075
    max_drawdown: float = .025
    profit_protection_start: float = .012
    profit_protection_risk_scale: float = .5
    profit_giveback: float = .006
    fee_per_side: float = .001
    friction_bps_per_side: float = 5.
    max_spread_bps: float = 12.
    max_basis_bps: float = 50.
    min_quote_volume_24h: float = 20_000_000.
    max_volume_participation: float = .00005
    min_atr_fraction: float = .002
    max_atr_fraction: float = .035
    stop_atr: float = 1.6
    target_atr: float = 2.4
    max_hold_hours: int = 24
    cooldown_hours: int = 4
    calibration_days: int = 180
    calibration_min_trades: int = 25
    calibration_min_blocks: int = 12
    calibration_min_win_rate: float = .55
    calibration_min_net_mean: float = .001
    bootstrap_replicates: int = 600
    bootstrap_block_hours: int = 48
    bootstrap_quantile: float = .10
    min_net_edge_lcb: float = 0.
    min_target_cost_multiple: float = 2.
    warmup_bars: int = 240
    poll_seconds: int = 60
    request_spacing_seconds: float = 2.
    data_base_url: str = "https://data-api.binance.vision"
    roostoo_base_url: str = "https://mock-api.roostoo.com"
    runtime_dir: str = "runtime"
    data_dir: str = "data"
    competition_timezone: str = "Asia/Hong_Kong"
    competition_start: str = "2026-10-04T00:00:00+08:00"
    competition_end: str = "2026-10-18T00:00:00+08:00"
    required_active_days: int = 8
    minimum_daily_filled_orders: int = 1  # provisional lower bound, NOT organizer confirmation
    minimum_order_notional: float = 10.

    @classmethod
    def load(cls, path: str | Path = "config.json") -> "Config":
        d = json.loads(Path(path).read_text())
        unknown = set(d) - {f.name for f in fields(cls)}
        if unknown:
            raise ValueError(f"Unknown configuration keys: {sorted(unknown)}")
        c = cls(**d)
        c.validate()
        return c

    def validate(self) -> None:
        if not self.pairs or "BTC/USD" not in self.pairs:
            raise ValueError("BTC/USD is required as the market regime reference")
        if len(set(self.pairs)) != len(self.pairs) or any(not p.endswith("/USD") for p in self.pairs):
            raise ValueError("Unique /USD pairs are required")
        if any(s not in {"pullback", "breakout", "range_reversion", "swing_trend"} for s in self.strategies):
            raise ValueError("Unsupported strategy")
        if not 0 < self.risk_per_trade <= self.max_open_risk < 1:
            raise ValueError("Invalid risk budget")
        if not 0 < self.max_position_weight <= self.max_gross_weight <= 1:
            raise ValueError("No leverage: 0 < position cap <= gross cap <= 1")
        if not 0 < self.fee_per_side < .1 or self.friction_bps_per_side < 0:
            raise ValueError("Invalid execution cost")
        if self.max_positions < 1 or self.max_entries_per_day < 1 or self.poll_seconds < 30:
            raise ValueError("Invalid positions, entry budget, or polling interval")
        if self.calibration_min_trades < 10 or self.bootstrap_replicates < 100:
            raise ValueError("Insufficient calibration sample or bootstrap draws")
        if not 0 < self.bootstrap_quantile < .5 or self.calibration_min_blocks < 5:
            raise ValueError("Invalid bootstrap confidence or block count")
        if self.calibration_days * 24 < self.warmup_bars + self.max_hold_hours:
            raise ValueError("Calibration window is too short")
        if not 0 < self.min_atr_fraction < self.max_atr_fraction < .2:
            raise ValueError("Invalid ATR filter")
        if self.stop_atr <= 0 or self.target_atr <= 0:
            raise ValueError("Stop and target distances must be positive")
        if not 0 < self.daily_loss_limit <= self.max_drawdown < 1:
            raise ValueError("Invalid loss limits")

    @property
    def fingerprint(self) -> str:
        return hashlib.sha256(json.dumps(asdict(self), sort_keys=True).encode()).hexdigest()

    @property
    def roundtrip_cost(self) -> float:
        return 2 * (self.fee_per_side + self.friction_bps_per_side / 10000)
