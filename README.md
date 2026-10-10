# Roostoo Sentinel

A selective, cost-aware trading research package and Roostoo execution bot for the October 2026 competition. Original implementation; the supplied GitHub repository was studied for architecture, not copied.

**Execution correction:** the default tick entry now joins the best bid, rather than bidding an extra tick below it. Unfilled entries expire after ten minutes. See [FILL_CORRECTIONS.md](FILL_CORRECTIONS.md) for diagnosis, new reports and how to update a running installation without losing its ledger. Historical research files describe the original simulations; they are not validated account returns for this corrected execution policy.

**Status: research candidate with a new, fill-sensitive PEPE limit strategy.** The tick hypothesis produced positive simulated returns under touch fills, but failed trade-through stress. Actual Roostoo matching remains unverified. Read [TICK_STRATEGY_RESEARCH.md](TICK_STRATEGY_RESEARCH.md) first. The original hourly configuration remains separate and did not establish profitability or activity compliance.

Read [STRATEGY_RESEARCH.md](STRATEGY_RESEARCH.md) for evidence, results, applicable rules, unresolved details and instrument selection.

## New sequential tick candidate

The user clarified that sequential tick scalping is permitted. `tick_config.json` enables PEPE only, buys at the actual best bid while keeping its price below the ask, then submits a fee-aware sell limit above the filled entry. It uses one position and one resting order, starts at $100 per entry, and scales only after at least twenty completed cycles with positive net execution checks under the current configuration. The research report explains the difference between trade-price simulations and executable quote matching. Shadow observations never count as real mock-exchange fills.

```bash
python -m sentinel tick-run --mode shadow --once
python -m sentinel tick-run --mode shadow
```

With private competition keys configured in the EC2 environment, the continuous mock-exchange command is:

```bash
python -m sentinel tick-run --mode paper-exchange
```

Use `deploy/roostoo-tick.service` for this alternative. Do not run both services. Preserve `runtime/tick_paper_exchange.sqlite3`, examine actual maker roles/fees and net probe results, and keep the authenticated operation under supervision until matching and cancellation are verified. No authenticated order was placed while preparing this package. The full-size simulated results do not include the initial small-order stage.

Read-only diagnosis with the same private environment variables:

```bash
python -m sentinel tick-status --mode paper-exchange --remote --output runtime/diagnostics
```

The running tick bot updates `runtime/reports/paper-exchange/summary.txt`, `trade_logs.csv`, and `status.json` approximately every minute. Shadow reports go to `runtime/reports/shadow/`. Confirmed order records and closed cycles are counted separately from attempted entries. `tick_order_rejected` includes the redacted exchange error; `tick_order_status` shows pending prices and quote distances; `tick_decision` explains every idle gate. Export files stay on the machine; no external messaging is configured.

Reproduce the new analysis with `python research/download_limit_data.py`, `python research/evaluate_tick_cycles.py`, and `python research/validate_tick_candidate.py`. These use the included compressed minute candles and archive checksums. The download dates are explicit research endpoints, not rolling current-date defaults.

## Package contents

| Component | Purpose |
|---|---|
| `sentinel/data.py` | Checksummed Binance archives, hourly public data and strict candle validation |
| `sentinel/signals.py` | BTC-regime-conditioned pullback, breakout and range-reversion hypotheses; optional slower swing trend |
| `sentinel/research.py` | Completed trade labels, fee-aware historical outcomes and block-bootstrap admission gate |
| `sentinel/backtest.py` | Portfolio sizing and independent 14-day retrospective competition episodes |
| `sentinel/api.py` | Official Roostoo signing, price/amount precision and v6 short endpoint support |
| `sentinel/execution.py` | Write-ahead intents, fill reconciliation and account ownership checks |
| `sentinel/state.py` | Durable SQLite state, fill journal, configuration/commit audit events |
| `sentinel/risk.py` | Risk sizing, correlated exposure caps, stop/target/time exits and portfolio loss limits |
| `sentinel/runner.py` | Continuous monitoring, background data refresh, single-instance lock, activity reports |
| `sentinel/tick.py` | Sequential best-bid limit execution, fee reconciliation and explicit decision diagnostics |
| `sentinel/tick_report.py` | Read-only account diagnosis and periodic summary/trade exports |
| `deploy/roostoo-sentinel.service` | EC2 systemd example, initially in paper mode |
| `tests/` | Critical behavior tests, including timeout/restart and label leakage cases |
| `research/` | Actual development failures, retrospective results, benchmarks and test output |
| `data/` | Historical candles used in the research, plus their archive provenance manifest |

