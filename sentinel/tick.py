"""Sequential fee-aware limit cycles; the remote venue is a mock exchange.

No invented fill is recorded as a competition trade. Shadow mode is a hypothesis
test; paper-exchange mode relies exclusively on signed order/account responses.
"""
from dataclasses import asdict, dataclass
from decimal import Decimal, ROUND_CEILING, ROUND_FLOOR
from pathlib import Path
import hashlib
import json
import math
import time

from .api import AmbiguousWrite, ApiError, Roostoo, decimal_string
from .execution import validated_quote, wallet_qty
from .runner import InstanceLock, code_version
from .state import Store


@dataclass(frozen=True)
class TickConfig:
    pairs: tuple = ("PEPE/USD",)
    maker_fee: float = .0005
    taker_fee: float = .001
    min_cycle_net_bps: float = 6.
    target_ticks: int = 1
    entry_behind_bid_ticks: int = 0
    max_spread_ticks: int = 2
    stop_ticks: int = 3
    entry_ttl_seconds: int = 600
    max_hold_seconds: int = 1800
    cooldown_seconds: int = 180
    min_entry_interval_seconds: int = 300
    poll_seconds: int = 30
    max_cycles_per_day: int = 24
    max_entry_attempts_per_day: int = 48
    rejection_backoff_seconds: int = 300
    report_interval_seconds: int = 60
    allocation_cap: float = .15
    planned_risk: float = .0015
    daily_loss: float = .0075
    max_drawdown: float = .025
    min_daily_volume: float = 2_000_000.
    minimum_notional: float = 10.
    shadow_initial_cash: float = 100000.
    shadow_fill_reference: str = "quotes"
    probe_notional: float = 100.
    probe_cycles: int = 20
    probe_min_win_rate: float = .80
    runtime_dir: str = "runtime"
    competition_start: str = "2026-10-04T00:00:00+08:00"
    competition_end: str = "2026-10-18T00:00:00+08:00"
    tick_permission_basis: str = "User clarification: sequential tick scalping is permitted."

    @classmethod
    def load(cls, path="tick_config.json"):
        d=json.loads(Path(path).read_text())
        if 'pairs' in d:d['pairs']=tuple(d['pairs'])
        c=cls(**d)
        if any(not math.isfinite(float(v)) for v in asdict(c).values() if isinstance(v,(int,float))):
            raise ValueError("Configuration contains non-finite numbers")
        if c.poll_seconds<30 or c.target_ticks<1 or c.stop_ticks<1 or c.entry_behind_bid_ticks<0 or c.max_spread_ticks<1:
            raise ValueError("Invalid tick distances or excessive request cadence")
        if not 0<c.planned_risk<c.daily_loss<=c.max_drawdown<1 or not 0<c.allocation_cap<=1:
            raise ValueError("Invalid risk budget")
        if not 0<c.maker_fee<=c.taker_fee<.1 or c.min_cycle_net_bps<0:
            raise ValueError("Invalid fee assumptions")
        if not c.pairs or len(set(c.pairs))!=len(c.pairs) or any(not p.endswith('/USD') for p in c.pairs):
            raise ValueError("Invalid pair whitelist")
        if c.shadow_fill_reference not in {'quotes','last'} or not c.tick_permission_basis:
            raise ValueError("Invalid fill reference or missing strategy permission basis")
        if c.probe_notional<c.minimum_notional*2 or c.probe_cycles<20 or not .5<=c.probe_min_win_rate<=1:
            raise ValueError("Insufficient initial fill verification")
        if min(c.entry_ttl_seconds,c.max_hold_seconds,c.cooldown_seconds,c.min_entry_interval_seconds,c.rejection_backoff_seconds,c.report_interval_seconds,c.max_cycles_per_day,c.max_entry_attempts_per_day)<=0:
            raise ValueError("Durations and daily limits must be positive")
        if any(type(v) is not int for v in (c.target_ticks,c.stop_ticks,c.entry_behind_bid_ticks,c.max_spread_ticks,c.max_cycles_per_day,c.max_entry_attempts_per_day,c.probe_cycles)):
            raise ValueError("Tick distances and counts must be integers")
        if c.min_daily_volume<0 or c.minimum_notional<=0 or c.shadow_initial_cash<=0:
            raise ValueError("Invalid liquidity or notional limits")
        return c

    @property
    def fingerprint(self):
        return hashlib.sha256(json.dumps(asdict(self),sort_keys=True).encode()).hexdigest()


