"""Original, causal price-grid limit-cycle research on actual minute candles.

Touch fills deliberately model the possible paper-exchange advantage of no queue.
Trade-through and sampled-close variants expose sensitivity to that assumption.
Orders are decided from completed bars; no same-bar entry/target round trips.
"""
from dataclasses import dataclass, asdict
from pathlib import Path
import json
import sys

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
MINUTE = 60000


@dataclass(frozen=True)
class Spec:
    name: str
    entry_offset: int = 1
    target_ticks: int = 1
    stop_ticks: int = 3
    hold_minutes: int = 30
    ttl_minutes: int = 5
    cooldown_minutes: int = 3
    decision_minutes: int = 5
    min_net_bps: float = 6.
    filter: str = "stable"
    maker_fee: float = .0005
    taker_fee: float = .001
    stop_extra_ticks: int = 1
    fill: str = "touch"
    risk: float = .0015
    cap: float = .15
    max_daily_cycles: int = 24
    min_daily_volume: float = 2_000_000.


def load(coin):
    d = pd.read_csv(ROOT / f"data/limit_minutes/{coin}_USD.csv.gz")
    if not (d.timestamp.diff().iloc[1:] == MINUTE).all():
        # Separate contiguous sections: invalidate warmup rather than compress gaps.
        d["segment"] = (d.timestamp.diff().fillna(MINUTE) != MINUTE).cumsum()
    else:
        d["segment"] = 0
    d["warm"] = d.groupby("segment").cumcount() >= 1440
    d["ret30"] = d.close.pct_change(30)
    d["ret60"] = d.close.pct_change(60)
    d["ema60"] = d.close.ewm(span=60, adjust=False, min_periods=60).mean()
    d["v24"] = d.quote_volume.rolling(1440, min_periods=1440).sum()
    d["range30"] = d.high.rolling(30).max() - d.low.rolling(30).min()
    return {c:d[c].to_numpy() for c in d.columns}


def cycle_return(entry, exit_price, spec, maker_exit=True):
    fee = spec.maker_fee if maker_exit else spec.taker_fee
    # Conservative cash-fee convention; per-order rounding can only reduce proceeds.
    return exit_price * (1-fee) / (entry * (1+spec.maker_fee)) - 1


