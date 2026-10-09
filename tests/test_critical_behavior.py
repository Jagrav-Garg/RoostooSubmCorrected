from __future__ import annotations

import contextlib
import io
import json
import sqlite3
import sys
import tempfile
import unittest
import urllib.error
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

import numpy as np
import pandas as pd

from sentinel.api import AmbiguousWrite, ApiError, Roostoo, decimal_string, signature
from sentinel.config import Config
from sentinel.data import HOUR, normalize_frame, validate_frame
from sentinel.execution import Executor
from sentinel.research import Outcome, calibration, evidence, exit_bar, label_setups, net_return
from sentinel.risk import Position, allocation, loss_breach
from sentinel.runner import InstanceLock, activity_report
from sentinel.signals import Signal, features
from sentinel.state import Store


def candles(n=400):
    rng = np.random.default_rng(22)
    c = 100 * np.exp(np.cumsum(rng.normal(0, .005, n)))
    o = np.r_[c[0], c[:-1]]
    return pd.DataFrame({"timestamp": np.arange(n, dtype=np.int64) * HOUR, "open": o, "high": np.maximum(o, c) * 1.003, "low": np.minimum(o, c) * .997, "close": c, "volume": np.ones(n) * 100000, "quote_volume": np.ones(n) * 10000000})


class SignalAndDataTests(unittest.TestCase):
    def test_future_candles_cannot_change_past_features(self):
        data = candles()
        past = features(data.iloc[:320].copy())
        altered = data.copy()
        altered.loc[320:, ["open", "high", "low", "close"]] *= 9
        # Preserve each changed candle's OHLC constraints, while introducing a huge future jump.
        pd.testing.assert_frame_equal(past, features(altered).iloc[:320].reset_index(drop=True))

    def test_gaps_are_not_forward_filled(self):
        bad = candles().drop(index=20).reset_index(drop=True)
        with self.assertRaisesRegex(ValueError, "gap"):
            features(bad)

    def test_microsecond_archive_timestamp_is_normalized(self):
        data = candles(3)
        data.timestamp = (data.timestamp + 1735689600000) * 1000
        fixed = normalize_frame(data)
        self.assertEqual(int(fixed.timestamp.iloc[0]), 1735689600000)
        self.assertEqual(int(fixed.timestamp.iloc[1] - fixed.timestamp.iloc[0]), HOUR)

    def test_malformed_ohlc_is_rejected(self):
        data = candles(3)
        data.loc[1, "low"] = data.loc[1, "high"] * 2
        with self.assertRaises(ValueError):
            validate_frame(data)


class ResearchTests(unittest.TestCase):
    def test_both_barriers_in_one_candle_uses_stop(self):
        bar = {"open": 100, "high": 120, "low": 80, "close": 110}
        self.assertEqual(exit_bar(100, 95, 108, 1, bar), (95, "stop"))
        self.assertEqual(exit_bar(100, 105, 92, -1, bar), (105, "stop"))

    def test_stop_gap_uses_worse_open_not_stop_price(self):
        bar = {"open": 80, "high": 90, "low": 75, "close": 85}
        self.assertEqual(exit_bar(100, 95, 108, 1, bar), (80, "stop_gap"))

    def test_fees_apply_to_both_notional_legs(self):
        self.assertAlmostEqual(net_return(100, 110, 1, .001), .1 - .0021)
        self.assertAlmostEqual(net_return(100, 90, -1, .001), .1 - .0019)
        self.assertLess(net_return(100, 100.1, 1, .001), 0)

    def test_unfinished_future_labels_are_excluded(self):
        cfg = replace(Config(), calibration_min_trades=10, calibration_min_blocks=5)
        known = Outcome("BTC/USD", "pullback", 1, 0, HOUR, .01, "target", 100, 101)
        future = Outcome("BTC/USD", "pullback", 1, 0, 3 * HOUR, 99., "target", 100, 999)
        stats = calibration([known, future], 2 * HOUR, cfg)[known.key]
        self.assertEqual(stats.n, 1)
        self.assertAlmostEqual(stats.mean_net, .01)

    def test_high_win_rate_with_negative_expectancy_is_rejected(self):
        cfg = replace(Config(), calibration_min_trades=10, calibration_min_blocks=5)
        values = [.001] * 18 + [-.02] * 6
        rows = [Outcome("BTC/USD", "pullback", 1, i * 48 * HOUR, i * 48 * HOUR + HOUR, r, "time", 100, 100) for i, r in enumerate(values)]
        ev = evidence(rows, cfg)
        self.assertEqual(ev.win_rate, .75)
        self.assertFalse(ev.accepted)
        self.assertLess(ev.mean_net, 0)

    def test_supported_positive_edge_can_pass_the_gate(self):
        cfg = replace(Config(), calibration_min_trades=10, calibration_min_blocks=5)
        rows = [Outcome("BTC/USD", "pullback", 1, i * 48 * HOUR, i * 48 * HOUR + HOUR, .005, "target", 100, 100.5) for i in range(30)]
        self.assertTrue(evidence(rows, cfg).accepted)

    def test_prefix_label_results_match_live_history(self):
        cfg = replace(Config(), pairs=["BTC/USD"], strategies=["pullback", "breakout", "swing_trend"], min_quote_volume_24h=1, min_atr_fraction=.0001)
        f = features(candles(1500))
        full = label_setups({"BTC/USD": f}, cfg)
        cut = 1100 * HOUR
        prefix = label_setups({"BTC/USD": f.iloc[:1100].copy()}, cfg)
        known = [o for o in full if o.known_ts <= cut]
        self.assertGreater(len(known), 2)
        self.assertEqual(known, prefix)


