"""Slower trend allocation comparison; descriptive, continuously held portfolio."""
from pathlib import Path
import json
import numpy as np
import pandas as pd

ROOT=Path(__file__).resolve().parents[1]


def main():
    prices=pd.DataFrame({c:pd.read_csv(ROOT/f'data/{c}_USD.csv').set_index('timestamp').close for c in ['BTC','ETH','BNB','SOL','XRP']}).dropna()
    returns=prices.pct_change();outputs=[]
    for name,shorts in [('trend_long',False),('trend_both',True)]:
        nav=100000.;weights=np.zeros(5);curve=[]
        for i,t in enumerate(prices.index):
            if i<24*31:continue
            r=returns.iloc[i].to_numpy();gain=float(weights@r)
            nav*=1+gain;weights=weights*(1+r)/(1+gain)
            if t%86400000==0:
                gates=[]
                for h in [7*24,14*24,30*24]:
                    window=returns.iloc[i-h+1:i+1]
                    trend=window.mean().to_numpy()*h;noise=window.std().to_numpy()*np.sqrt(h)
                    gates.append(np.where(trend>.4*noise,1.,np.where(trend<-.4*noise,-1.,0.)))
                direction=np.mean(gates,axis=0)
                if not shorts:direction=np.maximum(direction,0)
                vol=returns.iloc[i-24*30+1:i+1].std().to_numpy()*np.sqrt(24*365)
                base=direction/np.maximum(vol,.10)
                target=np.clip(base/max(1.,np.abs(base).sum())*.60,-.15,.20)
                target=np.where(target<0,target*.5,target)
                change=np.where(np.abs(target-weights)>.03,target-weights,0.)
                fee=np.where((target<0)|(weights<0),.001,.0005)
                nav*=1-float(np.abs(change)@fee);weights+=change
            curve.append({'timestamp':int(t),'equity':nav})
        curve=pd.DataFrame(curve);episodes=[]
        for start in pd.date_range('2026-01-01','2026-09-10',freq='14D',tz='UTC'):
            sm=int(start.timestamp()*1000);d=curve[(curve.timestamp>=sm)&(curve.timestamp<sm+14*86400000)]
            if len(d)!=336:continue
            vals=d.equity.to_numpy();episodes.append({'start':str(start),'return':float(vals[-1]/vals[0]-1),'max_drawdown':float((vals/np.maximum.accumulate(vals)-1).min())})
        rs=np.array([r['return'] for r in episodes])
        outputs.append({'name':name,'mean_14d':float(rs.mean()),'median_14d':float(np.median(rs)),'positive_14d':int((rs>0).sum()),'over_1pct':int((rs>.01).sum()),'worst_14d':float(rs.min()),'best_14d':float(rs.max()),'episodes':episodes,'note':'Continuous portfolio with daily gates; windows are descriptive slices, not independent capital resets. Ideal rebalance fills with specified fees; no proof of live maker fills or activity qualification.'})
    (ROOT/'research/limit_research/slow_trend_comparison.json').write_text(json.dumps(outputs,indent=2))
    print(json.dumps([{k:v for k,v in x.items() if k!='episodes'} for x in outputs],indent=2))


if __name__=='__main__':main()