def one_coin(a, coin, tick, spec, start, end, save_trades=False):
    indexes = np.flatnonzero((a['timestamp'] >= start) & (a['timestamp'] < end))
    if not len(indexes):
        return None, []
    cash, equity, peak = 100000., 100000., 100000.
    pending, position = None, None
    next_decision = 0
    trades, curve, orders = [], [], {}
    day, day_start, cycles_today = None, cash, 0
    halted = False
    daily_halt = False
    min_dd = 0.
    i_last = int(indexes[-1])

    def close(i, px, reason, maker_exit):
        nonlocal cash, position, next_decision, cycles_today
        entry, qty, ts, fee = position
        cash += qty*px*(1-(spec.maker_fee if maker_exit else spec.taker_fee))
        pnl = qty*px*(1-(spec.maker_fee if maker_exit else spec.taker_fee))-qty*entry-fee
        trades.append({'coin':coin,'entry_ts':ts,'exit_ts':int(a['timestamp'][i])+MINUTE,'entry':entry,'exit':px,'pnl':pnl,'reason':reason,'quantity':qty})
        orders[int(a['timestamp'][i]//86400000)] = orders.get(int(a['timestamp'][i]//86400000),0)+1
        position = None
        next_decision = i+spec.cooldown_minutes+1
        cycles_today += 1

    for ii in indexes:
        i = int(ii)
        t = int(a['timestamp'][i])
        if day != t//86400000:
            day,day_start,cycles_today,daily_halt = t//86400000,equity,0,False
        op,hi,lo,cl = (float(a[c][i]) for c in ('open','high','low','close'))
        has_trade = float(a['trades'][i]) > 0
        if position:
            entry,qty,ts,fee=position
            stop=entry-spec.stop_ticks*tick
            target=entry+spec.target_ticks*tick
            hit_stop=lo <= stop+tick*1e-8
            threshold=target+(tick if spec.fill=='through' else 0)
            hit_target=(cl if spec.fill=='sampled' else hi) >= threshold-tick*1e-8 and has_trade
            if hit_stop:
                close(i,max(tick,min(op,stop)-spec.stop_extra_ticks*tick),'stop',False)
            elif hit_target:
                close(i,target,'target',True)
            elif t-ts>=spec.hold_minutes*MINUTE or i==i_last:
                close(i,max(tick,cl-spec.stop_extra_ticks*tick),'time' if i!=i_last else 'episode_end',False)
        elif pending:
            px,submitted,qty=pending
            threshold=px-(tick if spec.fill=='through' else 0)
            touched=(cl if spec.fill=='sampled' else lo)<=threshold+tick*1e-8 and has_trade
            if touched:
                fee=qty*px*spec.maker_fee
                cash-=qty*px+fee
                position=(px,qty,t+MINUTE,fee)
                pending=None
                orders[day]=orders.get(day,0)+1
                # The entry-bar low may occur after the fill: use the adverse outcome.
                if lo<=px-spec.stop_ticks*tick:
                    close(i,max(tick,min(op,px-spec.stop_ticks*tick)-spec.stop_extra_ticks*tick),'entry_bar_stop',False)
            elif i-submitted>=spec.ttl_minutes:
                pending=None
                next_decision=i+spec.cooldown_minutes
        equity=cash+(position[1]*max(tick,cl-spec.stop_extra_ticks*tick)*(1-spec.taker_fee) if position else 0.)
        peak=max(peak,equity)
        if equity<peak*.975 or equity<day_start*.9925:
            if position:
                close(i,max(tick,cl-spec.stop_extra_ticks*tick),'risk_halt',False)
                equity=cash
            pending=None
            if equity<peak*.975:halted=True
            else:daily_halt=True
        min_dd=min(min_dd,equity/peak-1)
        if i%60==0 or i==i_last:curve.append(equity)
        if halted or daily_halt or position or pending or i<next_decision or i>=i_last-spec.hold_minutes:
            continue
        if i%spec.decision_minutes or i<1 or cycles_today>=spec.max_daily_cycles or not a['warm'][i]:
            continue
        px=round(cl/tick)*tick-spec.entry_offset*tick
        if px<=0 or a['v24'][i]<spec.min_daily_volume:
            continue
        net=cycle_return(px,px+spec.target_ticks*tick,spec)
        if net*10000<spec.min_net_bps:
            continue
        if spec.filter=='stable':
            if a['ret30'][i]<-.003 or a['ret60'][i]<-.006 or cl<a['ema60'][i]-2*tick:
                continue
        elif spec.filter=='quiet':
            if abs(a['ret30'][i])>.004 or a['range30'][i]>8*tick:
                continue
        elif spec.filter=='imbalance':
            if a['ret30'][i]<-.003 or a['volume'][i]<=0 or a['taker_base'][i]/a['volume'][i]<.5:
                continue
        loss=-cycle_return(px,max(tick,px-(spec.stop_ticks+spec.stop_extra_ticks)*tick),spec,False)
        notional=min(equity*spec.cap,equity*spec.risk/max(loss,1e-9),cash/(1+spec.maker_fee))
        qty=np.floor(notional/px) if coin in {'BONK','PEPE','SHIB','1000CHEEMS'} else np.floor(notional/px*10)/10
        if qty*px<10:continue
        pending=(px,i,qty)
    vals=np.array(curve)
    ps=np.array([r['pnl'] for r in trades])
    result={'coin':coin,'start':str(pd.to_datetime(start,unit='ms',utc=True)),'end':str(pd.to_datetime(end,unit='ms',utc=True)),'return':cash/100000-1,'max_drawdown':min_dd,'trades':len(trades),'win_rate':float((ps>0).mean()) if len(ps) else None,'profit_factor':float(ps[ps>0].sum()/-ps[ps<0].sum()) if (ps<0).any() else None,'active_utc_days':sum(n>0 for n in orders.values()),'halts':halted,'target_exits':sum(r['reason']=='target' for r in trades),'mean_pnl':float(ps.mean()) if len(ps) else None}
    return result,trades if save_trades else []


def main():
    info=json.loads((ROOT/'research/limit_research/exchange_info.json').read_text())
    specs=[Spec('passive_one_tick'),Spec('passive_two_tick',target_ticks=2),Spec('touch_bid_one_tick',entry_offset=0),Spec('quiet_one_tick',filter='quiet'),Spec('imbalance_one_tick',filter='imbalance'),Spec('longer_one_tick',hold_minutes=120,stop_ticks=5)]
    coins=['BONK','PEPE','SHIB','1000CHEEMS','WLFI','STO','HEMI','LISTA']
    runs=[]
    for coin in coins:
        a=load(coin)
        tick=10**(-info['TradePairs'][coin+'/USD']['PricePrecision'])
        for spec in specs:
            parts=[]
            for start,end in [('2026-01-02','2026-07-01'),('2026-07-01','2026-09-01'),('2026-09-01','2026-10-09')]:
                sm,em=(int(pd.Timestamp(x,tz='UTC').timestamp()*1000) for x in (start,end))
                r,_=one_coin(a,coin,tick,spec,sm,em)
                if r:parts.append(r)
            runs.append({'spec':asdict(spec),'coin':coin,'periods':parts})
        print(json.dumps({'coin':coin,'done':len(runs)}),flush=True)
    dest=ROOT/'research/limit_research/development_comparison.json'
    dest.write_text(json.dumps(runs,indent=2,allow_nan=False))
    for row in runs:
        hold=row['periods'][-1]
        print(json.dumps({'coin':row['coin'],'spec':row['spec']['name'],'holdout_return':round(hold['return']*100,3),'win_rate':hold['win_rate'],'trades':hold['trades']}),flush=True)


if __name__=='__main__':
    main()
