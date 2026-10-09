# Correcting unfilled entries

The supplied log lines show `phase: idle` and unchanged $100,000 equity. They do not show an order that is currently pending, its limit price, a server rejection, or a confirmed cancellation. The old `cycles_today: 6` meant six accepted entry submissions, not six completed trades. These lines alone cannot prove why the earlier orders did not execute. The uploaded archive contains no runtime ledger or signed account responses.

The running log names commit `a2745173...+dirty` and configuration hash `ff0b9e...`. The uploaded source archive names baseline commit `137f3a9...`. Any unprovided changes on the deployed instance remain outside this review. The corrections here are made against the uploaded archive.

## Findings and corrections

1. **The entry was unnecessarily far from an executable buy.** The old rule was `MaxBid - one tick`. At the public quote checked during the investigation, PEPE's bid was 0.00000390, ask was 0.00000391, and last price was 0.00000391. The old buy was 0.00000389, two ticks below the ask. The corrected default is 0.00000390: the best bid, still below the ask. A locked quote is handled by staying one tick below the ask. The allowed spread is at most two ticks. This improves the placement, but does not guarantee execution.
2. **The stability filter compared different price series.** It divided the current bid by an older last-traded price. For a coarse-tick token, an unchanged trade price with a lower bid could trigger the falling-market filter. It now compares last prices to last prices.
3. **Rejections were effectively hidden in the database.** The bot now prints `tick_order_rejected` with the exchange's redacted error and backs off for five minutes. It reports cooldown, warm-up, volume, fee margin, drawdown, timing and daily limits as named decisions.
4. **Submission counts were mislabeled.** `entry_attempts_today`, `accepted_entries_today`, `filled_entries_today`, `filled_orders_today` and `completed_cycles_today` are separate HKT-day counts derived from the durable journal. `max_cycles_per_day: 24` now caps actual completed cycles; the separate attempt limit is 48. The old ambiguous `cycles_today` field is removed.
5. **Expiry and diagnostics were insufficient.** A passive buy can now wait ten minutes rather than five. A pending-order log includes its actual ID, status, role, limit, filled quantity, quote distances and age. A crossed public quote with a pending signed order is reported; it is never treated as a client-created fill.
6. **Residual dust could stop the bot after an exit.** Tracked or immaterial dust below the minimum trading notional no longer triggers an unmanaged-holding error. Its value remains included in equity. Material unmanaged holdings still block new trading.
7. **A realized sample could survive a failed exit transaction.** State, shadow cash, fill journal and realized sample now commit together. Restart recovery does not duplicate samples. The scaling gate requires at least twenty complete cycles under the current mode and configuration; older strategy samples do not validate the revised policy.
8. **The daily loss halt now stays latched.** It resets on the next Hong Kong calendar day. A drawdown halt remains persistent. Free USD, rather than locked cash, limits new order size. Risk and account reconciliation remain independent of entry warm-up.

## What the documentation establishes

The official README documents the `/v3/place_order` endpoint, `type=LIMIT`, the required `price`, decimal and quantity precision, minimum order notional, HMAC SHA-256 signing, millisecond timestamps, order queries and owned-order cancellation. These parts of the original transport were already aligned with the documented contract.

It does not specify a complete spot-limit matching engine or a post-only parameter. A resting buy below the ask is a passive order candidate, not an obligation for the server to fill it. Public price touches do not prove a signed order execution, and a LIMIT label is not a substitute for checking actual maker/taker role and commission.

Source: https://github.com/roostoo/Roostoo-API-Documents

The code submits only documented exchange fields. Internal audit details are not added to signed request bodies. There is no automatic market-buy fallback to manufacture fills. Market sells remain reserved for risk/time/cutoff exits after cancellation and balance reconciliation. Maker mismatch pauses further entries while existing inventory continues to be managed.

## Updating an existing EC2 or tmux installation