class RiskTests(unittest.TestCase):
    def setUp(self):
        self.cfg = Config()
        self.sig = Signal("ETH/USD", "pullback", 1, HOUR, .01, .7, 1e10)

    def test_sizing_respects_planned_loss_including_costs(self):
        notional, reason = allocation(self.sig, 100, 100000, 100000, [], self.cfg)
        self.assertGreater(notional, 0)
        self.assertLessEqual(notional * (self.cfg.stop_atr * .01 + self.cfg.roundtrip_cost), 150.00001)
        self.assertLessEqual(notional, 20000)

    def test_marked_gross_and_correlated_caps(self):
        p = Position("BTC/USD", "pullback", 1, 290, 100, 0, 99, 104)
        notional, _ = allocation(self.sig, 100, 100000, 70000, [p], self.cfg, {"BTC/USD": 1}, {"BTC/USD": 110})
        self.assertEqual(notional, 0.)

    def test_existing_pair_cannot_be_averaged_down(self):
        p = Position("ETH/USD", "pullback", 1, 100, 100, 0, 95, 108)
        notional, why = allocation(self.sig, 90, 100000, 90000, [p], self.cfg)
        self.assertEqual(notional, 0.)
        self.assertEqual(why, "already_exposed_to_pair")

    def test_daily_loss_and_total_drawdown_are_distinct(self):
        self.assertEqual(loss_breach(99000, 100000, 100000, self.cfg), "daily_loss")
        self.assertEqual(loss_breach(97000, 98000, 100000, self.cfg), "maximum_drawdown")

    def test_rounding_never_increases_quantity(self):
        self.assertEqual(decimal_string(.123459, 5), "0.12345")


