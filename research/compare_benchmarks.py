"""Transparent opportunity-cost references; these are NOT active-day-compliant bots."""
from pathlib import Path
import json
import pandas as pd

from sentinel.config import Config
from sentinel.data import read_bars, HOUR
from sentinel.research import net_return


def main():
    cfg = Config.load()
    bars = {p: read_bars(cfg.data_dir, p).set_index("timestamp") for p in cfg.pairs}
    start = pd.Timestamp("2026-01-01", tz="UTC")
    end = pd.Timestamp("2026-10-01", tz="UTC")
    result = []
    slip = cfg.friction_bps_per_side / 10000
    while start + pd.Timedelta(days=14) <= end:
        nxt = start + pd.Timedelta(days=14)
        a = int(start.timestamp() * 1000)
        b = int(nxt.timestamp() * 1000) - HOUR
        per_pair = {}
        for p, df in bars.items():
            buy = float(df.loc[a, "open"]) * (1 + slip)
            sell = float(df.loc[b, "close"]) * (1 - slip)
            per_pair[p] = net_return(buy, sell, 1, cfg.fee_per_side)
        result.append({"start": start.isoformat(), "end_exclusive": nxt.isoformat(), "cash": 0., "half_btc_half_cash": .5 * per_pair["BTC/USD"], "half_equal_weight_crypto_half_cash": .5 * sum(per_pair.values()) / len(per_pair)})
        start = nxt
    Path("research/passive_benchmarks.json").write_text(json.dumps({"note": "Passive opportunity-cost references at 50% allocation, net of configured execution costs. They do not satisfy the competition's activity requirement. No Sharpe/Sortino/Calmar optimization is inferred.", "episodes": result}, indent=2))
    for key in ("half_btc_half_cash", "half_equal_weight_crypto_half_cash"):
        vals = [r[key] for r in result]
        print(key, {"mean_return": sum(vals) / len(vals), "fraction_over_one_percent": sum(x > .01 for x in vals) / len(vals)})


if __name__ == "__main__":
    main()