def tick_price(value, precision, upward=False):
    step=Decimal(1).scaleb(-precision)
    return float(Decimal(str(value)).quantize(step,rounding=ROUND_CEILING if upward else ROUND_FLOOR))


def shift_price(value,precision,ticks,upward=False):
    step=Decimal(1).scaleb(-precision)
    base=Decimal(str(value)).quantize(step,rounding=ROUND_CEILING if upward else ROUND_FLOOR)
    return float(base+ticks*step)


def net_cycle(entry, target, maker_fee):
    # Less favorable of cash/base entry commission conventions, before rounding.
    return min(target*(1-maker_fee)/(entry*(1+maker_fee))-1,
               target*(1-maker_fee)**2/entry-1)


def acceptable_fill(order, quantity_precision):
    """Reject contradictory cumulative execution fields; support actual partial fills."""
    qty=float(order.get('FilledQuantity',0) or 0)
    avg=float(order.get('FilledAverPrice',0) or 0)
    requested=float(order.get('Quantity',0) or 0)
    if not all(math.isfinite(x) for x in (qty,avg,requested)) or qty<0 or qty>requested+10**(-quantity_precision) or (qty>0 and avg<=0):
        raise ApiError("Contradictory execution fields; require read-only investigation")
    if order.get('Status')=='FILLED' and qty<=0:
        raise ApiError("Filled order reports no actual execution")
    return qty,avg