class ApiTests(unittest.TestCase):
    def test_official_signature_fixture(self):
        params = {"timestamp": "1580774512000", "pair": "BNB/USD", "quantity": "2000", "side": "BUY", "type": "MARKET"}
        demo_secret = "S1XP1e3UZj6A7H5fATj0jNhqPxxdSJYdInClVN65XAbvqqMKjVHjA7PZj4W12oep"
        self.assertEqual(signature(params, demo_secret), "20b7fd5550b67b3bf0c1684ed0f04885261db8fdabd38611e9e6af23c19b7fff")

    def test_transport_timeout_does_not_resend_mutation(self):
        api = Roostoo(spacing=0, api_key="fixture", secret="fixture")
        with patch("urllib.request.urlopen", side_effect=urllib.error.URLError("timeout")) as mock:
            with self.assertRaises(AmbiguousWrite):
                api.place("BTC/USD", "BUY", "1.00000")
            self.assertEqual(mock.call_count, 1)

    def test_missing_success_flag_is_an_unknown_write(self):
        api = Roostoo(spacing=0, api_key="fixture", secret="fixture")
        response = contextlib.closing(io.BytesIO(b'{"ErrMsg":"unexpected"}'))
        with patch("urllib.request.urlopen", return_value=response):
            with self.assertRaises(AmbiguousWrite):
                api.place("BTC/USD", "BUY", "1.00000")

    def test_no_order_matched_is_empty_not_transport_failure(self):
        api = Roostoo(spacing=0, api_key="fixture", secret="fixture")
        response = contextlib.closing(io.BytesIO(b'{"Success":false,"ErrMsg":"no order matched"}'))
        with patch("urllib.request.urlopen", return_value=response):
            self.assertEqual(api.query_orders(), [])

    def test_short_open_sends_collateral_not_spot_sell(self):
        api = Roostoo(spacing=0, api_key="fixture", secret="fixture")
        with patch.object(api, "_request", return_value={"Success": True}) as call:
            api.short_open("BTC/USD", "1000.00")
        args, kw = call.call_args
        self.assertEqual(args[1], "/v6/short_open")
        self.assertEqual(args[2], {"pair": "BTC/USD", "collateral": "1000.00"})

    def test_server_error_cannot_echo_credentials_into_logs(self):
        api = Roostoo(spacing=0, api_key="dummy_private_key", secret="dummy_private_secret")
        body = json.dumps({"Success": False, "ErrMsg": "rejected dummy_private_key dummy_private_secret"}).encode()
        with patch("urllib.request.urlopen", return_value=contextlib.closing(io.BytesIO(body))):
            with self.assertRaises(ApiError) as caught:
                api.place("BTC/USD", "BUY", "1.00000")
        self.assertNotIn("dummy_private_key", str(caught.exception))
        self.assertNotIn("dummy_private_secret", str(caught.exception))
        self.assertIn("[redacted]", str(caught.exception))

    def test_live_one_shot_is_rejected_before_exchange_access(self):
        from sentinel.__main__ import main
        with patch.object(sys, "argv", ["sentinel", "run", "--mode", "live", "--once"]), contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit) as caught:
                main()
        self.assertEqual(caught.exception.code, 2)


class RecoveryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "live.sqlite3"
        self.store = Store(self.path)
        self.cfg = replace(Config(), pairs=["BTC/USD"], request_spacing_seconds=0)
        self.meta = {"TradePairs": {"BTC/USD": {"AmountPrecision": 3, "MiniOrder": 1}}}
        self.ticker = {"Data": {"BTC/USD": {"MaxBid": 99.9, "MinAsk": 100.}}}
        self.sig = Signal("BTC/USD", "pullback", 1, 1000, .01, .8, 1e9)

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def test_acknowledged_order_survives_a_followup_read_failure(self):
        class Broker:
            calls = 0
            def place(self, *args):
                self.calls += 1
                return {"Success": True, "OrderDetail": {"OrderID": 9, "Status": "FILLED"}}
            def balance(self):
                raise ApiError("temporary read outage")
        broker = Broker()
        ex = Executor(broker, self.store, self.cfg, "live", self.meta, "fixture")
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertFalse(ex.open(self.sig, 1000, self.ticker, {}, 1000))
            self.assertFalse(ex.open(self.sig, 1000, self.ticker, {}, 1001))
        self.assertEqual(broker.calls, 1)
        self.assertEqual(self.store.unfinished()[0]["status"], "acknowledged")

    def test_unknown_write_recovers_net_coin_quantity_after_restart(self):
        payload = {"action": "open", "signal": self.sig.__dict__, "quantity": "10.000", "before_qty": 0., "quote_entry": 100., "request_timestamp": 1000}
        key = self.store.intent("fixture", payload, 1000)
        self.store.update_intent(key, "unknown")
        self.store.close()
        self.store = Store(self.path)
        class Broker:
            def balance(self):
                return {"BTC": {"Free": 9.99, "Lock": 0}}
            def short_positions(self):
                return []
            def query_orders(self, **kwargs):
                return [{"Pair": "BTC/USD", "Side": "BUY", "OrderID": 9, "Status": "FILLED", "Quantity": 10., "FilledQuantity": 10., "FilledAverPrice": 100., "CreateTimestamp": 1000, "FinishTimestamp": 1001, "CommissionCoin": "BTC", "CommissionChargeValue": .01}]
        ex = Executor(Broker(), self.store, self.cfg, "live", self.meta, "fixture")
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertTrue(ex.reconcile())
        self.assertAlmostEqual(self.store.positions()[0].quantity, 9.99)
        self.assertAlmostEqual(self.store.positions()[0].entry_fee, 1.)
        self.assertEqual(len(self.store.fills()), 1)
        self.assertTrue(ex.reconcile())
        self.assertEqual(len(self.store.fills()), 1)

    def test_ambiguous_multiple_history_matches_remain_paused(self):
        payload = {"action": "open", "signal": self.sig.__dict__, "quantity": "10.000", "before_qty": 0., "quote_entry": 100., "request_timestamp": 1000}
        self.store.intent("fixture", payload, 1000)
        class Broker:
            def balance(self):
                return {"BTC": {"Free": 20.}}
            def short_positions(self):
                return []
            def query_orders(self, **kwargs):
                return [{"Pair": "BTC/USD", "Side": "BUY", "OrderID": i, "Status": "FILLED", "Quantity": 10., "CreateTimestamp": 1000} for i in [9, 10]]
        ex = Executor(Broker(), self.store, self.cfg, "live", self.meta, "fixture")
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertFalse(ex.reconcile())
        self.assertEqual(len(self.store.positions()), 0)

    def test_paper_transaction_failure_rolls_back_cash_and_positions(self):
        ex = Executor(None, self.store, self.cfg, "paper", self.meta, "fixture")
        self.store.db.executescript("CREATE TRIGGER reject_fill BEFORE INSERT ON fills BEGIN SELECT RAISE(ABORT,'fixture failure'); END;")
        with contextlib.redirect_stdout(io.StringIO()):
            with self.assertRaises(sqlite3.IntegrityError):
                ex.open(self.sig, 1000, self.ticker, {}, 1000)
        self.assertEqual(self.store.get("paper_cash"), self.cfg.initial_equity)
        self.assertEqual(self.store.positions(), [])

    def test_single_instance_lock_prevents_double_bots(self):
        lock = InstanceLock(Path(self.tmp.name) / "bot.lock")
        try:
            with self.assertRaises(RuntimeError):
                InstanceLock(Path(self.tmp.name) / "bot.lock")
        finally:
            lock.close()

    def test_short_equity_does_not_double_count_collateral(self):
        class Broker:
            def balance(self):
                return {"USD": {"Free": 90000, "Lock": 10000}}
            def short_positions(self):
                return [{"Pair": "BTC/USD", "EntryPrice": 50000, "ShortQty": .2, "Collateral": 10000}]
        ticker = {"Data": {"BTC/USD": {"MaxBid": 47990, "MinAsk": 48000}}}
        ex = Executor(Broker(), self.store, self.cfg, "live", self.meta, "fixture")
        eq, cash, _, _ = ex.snapshot(ticker)
        self.assertAlmostEqual(eq, 100390.4)
        self.assertEqual(cash, 90000)

    def test_unverified_short_collateral_accounting_fails_closed(self):
        class Broker:
            def balance(self):
                return {"USD": {"Free": 90000, "Lock": 0}}
            def short_positions(self):
                return [{"Pair": "BTC/USD", "EntryPrice": 50000, "ShortQty": .2, "Collateral": 10000}]
        ex = Executor(Broker(), self.store, self.cfg, "live", self.meta, "fixture")
        with self.assertRaisesRegex(ApiError, "accounting"):
            ex.snapshot(self.ticker)

    def test_activity_report_does_not_claim_unspecified_daily_minimum(self):
        now = int(pd.Timestamp("2026-10-11T12:00:00+08:00").timestamp() * 1000)
        report = activity_report([], self.cfg, now)
        self.assertTrue(report["schedule_at_risk"])
        self.assertEqual(report["maximum_possible_from_now"], 7)


if __name__ == "__main__":
    unittest.main()
