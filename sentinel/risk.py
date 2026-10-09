from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime
from zoneinfo import ZoneInfo

from .config import Config
from .signals import Signal


@dataclass
class Position:
    pair: str
    strategy: str
    direction: int
    quantity: float
    entry: float
    entry_ts: int
    stop: float
    target: float
    collateral: float = 0.
    entry_fee: float = 0.
    server_id: str = ""

    @property
    def notional(self) -> float:
        return self.quantity * self.entry

    def liquidation_value(self, price: float, fee: float) -> float:
        if self.direction == 1:
            return self.quantity * price * (1 - fee)
        pnl = max(-self.collateral, self.quantity * (self.entry - price))
        return self.collateral + pnl - self.quantity * price * fee

    def planned_loss(self, cfg: Config) -> float:
        stop_distance = abs(self.entry - self.stop) / self.entry
        return self.notional * (stop_distance + cfg.roundtrip_cost)

    def to_dict(self) -> dict:
        return asdict(self)


def day_key(now_ms: int, timezone: str) -> str:
    return datetime.fromtimestamp(now_ms / 1000, tz=ZoneInfo(timezone)).date().isoformat()


def loss_breach(equity: float, day_start: float, peak: float, cfg: Config) -> str | None:
    if equity <= 0:
        return "nonpositive_equity"
    if equity <= peak * (1 - cfg.max_drawdown):
        return "maximum_drawdown"
    if peak >= cfg.initial_equity * (1 + cfg.profit_protection_start) and equity <= peak * (1 - cfg.profit_giveback):
        return "profit_giveback"
    if equity <= day_start * (1 - cfg.daily_loss_limit):
        return "daily_loss"
    return None


def allocation(signal: Signal, entry: float, equity: float, cash: float, positions: list[Position], cfg: Config, correlations: dict[str, float] | None = None, marks: dict[str, float] | None = None) -> tuple[float, str]:
    if equity <= 0 or cash <= 0 or len(positions) >= cfg.max_positions:
        return 0., "cash_or_position_limit"
    if any(p.pair == signal.pair for p in positions):
        return 0., "already_exposed_to_pair"
    risk_scale = cfg.profit_protection_risk_scale if equity >= cfg.initial_equity * (1 + cfg.profit_protection_start) else 1.
    stop_fraction = cfg.stop_atr * signal.atr_fraction
    risk_per_dollar = stop_fraction + cfg.roundtrip_cost
    if stop_fraction >= .25 or risk_per_dollar <= 0:
        return 0., "invalid_stop_distance"
    marks = marks or {}
    exposure = {p.pair: p.quantity * max(p.entry, marks.get(p.pair, p.entry)) for p in positions}
    gross = sum(exposure.values())
    used_risk = sum(p.planned_loss(cfg) for p in positions)
    cap = min(
        equity * cfg.risk_per_trade * risk_scale / risk_per_dollar,
        max(0., equity * cfg.max_open_risk * risk_scale - used_risk) / risk_per_dollar,
        equity * cfg.max_position_weight,
        max(0., equity * cfg.max_gross_weight - gross),
        max(0., cash) / (1 + cfg.fee_per_side + cfg.friction_bps_per_side / 10000),
        signal.quote_volume_24h * cfg.max_volume_participation,
    )
    if positions:
        correlations = correlations or {}
        correlated = sum(exposure[p.pair] for p in positions if correlations.get(p.pair, 1.) * p.direction * signal.direction >= cfg.correlation_threshold)
        cap = min(cap, max(0., equity * cfg.max_correlated_weight - correlated))
    if cap < cfg.minimum_order_notional or cap / entry <= 0:
        return 0., "risk_budget_below_minimum_order"
    return float(cap), "sized"


def quote_exit_reason(p: Position, price: float, now_ms: int, cfg: Config) -> str | None:
    if p.direction * (price - p.stop) <= 0:
        return "stop"
    if p.direction * (price - p.target) >= 0:
        return "target"
    if now_ms - p.entry_ts >= cfg.max_hold_hours * 3_600_000:
        return "time"
    return None
