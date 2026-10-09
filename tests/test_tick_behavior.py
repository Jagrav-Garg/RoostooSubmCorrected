import importlib.util
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest

import numpy as np

from sentinel.api import AmbiguousWrite, ApiError, Roostoo
from sentinel.state import Store
from sentinel.tick import TickConfig, TickEngine, acceptable_fill, net_cycle, shift_price


META={'TradePairs':{'PEPE/USD':{'PricePrecision':8,'AmountPrecision':0,'MiniOrder':1,'CanTrade':True}}}
QUOTES={'Data':{'PEPE/USD':{'MaxBid':.00000388,'MinAsk':.00000389,'LastPrice':.00000388,'UnitTradeValue':4e7}}}


class FakeApi:
    authenticated=True
    def __init__(self):
        self.w={'USD':{'Free':100000.,'Lock':0.}}
        self.orders=[];self.writes=0;self.unknown=False;self.cancel_writes=0
    def balance(self):return self.w
    def query_orders(self,order_id=None,**kwargs):
        return [o for o in self.orders if order_id is None or str(o['OrderID'])==str(order_id)]
    def place_limit(self,pair,side,quantity,price):
        self.writes+=1
        if self.unknown:raise AmbiguousWrite('timeout after request')
        o={'OrderID':11,'Pair':pair,'Side':side,'Type':'LIMIT','Quantity':float(quantity),'Price':float(price),'CreateTimestamp':1000,'Status':'PENDING','FilledQuantity':0,'FilledAverPrice':0,'Role':'MAKER','CommissionPercent':.0005}
        self.orders=[o]
        return {'Success':True,'OrderDetail':o}
    def cancel_order(self,oid):
        self.cancel_writes+=1
        return {'Success':True,'CanceledList':[oid]}


class TickTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.store=Store(Path(self.tmp.name)/'tick.db')
        self.api=FakeApi();self.c=TickConfig();self.e=TickEngine(self.c,self.api,self.store,META)
        self.e.quotes=QUOTES
    def tearDown(self):self.store.close();self.tmp.cleanup()
    def state(self):
        s=self.e.state();s['pair']='PEPE/USD';return s

    def test_one_tick_must_cover_both_fee_conventions(self):
        self.assertGreater(net_cycle(.00000388,.00000389,.0005),.0015)
        self.assertLess(net_cycle(83000,83000.01,.0005),0)
        self.assertAlmostEqual(net_cycle(1,1.002,.0005),1.002*(1-.0005)**2-1)

    def test_decimal_tick_shift_does_not_accidentally_add_two_ticks(self):
        for p in [.00000332,.00000388,.00000537]:
            self.assertEqual(shift_price(p,8,1),float(f'{p+.00000001:.8f}'))

    def test_limit_and_cancel_use_signed_single_mutations(self):
        class Capture(Roostoo):
            def _request(self,*a,**k):self.call=(a,k);return {}
        api=Capture();api.place_limit('PEPE/USD','BUY','10000000','0.00000387')
        self.assertEqual(api.call[0][2]['type'],'LIMIT')
        self.assertTrue(api.call[1]['mutation']);self.assertTrue(api.call[1]['signed'])
        api.cancel_order(7);self.assertEqual(api.call[0][2],{'order_id':'7'})
        with self.assertRaises(ValueError):api.cancel_order(None)

    def test_shadow_does_not_fill_on_submission_snapshot_or_last_only_touch(self):
        s=self.state();self.e.submit(s,'BUY',100/.00000388,.00000388,1000)
        self.e.observe(s,1000);self.assertIsNone(s['position'])
        self.e.observe(s,31000);self.assertIsNone(s['position'])
        q=json.loads(json.dumps(QUOTES));q['Data']['PEPE/USD']['MinAsk']=.00000388
        self.e.quotes=q;self.e.observe(s,61000)
        self.assertGreater(self.e.state()['position']['qty'],0)

    def test_shadow_fill_state_cash_and_journal_rollback_together(self):
        s=self.state();self.e.submit(s,'BUY',100/.00000388,.00000388,1000)
        before=self.e.state();cash=self.store.get('tick_cash')
        self.store.db.execute("CREATE TRIGGER prevent_fill BEFORE INSERT ON fills BEGIN SELECT RAISE(ABORT,'test'); END")
        q=json.loads(json.dumps(QUOTES));q['Data']['PEPE/USD']['MinAsk']=.00000388;self.e.quotes=q
        with self.assertRaises(sqlite3.IntegrityError):self.e.observe(s,31000)
        self.assertEqual(self.e.state(),before);self.assertEqual(self.store.get('tick_cash'),cash)

    def test_unknown_limit_is_not_resent_after_restart(self):
        self.api.unknown=True;e=TickEngine(self.c,self.api,self.store,META,'paper-exchange');s=self.state()
        self.assertFalse(e.submit(s,'BUY',100/.00000388,.00000388,1000))
        restored=TickEngine(self.c,self.api,self.store,META,'paper-exchange')
        self.assertFalse(restored.resolve_writes(s));self.assertFalse(restored.submit(s,'BUY',100/.00000388,.00000388,4000))
        self.assertEqual(self.api.writes,1);self.assertEqual(self.store.unfinished()[0]['status'],'unknown')

    def test_unique_limit_history_recovers_without_resending(self):
        self.api.unknown=True;e=TickEngine(self.c,self.api,self.store,META,'paper-exchange');s=self.state()
        e.submit(s,'BUY',100/.00000388,.00000388,1000)
        qty=float(self.store.unfinished()[0]['payload']['quantity'])
        self.api.orders=[{'OrderID':9,'Pair':'PEPE/USD','Side':'BUY','Type':'LIMIT','Quantity':qty,'Price':.00000388,'CreateTimestamp':1000,'Status':'PENDING'}]
        self.assertTrue(e.resolve_writes(s));self.assertEqual(self.e.state()['order']['id'],9);self.assertEqual(self.api.writes,1)

    def test_ambiguous_two_matching_limits_remain_blocked(self):
        self.api.unknown=True;e=TickEngine(self.c,self.api,self.store,META,'paper-exchange');s=self.state()
        e.submit(s,'BUY',100/.00000388,.00000388,1000)
        qty=float(self.store.unfinished()[0]['payload']['quantity'])
        o={'Pair':'PEPE/USD','Side':'BUY','Type':'LIMIT','Quantity':qty,'Price':.00000388,'CreateTimestamp':1000,'Status':'PENDING'}
        self.api.orders=[{**o,'OrderID':8},{**o,'OrderID':9}]
        self.assertFalse(e.resolve_writes(s));self.assertEqual(self.api.writes,1)

    def test_cancel_ack_is_not_proof_of_cancellation(self):
        e=TickEngine(self.c,self.api,self.store,META,'paper-exchange');s=self.state()
        e.submit(s,'BUY',100/.00000388,.00000388,1000)
        self.assertFalse(e.cancel(s,31000))
        self.assertEqual(len(self.store.unfinished()),1);self.assertIsNotNone(self.e.state()['order'])
        self.assertFalse(e.submit(s,'SELL',100/.00000388,.00000389,32000));self.assertEqual(self.api.writes,1)
        self.api.orders[0]['Status']='CANCELED';e.resolve_writes(s);e.quotes=QUOTES;e.observe(s,61000)
        self.assertIsNone(self.e.state()['order']);self.assertEqual(self.store.fills(),[])

    def test_partial_execution_is_valid_but_contradictory_fields_pause(self):
        self.assertEqual(acceptable_fill({'Quantity':100,'FilledQuantity':30,'FilledAverPrice':1,'Status':'PENDING'},0),(30,1))
        with self.assertRaises(ApiError):acceptable_fill({'Quantity':100,'FilledQuantity':30,'FilledAverPrice':0,'Status':'PENDING'},0)
        with self.assertRaises(ApiError):acceptable_fill({'Quantity':100,'FilledQuantity':130,'FilledAverPrice':1,'Status':'FILLED'},0)

    def test_actual_coin_fee_is_reconciled_from_account_not_requested_quantity(self):
        e=TickEngine(self.c,self.api,self.store,META,'paper-exchange');e.quotes=QUOTES;s=self.state()
        e.submit(s,'BUY',100/.00000388,.00000388,1000)
        qty=self.api.orders[0]['Quantity'];net_qty=float(int(qty*(1-.0005)))
        self.api.orders[0].update(Status='FILLED',FilledQuantity=qty,FilledAverPrice=.00000388,FinishTimestamp=2000,CommissionCoin='PEPE')
        self.api.w={'USD':{'Free':100000-qty*.00000388,'Lock':0.},'PEPE':{'Free':net_qty,'Lock':0.}}
        e.observe(s,31000);pos=e.state()['position']
        self.assertEqual(pos['qty'],net_qty);self.assertLess(pos['qty'],qty)
        self.assertAlmostEqual(pos['cost'],qty*.00000388)
        self.assertIsNone(e.state()['order']);self.assertEqual(len(self.store.fills()),1)

    def test_documented_pending_quantity_echo_requires_unchanged_account(self):
        e=TickEngine(self.c,self.api,self.store,META,'paper-exchange');e.quotes=QUOTES;s=self.state()
        e.submit(s,'BUY',100/.00000388,.00000388,1000)
        self.api.orders[0].update(FilledQuantity=self.api.orders[0]['Quantity'],FilledAverPrice=0,CoinChange=0,UnitChange=0,CommissionChargeValue=0)
        e.observe(s,31000)
        self.assertIsNone(e.state()['position']);self.assertIsNotNone(e.state()['order']);self.assertEqual(self.store.fills(),[])
        self.api.w['PEPE']={'Free':10,'Lock':0}
        with self.assertRaises(ApiError):e.observe(s,61000)

    def test_unexpected_taker_role_blocks_additional_entries(self):
        e=TickEngine(self.c,self.api,self.store,META,'paper-exchange');e.quotes=QUOTES;s=self.state()
        e.submit(s,'BUY',100/.00000388,.00000388,1000)
        qty=self.api.orders[0]['Quantity']
        self.api.orders[0].update(Status='FILLED',FilledQuantity=qty,FilledAverPrice=.00000388,FinishTimestamp=2000,Role='TAKER',CommissionPercent=.001)
        self.api.w={'USD':{'Free':100000-qty*.00000388*1.001,'Lock':0.},'PEPE':{'Free':qty,'Lock':0.}}
        e.observe(s,31000)
        self.assertEqual(self.store.get('tick_fee_violation')['role'],'TAKER')

    def test_probe_requires_positive_net_samples_and_cannot_count_duplicates(self):
        for i in range(19):self.e.close_sample('PEPE/USD',1,100,1000+i,order_id=i)
        self.assertFalse(self.e.probe_verified('PEPE/USD'))
        self.e.close_sample('PEPE/USD',1,100,2000,order_id=18)
        self.assertEqual(len(self.store.get('tick_closed_samples')),19)
        self.e.close_sample('PEPE/USD',1,100,2001,order_id=19)
        self.assertTrue(self.e.probe_verified('PEPE/USD'))
        self.assertFalse(self.e.probe_verified('BONK/USD'))
        self.store.set('tick_closed_samples',[{'pair':'PEPE/USD','return':-.001}]*20)
        self.assertFalse(self.e.probe_verified('PEPE/USD'))

    def test_minute_simulation_has_correct_risk_size_and_no_same_bar_target(self):
        path=Path(__file__).parents[1]/'research/evaluate_tick_cycles.py'
        spec=importlib.util.spec_from_file_location('cycle_test_module',path);m=importlib.util.module_from_spec(spec);sys.modules[spec.name]=m;spec.loader.exec_module(m)
        n=120;a={'timestamp':np.arange(n)*60000,'open':np.full(n,.00000301),'high':np.full(n,.00000301),'low':np.full(n,.000003),'close':np.full(n,.00000301),'trades':np.ones(n),'warm':np.ones(n,dtype=bool),'v24':np.full(n,4e7),'ret30':np.zeros(n),'ret60':np.zeros(n),'ema60':np.full(n,.00000301),'range30':np.full(n,.00000001),'volume':np.ones(n),'taker_base':np.ones(n)}
        result,tr=m.one_coin(a,'PEPE',1e-8,m.Spec('test'),0,n*60000,True)
        self.assertGreater(len(tr),0)
        first=tr[0];notional=first['entry']*first['quantity']
        self.assertGreater(notional,1000);self.assertLessEqual(notional,15000)
        loss=-m.cycle_return(first['entry'],first['entry']-4e-8,m.Spec('test'),False)
        self.assertLessEqual(notional*loss,150.0001)
        self.assertGreaterEqual(first['exit_ts'],first['entry_ts']+60000)


if __name__=='__main__':unittest.main()