1. Stop the current bot in its tmux pane with Ctrl+C, or stop its existing systemd service. Stopping does not liquidate its holdings. Keep the process down only for the update and resume the same ledger promptly.
2. Back up the existing installation and ledger while the process is stopped. Preserve `.env`, `runtime/`, any SQLite WAL/SHM files, and your deployed `.git` history. Do not replace a live working directory by blindly extracting this archive over it.
3. Extract this corrected archive to a separate staging directory. Copy `sentinel/tick.py`, `sentinel/tick_report.py`, `sentinel/execution.py`, `sentinel/__main__.py`, the corrected tests and documentation into the existing project. Merge the new `tick_config.json` values into your current configuration. In particular, use `entry_behind_bid_ticks: 0`, `entry_ttl_seconds: 600`, `max_spread_ticks: 2`, `max_entry_attempts_per_day: 48`, `rejection_backoff_seconds: 300`, and `report_interval_seconds: 60`. Review any existing overrides and the competition cutoff.
4. Keep `runtime_dir` pointing to the existing runtime directory. Do not delete or reset `runtime/tick_paper_exchange.sqlite3`. The corrected code reads the old schema and recomputes real counts. It continues existing owned orders and holdings and retains unresolved-write protection.
5. Activate the existing virtual environment and export the same private environment variables. A systemd `EnvironmentFile` supplies them to the service; a terminal must supply them separately. Do not paste credentials into logs or Git.

Run from the existing project root:

```bash
python -m unittest discover -s tests -v
python -m sentinel tick-status --mode paper-exchange --remote --output runtime/diagnostics
```

The diagnosis command reads the existing local database and signed account history. It never submits or cancels an order. Inspect `remote.pending_orders`, `remote.recent_orders`, `recent_errors`, `last_decision`, and `unresolved_writes`. Missing local state with existing holdings requires restoring the correct ledger; do not force ownership or clear an unresolved write to bypass it.

Resume one process only:

```bash
python -m sentinel tick-run --mode paper-exchange
```

The example service still defaults to shadow mode. An authenticated service deployment must select `paper-exchange` explicitly. It shares the instance lock with the old bot; do not run both.

## Logs and exports

Every approximately thirty seconds, successful monitoring writes a portfolio event with actual journal counts and a named decision. The loop's timing can be longer when requests are slow. Approximately every sixty seconds, it replaces:

```text
runtime/reports/paper-exchange/summary.txt
runtime/reports/paper-exchange/trade_logs.csv
runtime/reports/paper-exchange/status.json
```

`trade_logs.csv` contains recorded, reconciled executed orders, including executed portions of terminal canceled orders. Partial execution still pending is visible in `tick_order_status`; it is not invented as a completed cycle. The UTC/IST timestamps are local reconciliation times. A SELL row includes recorded net realized P&L; BUY rows leave realized P&L blank. Shadow reports use a separate directory and identify local simulations. No external messages are sent.

Manually export or inspect local data without contacting the exchange:

```bash
python -m sentinel tick-status --mode paper-exchange --output runtime/manual_report
```

For a new unauthenticated test installation, use `python -m sentinel tick-run --mode shadow --once`. Do not use a new empty runtime directory on an account that already has live bot state.

## Verification and limits

The regression suite has 67 tests, including the original 46 and 21 added cases. They cover the documented signature, pending-quantity echo, pricing, no invented fill on quote crossing, a confirmed buy/sell lifecycle, cancel/fill races, rejection backoff, true counts, atomic exit P&L, dust, daily halts, ledger migration and read-only diagnosis. API fixtures are simulated. The public quote check is real and read-only.

No private competition credentials were provided here and no authenticated account order was executed during the fix. Actual account matching and profitability are still unverified. The historical return tables are unchanged records of the earlier exploratory candle simulations, not measured performance of the corrected best-bid policy. Client-side stops can be delayed by outages or unresolved writes. The cutoff is still the supplied provisional configuration and should match the organizer's actual deadline.