## Local installation and verification

Python 3.11 or later is required. Commands below are run from this directory.

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
python -m unittest discover -s tests -v
python -m sentinel preflight
python -m sentinel collect
python -m sentinel run --mode paper --once
```

The collector and public preflight make no trading requests. Paper mode requires no trading credentials and uses a separate simulated ledger. `--once` is prohibited in live mode. A paper iteration staying in cash is an expected outcome when no signal passes, not a sign that the process is broken.

The archive contains research candles. To reproduce from the original published files:

```bash
python -m sentinel download --start 2025-01-01 --end 2026-09-30
python -m sentinel backtest --start 2026-01-01 --end 2026-10-01 --output research/reproduced_default
python -m sentinel backtest --start 2026-01-01 --end 2026-10-01 --output research/reproduced_cost_150 --cost-multiplier 1.5
python -m sentinel evidence --as-of 2026-10-01T00:00:00Z
```

`--end` is exclusive for backtests and inclusive for archive downloads. The monthly downloader accepts completed months. The backtester uses whole, independent 14-day windows and omits a final partial window. Research outputs name the evaluated start and end of each window. Never call a zero-trade period a high win rate or assign it an infinite Sharpe/Sortino/Calmar ratio.

## Decision logic

1. Collect completed hourly candles only; reject gaps, stale data and malformed OHLC.
2. Generate a proposed directional setup on a whitelisted instrument.
3. Check the trailing historical outcomes for that pair, setup and direction. Admit only if all sample-size, win-rate, net-mean and bootstrap requirements pass.
4. Check executable spread, liquidity, concurrent Binance/Roostoo basis, cooldown, portfolio limits and the configured competition clock.
5. Size from the loss to the stop plus round-trip costs; submit at most one directional market entry.
6. Monitor existing positions independently of the background data worker. Exit at the observed stop/target, time limit, exposure breach, portfolio loss breach or approaching cutoff.

The rank score is a heuristic priority score. It is **not** an estimated probability of profit. Historical win rates and confidence bounds are descriptive admission checks, not guarantees. A cost-aware target distance is not itself a forecast of expected return.

The initial whitelist is BTC, ETH, BNB, SOL, XRP, LINK, AVAX, AAVE, SUI and LTC versus USD. Only cells with passing evidence are eligible to trade. Newly added tokens, tokenized equity symbols and scaled meme-token mappings are not inferred automatically. PAXG would require a separate commodity regime model and its own validation.

## Risk and execution defaults

| Setting | Default |
|---|---:|
| Planned risk per entry, including modeled costs | 0.15% of equity |
| Aggregate planned stop risk | 0.45% |
| Maximum gross allocation | 10% (Hard cap: $10,000 max trading capital) |
| Maximum single-asset allocation | 5% ($5,000) |
| Hard trading capital ceiling | $10,000 USD (strictly enforced; 90% stays in cash) |
| Same-direction correlated group cap | 10% ($10,000) |
| Maximum concurrent positions | 3 |
| Maximum new entries per HKT day | 4 |
| Daily equity loss halt | 0.75% |
| Peak-to-current equity drawdown halt | 2.5% |
| Stop / target | 1.6 / 2.4 entry-time hourly ATR |
| Maximum holding time | 24 hours |
| Symbol cooldown after closing | 4 hours |
| Risk/quote monitoring cadence | approximately 60 seconds |

The slower swing strategy uses 2.5 times hourly ATR as its volatility unit and evaluates entry opportunities every six hours. It is implemented but not enabled in `config.json`; its tests did not support recommending it. Optional short support is also disabled by default. Enabling it requires both competition permission and verified collateral accounting. A spot SELL never opens a short.

The original hourly runner models market fees with an additional five basis points of friction per leg and uses market orders as its execution benchmark. The separate tick runner rests sequential limits and verifies the actual role and commission before allowing more entries. Neither runner assumes a profitable or guaranteed fill. No unrestricted two-sided quoting, latency arbitrage or high-frequency module is included.

Planned loss limits are not hard guarantees. Client-managed stops can miss a price during an outage or a gap. A network timeout leaves the exchange write status unknown. The bot pauses further writes, persists the intent and reconciles account state/order history instead of blindly retrying. An unresolved write can also delay exits. An ambiguous result that cannot be matched uniquely requires investigation of read-only history and the journal; restarting or deleting the database does not resolve it.

## AWS Deployment Guide (Hackathon Official Procedure)

Per the Hackathon AWS Guide, the competition runs on an EC2 instance in **`ap-southeast-2` (Sydney)**:

1. **Launch Instance in AWS Console:**
   - Region: `ap-southeast-2` (Sydney).
   - Launch Template: `Hackathon-Starter-Template` (`t3.medium`, proceed without a key pair).
   - Storage: Default EBS (30 GB limit). Launch only **one** instance.
2. **Connect via AWS Session Manager:**
   - In EC2 Instances, select your instance -> Click **Connect** -> **Session Manager** tab -> Click **Connect**.
3. **Setup and Clone:**
   ```bash
   cd ~
   git clone <your-bot-repo>
   cd roostoo_sentinel
   python3 -m venv .venv
   source .venv/bin/activate
   pip install -r requirements.txt
   ```
4. **Configure API Keys:**
   ```bash
   cp .env.example .env
   # Edit .env with your assigned API Key and Secret
   chmod 600 .env
   export ROOSTOO_API_KEY="your-api-key"
   export ROOSTOO_SECRET_KEY="your-secret-key"
   ```
5. **Run Inside TMUX (Keeps running after browser disconnect):**
   ```bash
   sudo dnf install -y tmux
   tmux new -s trading-bot
   
   # Run the launcher script or command:
   ./start_bot.sh tick paper-exchange
   
   # Detach from TMUX: Press Ctrl+B, then D
   ```
   To reattach to the running session later:
   ```bash
   tmux attach -t trading-bot
   ```
6. **Systemd Alternative (Optional):**
   ```bash
   sudo cp deploy/roostoo-tick.service /etc/systemd/system/
   sudo systemctl daemon-reload
   sudo systemctl enable --now roostoo-tick
   journalctl -u roostoo-tick -f
   ```

## Honest activity auditing

```bash
python -m sentinel audit --mode paper
python -m sentinel audit --remote
```

Paper fills are not competition trades. The remote command reads actual signed order history; it never places an order. The local live journal contains this bot's fills only. Earlier trades by a different bot require the remote history to count them. The configured daily minimum of one is a provisional counter, not an organizer-confirmed definition of an active day. The bot produces no quota-only trades. The current conservative configuration is likely unsuitable as the sole bot for satisfying the activity screen.

## Source control and judging

The package includes a genuine local Git history for this implementation. It does not recreate trades or commits before development. Push this original project to your team's repository, follow the stated submission deadline, and commit every later strategy/configuration change with a reason. Secrets, runtime balances and local data caches are excluded by `.gitignore`.

The source archive is a development deliverable, not a published GitHub submission. No authenticated competition order or AWS deployment was performed while producing it. See `research/test_results.txt`, `research/public_preflight.json`, and `research/validation_comparison.json` for the verification actually completed.