class TickEngine:
    def __init__(self, cfg, api, store, metadata, mode='shadow'):
        if mode not in {'shadow','paper-exchange'}:raise ValueError("Unknown tick mode")
        self.c,self.api,self.store,self.meta,self.mode=cfg,api,store,metadata,mode
        if mode=='paper-exchange' and not api.authenticated:
            raise ApiError("Paper exchange requires private competition credentials")
        if mode=='shadow' and store.get('tick_cash') is None:store.set('tick_cash',cfg.shadow_initial_cash)
        self.version=code_version()

    def state(self):return self.store.get('tick_state',{'phase':'idle','position':None,'order':None,'last_entry':0,'cooldown':0})
    def save(self,s):self.store.set('tick_state',s)

    def commit(self,s,key=None,status=None,response=None,updates=None,fill=None,sample=None):
        """One SQLite transaction for state, cash, intent and terminal fill."""
        with self.store.db:
            changes={'tick_state':s,**(updates or {})}
            if sample is not None:
                rows=self.store.get('tick_closed_samples',[])
                if not any(str(r.get('order_id'))==str(sample.get('order_id')) for r in rows):
                    changes['tick_closed_samples']=[*rows,sample]
            for k,v in changes.items():
                self.store.db.execute('INSERT INTO kv VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value',(k,json.dumps(v,allow_nan=False)))
            if key is not None:
                self.store.db.execute('UPDATE intents SET status=?,response=? WHERE id=?',(status,json.dumps(response) if response is not None else None,key))
            if fill is not None:
                self.store.db.execute('INSERT OR IGNORE INTO fills VALUES(?,?,?)',fill)

    def stamp(self):
        return {'mode':self.mode,'config_hash':self.c.fingerprint,'commit':self.version}

    def close_sample(self,pair,pnl,cost,now,**detail):
        sample=self.sample(pair,pnl,cost,now,**detail)
        rows=self.store.get('tick_closed_samples',[])
        # Keyed execution observations cannot be duplicated after a restart.
        identity=detail.get('order_id')
        if identity is None or not any(str(r.get('order_id'))==str(identity) for r in rows):
            rows.append(sample)
        self.store.set('tick_closed_samples',rows)
        self.store.event('tick_cycle_closed',{**sample,**self.stamp()},now)

    def sample(self,pair,pnl,cost,now,**detail):
        return {'pair':pair,'pnl':pnl,'cost':cost,'return':pnl/cost if cost>0 else 0.,'timestamp_ms':now,'cycle_closed':True,**self.stamp(),**detail}

    def decision(self,now,reason,**detail):
        payload={'reason':reason,**detail,**self.stamp()}
        self.store.set('tick_last_decision',{'timestamp_ms':now,**payload})
        self.store.event('tick_decision',payload,now)

    def daily_counts(self,now):
        from .tick_report import daily_counts
        return daily_counts(self.store.db,now,self.mode)

    def probe_verified(self,pair):
        import numpy as np
        rows=[r['return'] for r in self.store.get('tick_closed_samples',[]) if r['pair']==pair and r.get('cycle_closed',True) and r.get('config_hash')==self.c.fingerprint and r.get('mode')==self.mode]
        if len(rows)<self.c.probe_cycles:return False
        x=np.array(rows[-60:]);rng=np.random.default_rng(918)
        means=x[rng.integers(0,len(x),(600,len(x)))].mean(axis=1)
        # Descriptive verification of this account's net fills, not a future guarantee.
        return float((x>0).mean())>=self.c.probe_min_win_rate and float(np.quantile(means,.10))>0

    def ownership(self,wallet,s):
        if self.mode=='shadow':return
        for coin,v in wallet.items():
            if coin=='USD':continue
            qty=float(v.get('Free',0))+float(v.get('Lock',0))
            if qty<=0:continue
            pair=coin+'/USD'
            q=self.quotes['Data'].get(pair)
            if not q:raise ApiError("Unmapped holding; account handover required")
            if qty*float(q['MaxBid'])>=self.c.minimum_notional and pair!=s.get('pair'):
                raise ApiError("Unmanaged holdings; preserve existing bot state")
            if qty*float(q['MaxBid'])>=self.c.minimum_notional and pair==s.get('pair') and not s.get('position') and not s.get('order') and not self.store.unfinished():
                raise ApiError("Holding has no matching position or order journal")
        pending=self.api.query_orders(pending_only=True)
        if len(pending)>=100:raise ApiError("Pending-order inventory exceeds verification budget")
        owned={str(s['order']['id'])} if s.get('order') else set()
        owned.update(str(r['response'].get('OrderDetail',{}).get('OrderID')) for r in self.store.unfinished() if r.get('response'))
        if any(str(o.get('OrderID')) not in owned for o in pending):
            raise ApiError("Unmanaged pending order; no new writes")

    def wallet(self):
        if self.mode=='paper-exchange':return self.api.balance()
        s=self.state();w={'USD':{'Free':float(self.store.get('tick_cash')),'Lock':0.}}
        if s.get('position'):w[s['pair'].split('/')[0]]={'Free':s['position']['qty'],'Lock':0.}
        return w

    def submit(self,s,side,quantity,price,now,kind='LIMIT'):
        if self.store.unfinished() or s.get('order'):return False
        m=self.meta['TradePairs'][s['pair']]
        qty=decimal_string(quantity,int(m['AmountPrecision']))
        px=decimal_string(price,int(m['PricePrecision']))
        if not math.isfinite(float(qty)) or not math.isfinite(float(px)) or float(qty)<=0 or float(px)<=0 or float(qty)*float(px)<=max(float(m['MiniOrder']),self.c.minimum_notional):
            self.decision(now,'order_below_minimum_notional',pair=s['pair'],side=side,quantity=qty,price=px)
            return False
        w=self.wallet()
        if side=='BUY' and float(qty)*float(px)*(1+self.c.maker_fee)>float(w.get('USD',{}).get('Free',0))+1e-8:
            self.decision(now,'insufficient_free_usd',pair=s['pair']);return False
        payload={**self.stamp(),'pair':s['pair'],'side':side,'quantity':qty,'price':px,'type':kind,'before_qty':wallet_qty(w,s['pair']),'before_usd':float(w.get('USD',{}).get('Free',0))+float(w.get('USD',{}).get('Lock',0)),'request_timestamp':now}
        key=self.store.intent(f"{self.mode}|tick|{side}|{s['pair']}|{now}",payload,now)
        if key is None:return False
        if side=='BUY':s['last_attempt']=now;self.save(s)
        self.store.event('tick_order_intent',{'intent':key,**payload},now)
        if self.mode=='shadow':
            order={'id':key,'intent':key,'side':side,'quantity':float(qty),'price':float(px),'type':kind,'created':now,'before_qty':payload['before_qty'],'before_usd':payload['before_usd']}
            s['order']=order;s['phase']='entry' if side=='BUY' else ('target' if kind=='LIMIT' else 'exit')
            self.commit(s,key,'resting',{'OrderDetail':{'OrderID':key}})
            return True
        try:
            r=self.api.place_limit(s['pair'],side,qty,px) if kind=='LIMIT' else self.api.place(s['pair'],side,qty)
        except AmbiguousWrite as e:
            self.store.update_intent(key,'unknown');self.store.event('tick_write_unknown',{'intent':key,'error':str(e)},now)
            self.decision(now,'order_submission_unknown',intent=key)
            return False
        except ApiError as e:
            self.store.update_intent(key,'rejected',{'error':str(e)})
            s['cooldown']=now+self.c.rejection_backoff_seconds*1000;self.save(s)
            self.store.event('tick_order_rejected',{'intent':key,'pair':s['pair'],'side':side,'quantity':qty,'price':px,'error':str(e),**self.stamp()},now)
            self.decision(now,'order_rejected',pair=s['pair'],error=str(e))
            return False
        self.store.update_intent(key,'acknowledged',r)
        return self.resolve_writes(s)

    def resolve_writes(self,s):
        for row in self.store.unfinished():
            p=row['payload']
            if p.get('action')=='cancel':
                orders=self.api.query_orders(order_id=p['order_id'])
                if len(orders)!=1:continue
                if orders[0].get('Status') in {'CANCELED','CANCELLED','FILLED','REJECTED'}:
                    self.store.update_intent(row['id'],'done',orders[0])
                continue
            detail=(row['response'] or {}).get('OrderDetail',{})
            oid=detail.get('OrderID')
            if oid is None:
                orders=[]
                for page in range(10):
                    chunk=self.api.query_orders(limit=100,offset=page*100);orders.extend(chunk)
                    if len(chunk)<100:break
                orders=[o for o in orders if o.get('Pair')==p['pair'] and o.get('Side')==p['side'] and o.get('Type')==p['type'] and abs(int(o.get('CreateTimestamp',0))-p['request_timestamp'])<=60000 and abs(float(o.get('Quantity',0))-float(p['quantity']))<10**(-self.meta['TradePairs'][p['pair']]['AmountPrecision'])/2]
                if p['type']=='LIMIT':orders=[o for o in orders if abs(float(o.get('Price',0))-float(p['price']))<10**(-self.meta['TradePairs'][p['pair']]['PricePrecision'])/2]
                if len(orders)!=1:continue
                detail=orders[0];oid=detail['OrderID']
            order={'id':oid,'intent':row['id'],'side':p['side'],'quantity':float(p['quantity']),'price':float(p['price']),'type':p['type'],'created':p['request_timestamp'],'before_qty':p['before_qty'],'before_usd':p['before_usd']}
            s['pair']=p['pair'];s['order']=order;s['phase']='entry' if p['side']=='BUY' else ('target' if p['type']=='LIMIT' else 'exit')
            # Store state before clearing the durable unresolved-write gate.
            self.commit(s,row['id'],'resting',{'OrderDetail':detail})
            self.store.event('tick_order_accepted',{'order_id':oid,'pair':p['pair'],'side':p['side'],'price':p['price'],'quantity':p['quantity'],'status':detail.get('Status'),'role':detail.get('Role'),**self.stamp()},p['request_timestamp'])
        return not self.store.unfinished()

    def cancel(self,s,now,reason='entry_expired'):
        if self.store.unfinished() or not s.get('order'):return False
        o=s['order']
        self.store.event('tick_cancel_requested',{'order_id':o['id'],'pair':s['pair'],'reason':reason,**self.stamp()},now)
        if self.mode=='shadow':
            self.record_terminal(s,o,now,0,0,filled=0,terminal={'OrderID':o['id'],'Status':'CANCELED','FilledQuantity':0})
            return True
        key=self.store.intent(f"{self.mode}|cancel|{o['id']}|{now}",{'action':'cancel','order_id':o['id'],'request_timestamp':now,**self.stamp()},now)
        if key is None:return False
        try:r=self.api.cancel_order(o['id'])
        except AmbiguousWrite:
            self.store.update_intent(key,'unknown');return False
        except ApiError as e:
            self.store.update_intent(key,'rejected',{'error':str(e)})
            self.store.event('tick_cancel_rejected',{'order_id':o['id'],'error':str(e),**self.stamp()},now)
            # An order may have filled during cancellation; query it before any replacement.
            return False
        self.store.update_intent(key,'acknowledged',r)
        self.resolve_writes(s)
        return False  # A cancel acknowledgment is not proof of a free holding.

    def observe(self,s,now):
        o=s.get('order')
        if not o:return
        pair=s['pair'];q=self.quotes['Data'][pair]
        if self.mode=='shadow':
            if now<=o['created']:return
            ref=float(q['LastPrice']) if self.c.shadow_fill_reference=='last' else float(q['MinAsk'] if o['side']=='BUY' else q['MaxBid'])
            if o['type']=='LIMIT' and not (ref<=o['price'] if o['side']=='BUY' else ref>=o['price']):return
            px=o['price'] if o['type']=='LIMIT' else float(q['MaxBid'])
            fee=self.c.maker_fee if o['type']=='LIMIT' else self.c.taker_fee
            cash=float(self.store.get('tick_cash'))
            sample=None
            if o['side']=='BUY':
                s['position']={'entry':px,'qty':o['quantity'],'entered':now,'cost':o['quantity']*px*(1+fee)}
                cash-=s['position']['cost']
            else:
                pos=s['position'];sold=min(o['quantity'],pos['qty']);cash+=sold*px*(1-fee)
                pnl=sold*px*(1-fee)-pos['cost']*(sold/pos['qty'])
                remain=pos['qty']-sold
                sample=self.sample(pair,pnl,pos['cost']*(sold/pos['qty']),now,order_id=o['id'],exit_type=o['type'],cycle_closed=remain*float(q['MaxBid'])<self.c.minimum_notional)
                s['position']=None if remain*float(q['MaxBid'])<self.c.minimum_notional else {**pos,'qty':remain,'cost':pos['cost']*remain/pos['qty']}
            self.record_terminal(s,o,now,px,fee,updates={'tick_cash':cash},sample=sample,terminal={'Status':'FILLED','Role':'MAKER' if o['type']=='LIMIT' else 'TAKER','FilledQuantity':o['quantity']})
            return
        rows=self.api.query_orders(order_id=o['id'])
        if len(rows)!=1:raise ApiError("Owned order disappeared; keep its intent")
        row=rows[0];precision=self.meta['TradePairs'][pair]['AmountPrecision']
        if float(row.get('FilledQuantity',0) or 0)>0 and float(row.get('FilledAverPrice',0) or 0)<=0:
            # The official pending example echoes requested size into FilledQuantity.
            # Normalize only with explicit zero execution fields AND unchanged account.
            zero_fields=all(k in row and float(row[k] or 0)==0 for k in ('CoinChange','UnitChange','CommissionChargeValue'))
            if row.get('Status') in {'PENDING','CANCELED','CANCELLED'} and zero_fields:
                w=self.wallet();usd=float(w.get('USD',{}).get('Free',0))+float(w.get('USD',{}).get('Lock',0))
                if abs(wallet_qty(w,pair)-o['before_qty'])<10**(-precision)/2 and abs(usd-o['before_usd'])<1e-6:
                    row={**row,'FilledQuantity':0}
        filled,avg=acceptable_fill(row,precision)
        bid,ask=validated_quote(self.quotes,pair)
        tick=10**(-self.meta['TradePairs'][pair]['PricePrecision'])
        distance=(ask-o['price'])/tick if o['side']=='BUY' else (o['price']-bid)/tick
        self.store.event('tick_order_status',{'order_id':o['id'],'pair':pair,'side':o['side'],'status':row.get('Status'),'role':row.get('Role'),'limit_price':o['price'],'filled_quantity':filled,'average_fill_price':avg,'age_seconds':max(0,(now-o['created'])/1000),'bid':bid,'ask':ask,'last':q.get('LastPrice'),'distance_to_executable_quote_ticks':round(distance,6),'quote_crossed_but_pending':o['type']=='LIMIT' and distance<=0 and row.get('Status')=='PENDING',**self.stamp()},now)
        if filled and o['side']=='BUY':
            # Own partial inventory immediately, then cancel the unfilled remainder.
            w=self.wallet();qty=wallet_qty(w,pair)-o['before_qty']
            if qty<0:raise ApiError("Account contradicts buy fill")
            unit=float(w.get('USD',{}).get('Free',0))+float(w.get('USD',{}).get('Lock',0))
            cost=o['before_usd']-unit
            if cost<=0 or abs(qty-filled)>max(filled*.002,2*10**(-precision)):
                raise ApiError("Buy/account fee reconciliation mismatch")
            entered=(s.get('position') or {}).get('entered',int(row.get('FinishTimestamp') or now))
            s['position']={'entry':avg,'qty':qty,'entered':entered,'cost':cost,'dust_base':o['before_qty']};self.save(s)
        if row.get('Status') not in {'FILLED','CANCELED','CANCELLED','REJECTED'}:
            if filled and o['side']=='BUY':self.cancel(s,now,'partial_entry')
            return
        w=self.wallet()
        sample=None
        if o['side']=='SELL' and filled:
            pos=s['position'];remaining=max(0.,wallet_qty(w,pair)-pos.get('dust_base',0.))
            if remaining>pos['qty']+10**(-precision):raise ApiError("Sell/account reconciliation mismatch")
            usd=float(w.get('USD',{}).get('Free',0))+float(w.get('USD',{}).get('Lock',0))
            received=usd-o['before_usd'];sold=pos['qty']-remaining
            pnl=received-pos['cost']*sold/pos['qty']
            if received<=0 or sold<=0:raise ApiError("Sell/account proceeds reconciliation mismatch")
            sample=self.sample(pair,pnl,pos['cost']*sold/pos['qty'],now,order_id=o['id'],cycle_closed=remaining*float(q['MaxBid'])<self.c.minimum_notional)
            s['position']=None if remaining*float(q['MaxBid'])<self.c.minimum_notional else {**pos,'qty':remaining,'cost':pos['cost']*remaining/pos['qty']}
        fee=float(row.get('CommissionPercent',0))
        if filled and o['type']=='LIMIT' and (row.get('Role')!='MAKER' or fee>self.c.maker_fee+1e-9):
            self.store.set('tick_fee_violation',{'order_id':o['id'],'role':row.get('Role'),'fee':fee})
        self.record_terminal(s,o,now,avg,fee,filled,sample=sample,terminal=row)

    def record_terminal(self,s,o,now,px,fee,filled=None,updates=None,sample=None,terminal=None):
        fill_qty=o['quantity'] if filled is None else filled
        s['order']=None;s['phase']='held' if s.get('position') else 'idle'
        s['cooldown']=now+self.c.cooldown_seconds*1000
        if o['side']=='BUY' and fill_qty:s['last_entry']=now
        detail=terminal or {'Status':'FILLED' if fill_qty else 'CANCELED'}
        payload={'pair':s['pair'],'side':o['side'],'quantity':fill_qty,'price':px,'fee_rate':fee,'order_id':o['id'],'role':detail.get('Role'),'status':detail.get('Status'),'commission_coin':detail.get('CommissionCoin'),'commission_amount':detail.get('CommissionChargeValue'),'pnl_usd':sample['pnl'] if sample else None,**self.stamp()}
        fill=(str(o['id']),now,json.dumps(payload,allow_nan=False)) if fill_qty else None
        self.commit(s,o['intent'],'done',response={'OrderDetail':detail},updates=updates,fill=fill,sample=sample)
        self.store.event('tick_order_terminal',payload,now)
        if sample is not None:self.store.event('tick_cycle_closed',sample,now)

    def step(self,quotes,now):
        import pandas as pd
        self.quotes=quotes;s=self.state()
        if self.mode=='paper-exchange':self.resolve_writes(s)
        if self.store.unfinished():
            self.decision(now,'unresolved_remote_write')
            self.store.event('tick_paused',{'reason':'unresolved_remote_write'},now);return
        self.observe(s,now);s=self.state()
        w=self.wallet();self.ownership(w,s)
        free_cash=float(w.get('USD',{}).get('Free',0))
        cash=free_cash+float(w.get('USD',{}).get('Lock',0))
        pos=s.get('position')
        equity=cash
        for coin,amount in w.items():
            if coin!='USD' and float(amount.get('Free',0))+float(amount.get('Lock',0))>0:
                bid,_=validated_quote(quotes,coin+'/USD')
                equity+=(float(amount.get('Free',0))+float(amount.get('Lock',0)))*bid*(1-self.c.taker_fee)
        day=pd.to_datetime(now,unit='ms',utc=True).tz_convert('Asia/Hong_Kong').date().isoformat()
        counters=self.store.get('tick_counters',{})
        if counters.get('day')!=day:counters={'day':day,'start_equity':equity,'peak':max(equity,float(counters.get('peak',equity))),'halted':counters.get('halted',False),'daily_halted':False}
        counters['peak']=max(equity,counters['peak'])
        breach=equity<counters['peak']*(1-self.c.max_drawdown) or equity<counters['start_equity']*(1-self.c.daily_loss)
        if equity<counters['peak']*(1-self.c.max_drawdown):counters['halted']=True
        if equity<counters['start_equity']*(1-self.c.daily_loss):counters['daily_halted']=True
        self.store.set('tick_counters',counters)
        end=int(pd.Timestamp(self.c.competition_end).timestamp()*1000)
        start=int(pd.Timestamp(self.c.competition_start).timestamp()*1000)
        flatten=now>=end-300000 or breach or counters['halted'] or counters.get('daily_halted',False)
        counts=self.daily_counts(now)
        portfolio={'timestamp_ms':now,'equity':equity,'free_usd':free_cash,'locked_usd':cash-free_cash,'phase':s['phase'],'pair':s.get('pair'),**counts,**self.stamp()}
        self.store.set('tick_last_snapshot',portfolio)
        self.store.event('tick_portfolio',portfolio,now)
        history=self.store.get('tick_quote_history',[])
        if history and now-history[-1]['t']>max(90000,self.c.poll_seconds*3000):history=[]
        history.append({'t':now,'quotes':{p:float(quotes['Data'][p]['LastPrice']) for p in self.c.pairs}})
        history=[r for r in history if 0<=now-r['t']<=3600000]
        if any(not math.isfinite(value) or value<=0 for r in history for value in r['quotes'].values()):
            raise ApiError('Invalid last-price history')
        self.store.set('tick_quote_history',history)
        if pos:
            precision=self.meta['TradePairs'][s['pair']]['PricePrecision'];tick=10**(-precision)
            bid,ask=validated_quote(quotes,s['pair'])
            urgent=flatten or bid<=pos['entry']-self.c.stop_ticks*tick or now-pos['entered']>=self.c.max_hold_seconds*1000
            if urgent and s.get('order'):
                self.decision(now,'cancel_exit_before_urgent_liquidation',pair=s['pair'])
                self.cancel(s,now,'risk_or_time_exit');return
            if not s.get('order'):
                if urgent:
                    self.decision(now,'urgent_exit',pair=s['pair'])
                    self.submit(s,'SELL',min(pos['qty'],float(w[s['pair'].split('/')[0]].get('Free',0))),bid,now,'MARKET')
                else:
                    fee_aware=pos['cost']/(pos['qty']*(1-self.c.maker_fee))*(1+self.c.min_cycle_net_bps/10000)
                    target=max(shift_price(pos['entry'],precision,self.c.target_ticks,True),tick_price(fee_aware,precision,True))
                    # Never submit a marketable target and silently assume a maker fee.
                    target=max(target,shift_price(bid,precision,1))
                    self.decision(now,'placing_profit_target',pair=s['pair'],target=target)
                    self.submit(s,'SELL',min(pos['qty'],float(w[s['pair'].split('/')[0]].get('Free',0))),target,now)
            else:self.decision(now,'waiting_for_entry_remainder_cancellation' if s['order']['side']=='BUY' else 'waiting_for_sell_fill',pair=s['pair'],order_id=s['order']['id'])
            return
        if s.get('order'):
            if flatten or now-s['order']['created']>=self.c.entry_ttl_seconds*1000:
                self.decision(now,'entry_expired' if not flatten else 'cancel_entry_for_halt',order_id=s['order']['id'])
                self.cancel(s,now,'entry_expired' if not flatten else 'portfolio_halt')
            else:self.decision(now,'waiting_for_buy_fill',pair=s['pair'],order_id=s['order']['id'])
            return
        gates=[(flatten,'portfolio_halt_or_cutoff'),(bool(self.store.get('tick_fee_violation')),'unexpected_execution_fee_or_role'),(not start<=now<end,'outside_competition_period'),(now<s['cooldown'],'cooldown'),(now-max(s['last_entry'],s.get('last_attempt',0))<self.c.min_entry_interval_seconds*1000,'minimum_entry_interval'),(counts['entry_attempts_today']>=self.c.max_entry_attempts_per_day,'daily_entry_attempt_limit'),(counts['completed_cycles_today']>=self.c.max_cycles_per_day,'daily_completed_cycle_limit')]
        for blocked,reason in gates:
            if blocked:self.decision(now,reason,**counts);return
        # Warm up a real public quote history; no retrospective minute is invented.
        if len(history)<10 or now-history[0]['t']<300000:
            self.decision(now,'warming_quote_history',observations=len(history),history_seconds=(now-history[0]['t'])/1000);return
        candidates=[];skips=[]
        for pair in self.c.pairs:
            m=self.meta['TradePairs'][pair]
            if not m.get('CanTrade'):skips.append({'pair':pair,'reason':'pair_not_tradable'});continue
            bid,ask=validated_quote(quotes,pair);precision=m['PricePrecision'];tick=10**(-precision)
            if (ask-bid)/tick>self.c.max_spread_ticks+1e-6:
                skips.append({'pair':pair,'reason':'spread_too_wide','spread_ticks':(ask-bid)/tick});continue
            entry=shift_price(bid,precision,-self.c.entry_behind_bid_ticks)
            # At the best bid by default; remain below ask even for locked quotes.
            entry=min(entry,shift_price(ask,precision,-1))
            target=shift_price(entry,precision,self.c.target_ticks)
            if entry<=0 or entry>=ask:skips.append({'pair':pair,'reason':'no_valid_passive_price'});continue
            edge=net_cycle(entry,target,self.c.maker_fee)
            q=quotes['Data'][pair]
            detail={'pair':pair,'entry':entry,'target':target,'bid':bid,'ask':ask,'last':q['LastPrice'],'expected_net_bps':edge*10000,'volume_usd':q.get('UnitTradeValue',0)}
            if edge*10000<self.c.min_cycle_net_bps:skips.append({**detail,'reason':'insufficient_fee_margin'});continue
            if not math.isfinite(float(q.get('UnitTradeValue',0))) or float(q.get('UnitTradeValue',0))<self.c.min_daily_volume:skips.append({**detail,'reason':'low_volume'});continue
            recent=[r['quotes'][pair] for r in history if now-r['t']<=1800000]
            # Compare last prices to last prices; a bid/last comparison penalized spread changes.
            recent_return=float(q['LastPrice'])/recent[0]-1
            if recent_return<-.003 or float(q['LastPrice'])<sum(recent)/len(recent)-2*tick:
                skips.append({**detail,'reason':'falling_price_filter','recent_return':recent_return});continue
            loss=(self.c.stop_ticks+1)*tick/entry+self.c.maker_fee+self.c.taker_fee
            notional=min(equity*self.c.allocation_cap,equity*self.c.planned_risk/loss,free_cash/(1+self.c.maker_fee))
            if not self.probe_verified(pair):notional=min(notional,self.c.probe_notional)
            candidates.append((edge,pair,entry,notional))
        if candidates:
            edge,pair,entry,notional=max(candidates)
            s['pair']=pair;self.save(s)
            self.decision(now,'placing_entry',pair=pair,entry=entry,notional=notional,expected_net_bps=edge*10000)
            self.submit(s,'BUY',notional/entry,entry,now)
        else:self.decision(now,'no_eligible_setup',pair_checks=skips)


