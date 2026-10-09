"""Explicit later-period comparison, independent windows and stress cases.

The PEPE whitelist was selected after exploratory comparisons. These are
retrospective validation results, not an untouched prospective test.
"""
from dataclasses import asdict, replace
import json
from pathlib import Path
import numpy as np
import pandas as pd
from evaluate_tick_cycles import Spec, load, one_coin

ROOT=Path(__file__).resolve().parents[1]
DEST=ROOT/'research/limit_research'


def main():
    stress=[]
    for coin in ['BONK','PEPE','SHIB']:
        a=load(coin)
        for mode in ['touch','through','sampled']:
            spec=replace(Spec('passive_one_tick'),fill=mode)
            periods=[]
            for start,end in [('2026-01-02','2026-09-01'),('2026-09-01','2026-10-09')]:
                sm,em=(int(pd.Timestamp(x,tz='UTC').timestamp()*1000) for x in (start,end))
                r,tr=one_coin(a,coin,1e-8,spec,sm,em,True);periods.append(r)
                if start=='2026-09-01':pd.DataFrame(tr).to_csv(DEST/f'{coin}_{mode}_trades.csv',index=False)
            stress.append({'coin':coin,'fill':mode,'periods':periods})
            print(json.dumps({'coin':coin,'fill':mode,'evaluation':periods[-1]}),flush=True)
    (DEST/'fill_stress.json').write_text(json.dumps(stress,indent=2))
    a=load('PEPE');base=Spec('passive_one_tick')
    configs=[base,replace(base,name='through',fill='through'),replace(base,name='sampled',fill='sampled'),replace(base,name='touch_fees_150',maker_fee=.00075,taker_fee=.0015),replace(base,name='sampled_fees_150',fill='sampled',maker_fee=.00075,taker_fee=.0015),replace(base,name='touch_stop_2',stop_ticks=2),replace(base,name='touch_stop_4',stop_ticks=4),replace(base,name='touch_entry_2',entry_offset=2),replace(base,name='touch_10pct_size',cap=.10),replace(base,name='touch_100dollar_probe',cap=.001,risk=.00001)]
    reports=[]
    for c in configs:
        episodes=[];ts=[]
        for start in pd.date_range('2026-01-02','2026-09-11',freq='14D',tz='UTC'):
            sm=int(start.timestamp()*1000);em=sm+14*86400000
            r,tr=one_coin(a,'PEPE',1e-8,c,sm,em,True);episodes.append(r)
            ts.extend([{**x,'episode':len(episodes)} for x in tr])
        returns=np.array([r['return'] for r in episodes]);pnl=np.array([r['pnl'] for r in ts])
        agg={'episodes':len(episodes),'mean_14d':float(returns.mean()),'median_14d':float(np.median(returns)),'positive_windows':int((returns>0).sum()),'over_one_percent':int((returns>.01).sum()),'worst_14d':float(returns.min()),'best_14d':float(returns.max()),'worst_drawdown':min(r['max_drawdown'] for r in episodes),'trades':len(ts),'win_rate':float((pnl>0).mean()) if len(pnl) else None,'windows_with_8_provisional_utc_active_days':sum(r['active_utc_days']>=8 for r in episodes)}
        r,tr=one_coin(a,'PEPE',1e-8,c,int(pd.Timestamp('2026-10-01',tz='UTC').timestamp()*1000),int(pd.Timestamp('2026-10-09',tz='UTC').timestamp()*1000),True)
        report={'spec':asdict(c),'aggregate':agg,'episodes':episodes,'october_1_to_8':r};reports.append(report)
        print(json.dumps({'spec':c.name,'aggregate':agg,'recent':r}),flush=True)
        pd.DataFrame(ts).to_csv(DEST/f'PEPE_{c.name}_episodes_trades.csv',index=False)
    (DEST/'candidate_validation.json').write_text(json.dumps(reports,indent=2))
    # Conditional statistical description: daily clustered net P&L in touch model.
    d=pd.read_csv(DEST/'PEPE_touch_trades.csv');d['day']=d.exit_ts//86400000
    returns=d.groupby('day').pnl.sum().to_numpy()/100000
    rng=np.random.default_rng(610);samples=returns[rng.integers(0,len(returns),(5000,len(returns)))].mean(axis=1)
    (DEST/'conditional_daily_bootstrap.json').write_text(json.dumps({'days':len(returns),'mean_daily_pnl_over_initial_cash':float(returns.mean()),'one_sided_90pct_lower_mean':float(np.quantile(samples,.1)),'caveat':'Conditional on touch fills, full simulated quantity, fixed precision and historical proxy. Not a confidence bound for the actual Roostoo matching engine or simultaneous strategy selection.'},indent=2))


if __name__=='__main__':main()
