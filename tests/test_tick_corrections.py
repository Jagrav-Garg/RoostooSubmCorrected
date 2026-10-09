import contextlib
from dataclasses import asdict, replace
import io
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from sentinel.api import ApiError
from sentinel.execution import validated_quote
from sentinel.state import Store
from sentinel.tick import TickConfig, TickEngine
from sentinel.tick_report import daily_counts, diagnose, export_report, snapshot

META = {'IsRunning': True, 'TradePairs': {'PEPE/USD': {'CanTrade': True, 'PricePrecision': 8, 'AmountPrecision': 0, 'MiniOrder': 1}}}
NOW = 1791356400000  # Within the configured competition period.


def quotes(bid=3.88e-6, ask=3.89e-6, last=3.89e-6, volume=4e7):
    return {'Data': {'PEPE/USD': {'MaxBid': bid, 'MinAsk': ask, 'LastPrice': last, 'UnitTradeValue': volume}}}


class Venue:
    authenticated = True
    def __init__(self):
        self.wallet = {'USD': {'Free': 100000., 'Lock': 0.}}
        self.orders = []
        self.writes = []
        self.now = NOW
        self.reject = False

    def balance(self): return self.wallet

    def query_orders(self, order_id=None, pending_only=False, **kwargs):
        return [r for r in self.orders if (order_id is None or str(r['OrderID']) == str(order_id)) and (not pending_only or r['Status'] == 'PENDING')]

    def place_limit(self, pair, side, quantity, price):
        if self.reject: raise ApiError('permission denied by fixture venue')
        self.writes.append(('LIMIT', side))
        row = {'OrderID': len(self.orders)+1, 'Pair': pair, 'Side': side, 'Type': 'LIMIT', 'Price': float(price), 'Quantity': float(quantity), 'CreateTimestamp': self.now, 'Status': 'PENDING', 'FilledQuantity': 0., 'FilledAverPrice': 0., 'Role': 'MAKER', 'CommissionPercent': .0005, 'CommissionCoin': 'USD', 'CommissionChargeValue': 0}
        self.orders.append(row)
        return {'Success': True, 'OrderDetail': row.copy()}

    def place(self, pair, side, quantity):
        self.writes.append(('MARKET', side))
        raise AssertionError('Unexpected market mutation in this scenario')

    def cancel_order(self, identity):
        self.writes.append(('CANCEL', identity))
        return {'Success': True, 'CanceledList': [identity]}

    def fill(self, row):
        qty, px = row['Quantity'], row['Price']
        row.update(Status='FILLED', FilledQuantity=qty, FilledAverPrice=px, FinishTimestamp=self.now, CommissionChargeValue=qty*px*.0005)
        if row['Side'] == 'BUY':
            self.wallet['USD']['Free'] -= qty*px*1.0005
            self.wallet['PEPE'] = {'Free': qty, 'Lock': 0.}
        else:
            self.wallet['USD']['Free'] += qty*px*.9995
            self.wallet['PEPE']['Free'] -= qty


class CorrectionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.store = Store(self.root/'tick_paper_exchange.sqlite3')
        self.c = replace(TickConfig(), runtime_dir=str(self.root))
        self.api = Venue()
        self.engine = TickEngine(self.c, self.api, self.store, META, 'paper-exchange')
        self.engine.quotes = quotes()
        self.prints = contextlib.redirect_stdout(io.StringIO())
        self.prints.__enter__()

    def tearDown(self):
        self.prints.__exit__(None, None, None)
        self.store.close()
        self.temp.cleanup()

    def warm(self, last=3.89e-6):
        self.store.set('tick_quote_history', [{'t': NOW-offset*30000, 'quotes': {'PEPE/USD': last}} for offset in range(10, 0, -1)])

    def state(self):
        s = self.engine.state()
        s['pair'] = 'PEPE/USD'
        return s

    def events(self, kind):
        return [json.loads(r[0]) for r in self.store.db.execute('SELECT payload FROM events WHERE kind=? ORDER BY id', (kind,))]

    def test_default_entry_joins_bid_instead_of_resting_an_extra_tick_below(self):
        self.warm()
        self.engine.step(quotes(), NOW)
        self.assertEqual(self.api.orders[0]['Price'], 3.88e-6)
        self.assertLess(self.api.orders[0]['Price'], 3.89e-6)
        self.assertLessEqual(self.api.orders[0]['Price']*self.api.orders[0]['Quantity'], 100)

    def test_locked_quote_still_requires_a_non_marketable_price(self):
        self.warm(3.88e-6)
        self.engine.step(quotes(3.88e-6, 3.88e-6, 3.88e-6), NOW)
        self.assertEqual(self.api.orders[0]['Price'], 3.87e-6)

    def test_stable_last_price_is_not_rejected_merely_because_bid_is_lower(self):
        self.warm()
        self.engine.step(quotes(3.87e-6, 3.89e-6), NOW)
        self.assertEqual(len(self.api.orders), 1)

    def test_wide_spread_is_reported_without_a_trade(self):
        self.warm()
        self.engine.step(quotes(3.85e-6, 3.89e-6), NOW)
        self.assertEqual(self.api.writes, [])
        self.assertEqual(self.store.get('tick_last_decision')['pair_checks'][0]['reason'], 'spread_too_wide')

    def test_low_volume_reports_the_blocking_condition(self):
        self.warm()
        self.engine.step(quotes(volume=1000), NOW)
        self.assertEqual(self.store.get('tick_last_decision')['pair_checks'][0]['reason'], 'low_volume')
        self.assertEqual(self.api.writes, [])

    def test_rejected_order_is_visible_and_next_loop_respects_backoff(self):
        self.warm()
        self.api.reject = True
        self.engine.step(quotes(), NOW)
        self.assertIn('permission denied', self.events('tick_order_rejected')[0]['error'])
        self.assertEqual(self.store.get('tick_last_decision')['reason'], 'order_rejected')
        self.engine.step(quotes(), NOW+30000)
        self.assertEqual(self.store.get('tick_last_decision')['reason'], 'cooldown')
        self.assertEqual(daily_counts(self.store.db, NOW, 'paper-exchange')['entry_attempts_today'], 1)

    def test_six_submitted_entries_are_not_six_completed_cycles(self):
        for n in range(6):
            key = self.store.intent(str(n), {'side': 'BUY', 'mode': 'paper-exchange'}, NOW+n)
            self.store.update_intent(key, 'done', {'OrderDetail': {'Status': 'CANCELED', 'FilledQuantity': 0}})
        self.store.set('tick_counters', {'day': daily_counts(self.store.db, NOW, 'paper-exchange')['hkt_day'], 'cycles': 6, 'start_equity': 100000, 'peak': 100000, 'halted': False})
        self.engine.step(quotes(), NOW+30000)
        counts = self.events('tick_portfolio')[-1]
        self.assertEqual(counts['entry_attempts_today'], 6)
        self.assertEqual(counts['unfilled_terminal_entries_today'], 6)
        self.assertEqual(counts['filled_orders_today'], 0)
        self.assertEqual(counts['completed_cycles_today'], 0)
        self.assertNotIn('cycles_today', counts)

    def test_attempt_limit_has_a_named_idle_reason(self):
        for n in range(self.c.max_entry_attempts_per_day):
            key = self.store.intent(str(n), {'side': 'BUY', 'mode': 'paper-exchange'}, NOW+n)
            self.store.update_intent(key, 'rejected')
        self.warm()
        self.engine.step(quotes(), NOW+100)
        self.assertEqual(self.store.get('tick_last_decision')['reason'], 'daily_entry_attempt_limit')
        self.assertEqual(self.api.writes, [])

    def test_crossed_public_quote_does_not_fabricate_an_exchange_fill(self):
        s = self.state()
        self.engine.submit(s, 'BUY', 100/3.88e-6, 3.88e-6, NOW)
        self.engine.quotes = quotes(3.87e-6, 3.88e-6, 3.88e-6)
        self.engine.observe(s, NOW+30000)
        self.assertIsNone(self.engine.state()['position'])
        self.assertEqual(self.store.fills(), [])
        self.assertTrue(self.events('tick_order_status')[-1]['quote_crossed_but_pending'])

    def test_entire_confirmed_buy_sell_cycle_updates_real_counts_and_net_pnl(self):
        self.warm()
        self.engine.step(quotes(), NOW)
        buy = self.api.orders[0]
        self.api.now = NOW+30000
        self.api.fill(buy)
        self.engine.step(quotes(), NOW+30000)
        self.assertEqual(len(self.api.orders), 2)
        self.assertEqual(self.engine.state()['phase'], 'target')
        self.assertEqual(daily_counts(self.store.db, NOW, 'paper-exchange')['completed_cycles_today'], 0)
        sell = self.api.orders[1]
        self.api.now = NOW+60000
        self.api.fill(sell)
        self.engine.step(quotes(), NOW+60000)
        counts = daily_counts(self.store.db, NOW, 'paper-exchange')
        self.assertEqual(counts['filled_orders_today'], 2)
        self.assertEqual(counts['completed_cycles_today'], 1)
        self.assertIsNone(self.engine.state()['position'])
        self.assertAlmostEqual(self.store.get('tick_closed_samples')[0]['pnl'], self.api.wallet['USD']['Free']-100000)
        self.assertEqual(self.store.fills()[1]['role'], 'MAKER')

    def test_cancel_fill_race_does_not_submit_an_extra_sell(self):
        self.warm()
        self.engine.step(quotes(), NOW)
        self.api.now = NOW+30000
        self.api.fill(self.api.orders[0])
        self.engine.step(quotes(), NOW+30000)
        sell = self.api.orders[1]
        self.engine.step(quotes(3.80e-6, 3.81e-6, 3.81e-6), NOW+60000)
        self.assertEqual(self.api.writes[-1], ('CANCEL', sell['OrderID']))
        self.api.now = NOW+90000
        self.api.fill(sell)
        self.engine.step(quotes(), NOW+90000)
        self.assertEqual(len(self.api.orders), 2)
        self.assertFalse(any(kind == 'MARKET' for kind, _ in self.api.writes))
        self.assertEqual(daily_counts(self.store.db, NOW, 'paper-exchange')['completed_cycles_today'], 1)

    def test_sell_pnl_sample_rolls_back_with_fill_and_cash(self):
        e = TickEngine(self.c, self.api, self.store, META, 'shadow')
        e.quotes = quotes(3.87e-6, 3.88e-6, 3.88e-6)
        s = e.state(); s['pair'] = 'PEPE/USD'
        e.submit(s, 'BUY', 100/3.88e-6, 3.88e-6, NOW)
        e.observe(s, NOW+30000)
        s = e.state()
        e.submit(s, 'SELL', s['position']['qty'], 3.89e-6, NOW+60000)
        before, cash = e.state(), self.store.get('tick_cash')
        self.store.db.execute("CREATE TRIGGER reject_sell_fill BEFORE INSERT ON fills WHEN json_extract(NEW.payload,'$.side')='SELL' BEGIN SELECT RAISE(ABORT,'fixture rollback'); END")
        e.quotes = quotes(3.89e-6, 3.90e-6, 3.89e-6)
        with self.assertRaises(sqlite3.IntegrityError): e.observe(e.state(), NOW+90000)
        self.assertEqual(e.state(), before)
        self.assertEqual(self.store.get('tick_cash'), cash)
        self.assertEqual(self.store.get('tick_closed_samples', []), [])

    def test_small_residual_coin_dust_does_not_block_the_next_cycle(self):
        s = self.state(); self.engine.save(s)
        self.api.wallet['PEPE'] = {'Free': .25, 'Lock': 0}
        self.engine.ownership(self.api.wallet, s)
        self.api.wallet['PEPE']['Free'] = 10000000
        with self.assertRaises(ApiError): self.engine.ownership(self.api.wallet, s)

    def test_old_configuration_samples_cannot_trigger_scaling_after_a_change(self):
        for n in range(20): self.engine.close_sample('PEPE/USD', 1, 100, NOW+n, order_id=n)
        self.assertTrue(self.engine.probe_verified('PEPE/USD'))
        changed = TickEngine(replace(self.c, entry_behind_bid_ticks=1), self.api, self.store, META, 'paper-exchange')
        self.assertFalse(changed.probe_verified('PEPE/USD'))

    def test_daily_loss_halt_remains_latched_after_equity_recovers(self):
        day = daily_counts(self.store.db, NOW, 'paper-exchange')['hkt_day']
        self.store.set('tick_counters', {'day': day, 'start_equity': 100000, 'peak': 100000, 'halted': False})
        self.api.wallet['USD']['Free'] = 99000
        self.engine.step(quotes(), NOW)
        self.api.wallet['USD']['Free'] = 100000
        self.engine.step(quotes(), NOW+30000)
        self.assertEqual(self.store.get('tick_last_decision')['reason'], 'portfolio_halt_or_cutoff')
        self.assertTrue(self.store.get('tick_counters')['daily_halted'])
        self.engine.step(quotes(), NOW+86400000)
        self.assertFalse(self.store.get('tick_counters')['daily_halted'])

    def test_quote_outage_resets_entry_warmup_without_affecting_owned_positions(self):
        self.store.set('tick_quote_history', [{'t': NOW-300000-offset*30000, 'quotes': {'PEPE/USD': 3.89e-6}} for offset in range(10, 0, -1)])
        self.engine.step(quotes(), NOW)
        self.assertEqual(self.store.get('tick_last_decision')['reason'], 'warming_quote_history')
        self.assertEqual(len(self.store.get('tick_quote_history')), 1)

    def test_nan_and_infinity_quotes_are_rejected(self):
        for value in (float('nan'), float('inf')):
            with self.assertRaises(ApiError): validated_quote(quotes(value, float('inf')), 'PEPE/USD')

    def test_reports_contain_confirmed_fills_and_are_explicit_about_their_mode(self):
        self.engine.close_sample('PEPE/USD', .2, 100, NOW, order_id='example')
        self.store.db.execute('INSERT INTO fills VALUES(?,?,?)', ('example', NOW, json.dumps({'pair': 'PEPE/USD', 'side': 'SELL', 'quantity': 100, 'price': 3.89e-6, 'mode': 'paper-exchange', 'pnl_usd': .2})))
        self.store.db.commit()
        output = self.root/'reports'
        result = export_report(self.store.db, 'paper-exchange', output, NOW)
        self.assertEqual(result['total_recorded_filled_orders'], 1)
        self.assertIn('signed_roostoo_account', (output/'summary.txt').read_text())
        self.assertIn('+05:30', (output/'trade_logs.csv').read_text())
        self.assertEqual(json.loads((output/'status.json').read_text())['recorded_realized_pnl_usd'], .2)
        shadow = snapshot(self.store.db, 'shadow', NOW)
        self.assertEqual(shadow['total_recorded_filled_orders'], 0)
        self.assertEqual(shadow['total_recorded_completed_cycles'], 0)

    def test_local_status_uses_existing_ledger_without_initializing_or_modifying_state(self):
        config = self.root/'config.json'
        config.write_text(json.dumps(asdict(self.c)))
        before = list(self.store.db.execute('SELECT * FROM kv'))
        result = diagnose(str(config), 'paper-exchange')
        self.assertTrue(result['ledger_exists'])
        self.assertEqual(before, list(self.store.db.execute('SELECT * FROM kv')))

    def test_no_ledger_status_does_not_create_a_fake_account(self):
        config = self.root/'config.json'
        config.write_text(json.dumps(asdict(replace(self.c, runtime_dir=str(self.root/'absent')))))
        result = diagnose(str(config), 'paper-exchange')
        self.assertFalse(result['ledger_exists'])
        self.assertFalse((self.root/'absent').exists())

    def test_remote_diagnosis_performs_only_read_calls(self):
        config = self.root/'config.json'; config.write_text(json.dumps(asdict(self.c)))
        class ReadOnly:
            authenticated = True
            def __init__(self, **kwargs): pass
            def sync_time(self): return NOW
            def now_ms(self): return NOW
            def exchange_info(self): return META
            def ticker(self): return quotes()
            def balance(self): return {'USD': {'Free': 100000, 'Lock': 0}}
            def query_orders(self, **kwargs): return []
            def short_positions(self): return []
        with patch('sentinel.tick_report.Roostoo', ReadOnly):
            result = diagnose(str(config), 'paper-exchange', remote=True)
        self.assertEqual(result['remote']['writes_performed'], 0)


if __name__ == '__main__': unittest.main()