def run_tick(config='tick_config.json',mode='shadow',once=False):
    if once and mode!='shadow':raise ValueError("Remote paper trading requires a continuous runner")
    c=TickConfig.load(config)
    api=Roostoo(spacing=2.)
    lock=InstanceLock(Path(c.runtime_dir)/'bot.lock')
    store=Store(Path(c.runtime_dir)/('tick_'+mode.replace('-','_')+'.sqlite3'))
    try:
        now=api.sync_time();meta=api.exchange_info();engine=TickEngine(c,api,store,meta,mode)
        if mode=='paper-exchange' and api.short_positions():raise ApiError("Unmanaged shorts; audited account handover required")
        store.event('tick_startup',{'permission_basis':c.tick_permission_basis,**engine.stamp()},now)
        while True:
            started=time.monotonic()
            try:
                if time.monotonic()-api.last_sync>600:api.sync_time()
                quotes=api.ticker()
                if abs(api.now_ms()-int(quotes.get('ServerTime',api.now_ms())))>60000:raise ApiError("Stale public quotes")
                engine.step(quotes,api.now_ms())
            except ApiError as e:store.event('tick_api_pause',{'error':str(e)})
            report_now=api.now_ms()
            if report_now-int(store.get('tick_last_report_ms',0))>=c.report_interval_seconds*1000:
                from .tick_report import export_report
                try:
                    export_report(store.db,mode,Path(c.runtime_dir)/'reports'/mode,report_now)
                    store.set('tick_last_report_ms',report_now)
                except OSError as e:store.event('tick_report_error',{'error':str(e)})
            if once:break
            time.sleep(max(0,c.poll_seconds-(time.monotonic()-started)))
    finally:
        store.close();lock.close()
