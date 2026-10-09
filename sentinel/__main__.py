from __future__ import annotations

import argparse
import json
from dataclasses import replace
from pathlib import Path

import pandas as pd

from .config import Config
from .data import LiveData, download_history, read_bars


def main():
    ap = argparse.ArgumentParser(description="Roostoo Sentinel: selective, cost-aware competition bot")
    ap.add_argument("--config", default="config.json")
    sub = ap.add_subparsers(dest="command", required=True)
    h = sub.add_parser("download", help="checksum-verified monthly Binance archives; no trading")
    h.add_argument("--start", required=True)
    h.add_argument("--end", required=True, help="inclusive calendar date; choose completed months")
    h.add_argument("--workers", type=int, default=4)
    sub.add_parser("collect", help="refresh completed hourly history from public Binance REST; no trading")
    p = sub.add_parser("preflight", help="public API checks; authenticated checks remain read-only")
    p.add_argument("--authenticated", action="store_true")
    p.add_argument("--output", default="runtime/preflight.json")
    b = sub.add_parser("backtest", help="independent historical competition-length episodes")
    b.add_argument("--start", required=True)
    b.add_argument("--end", required=True, help="exclusive UTC end date")
    b.add_argument("--output", default="research/backtest")
    b.add_argument("--episode-days", type=int, default=14)
    b.add_argument("--cost-multiplier", type=float, default=1.)
    b.add_argument("--without-evidence", action="store_true", help="diagnostic baseline, never an argument to the live runner")
    e = sub.add_parser("evidence", help="inspect accepted/rejected pair/strategy/direction cells")
    e.add_argument("--as-of", required=True, help="UTC instant, e.g. 2026-10-01T00:00:00Z")
    e.add_argument("--output", default="research/evidence.json")
    r = sub.add_parser("run", help="continuous bot; defaults to paper, uses separate durable ledgers")
    r.add_argument("--mode", choices=["paper", "live"], default="paper")
    r.add_argument("--once", action="store_true")
    a = sub.add_parser("audit", help="count activity; --remote reads actual order history, never trades")
    a.add_argument("--mode", choices=["paper", "live"], default="paper")
    a.add_argument("--remote", action="store_true")
    a.add_argument("--output", default="runtime/activity.json")
    tick = sub.add_parser("tick-run", help="sequential fee-aware limits; default is public-data shadow mode")
    tick.add_argument("--tick-config", default="tick_config.json")
    tick.add_argument("--mode", choices=["shadow", "paper-exchange"], default="shadow")
    tick.add_argument("--once", action="store_true")
    args = ap.parse_args()
    c = None if args.command == "tick-run" else Config.load(args.config)
    if args.command == "tick-run":
        from .tick import run_tick
        run_tick(args.tick_config,args.mode,args.once)
    elif args.command == "download":
        download_history(c.pairs, args.start, args.end, c.data_dir, args.workers)
    elif args.command == "collect":
        from .api import Roostoo
        api = Roostoo(c.roostoo_base_url, c.request_spacing_seconds)
        now = api.sync_time()
        feed = LiveData(c.data_base_url, c.data_dir, c.request_spacing_seconds)
        for pair in c.pairs:
            df = feed.update(pair, now, c.calibration_days + 20)
            print(json.dumps({"pair": pair, "bars": len(df), "latest_complete_hour_utc": pd.to_datetime(int(df.timestamp.iloc[-1]), unit="ms", utc=True).isoformat()}), flush=True)
    elif args.command == "preflight":
        from .runner import public_preflight
        report = public_preflight(c, args.authenticated)
        path = Path(args.output)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(report, indent=2, allow_nan=False))
        print(json.dumps(report, indent=2, allow_nan=False))
    elif args.command == "backtest":
        from .backtest import run_backtest
        if args.cost_multiplier <= 0:
            ap.error("cost multiplier must be positive")
        c = replace(c, fee_per_side=c.fee_per_side * args.cost_multiplier, friction_bps_per_side=c.friction_bps_per_side * args.cost_multiplier)
        report = run_backtest(c, args.start, args.end, args.output, args.episode_days, not args.without_evidence)
        print(json.dumps(report["aggregate"], indent=2))
    elif args.command == "evidence":
        from .research import calibration, evidence_json, label_setups
        from .signals import aligned_features
        cutoff = int(pd.Timestamp(args.as_of).timestamp() * 1000)
        bars = {p: read_bars(c.data_dir, p) for p in c.pairs}
        # Truncate the input before computing historical labels, not just the report at the end.
        frames = aligned_features({p: df[df.timestamp + 3600000 <= cutoff].reset_index(drop=True) for p, df in bars.items()})
        report = evidence_json(calibration(label_setups(frames, c), cutoff, c))
        path = Path(args.output)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(report, indent=2, allow_nan=False))
        print(json.dumps({"accepted": [x for x in report if x["accepted"]], "total_cells": len(report)}, indent=2))
    elif args.command == "run":
        from .runner import Runner
        if args.once and args.mode == "live":
            ap.error("Live trading must run continuously; --once is for paper verification")
        Runner(c, args.mode).run(args.once)
    elif args.command == "audit":
        from .api import Roostoo
        from .runner import activity_report
        from .state import Store
        api = Roostoo(c.roostoo_base_url, c.request_spacing_seconds)
        now = api.sync_time()
        if args.remote:
            found = {}
            for page in range(100):
                rows = api.query_orders(limit=100, offset=page * 100)
                for row in rows:
                    if row.get("Status") == "FILLED":
                        found[str(row["OrderID"])] = {"timestamp_ms": int(row.get("FinishTimestamp") or row["CreateTimestamp"]), "order": row}
                if len(rows) < 100:
                    break
            else:
                raise RuntimeError("History exceeds audit pagination budget; do not claim a complete count")
            fills = list(found.values())
        else:
            store = Store(Path(c.runtime_dir) / f"{args.mode}.sqlite3")
            fills = store.fills()
            store.close()
        report = activity_report(fills, c, now)
        report["source"] = "signed_remote_history" if args.remote else args.mode + "_journal"
        path = Path(args.output)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(report, indent=2))
        print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
