import sys;sys.path.insert(0,'/tmp/claude-0/-home-user-926-bot/62a67582-ef00-5144-a021-fd06a5610889/scratchpad/hist')
from engine import *
from datetime import date
def orders(variant,start=date(2025,11,1),end=date(2026,11,5)):
    out=[];d=start
    while d<=end:
        for hh,tag in [(8,'am'),(14,'pm')]:
            now=datetime(d.year,d.month,d.day,hh,0,tzinfo=PR)
            exp=datetime(d.year,d.month,d.day,23,59,tzinfo=PR)
            b1,_=closed(H1s,H1,now,3600,240);b4,_=closed(H4s,H4,now,4*3600,200)
            t1,t4=trend(b1),trend(b4)
            mode=variant.get('bias','agree')
            if mode=='agree': side={'down':'SELL','up':'BUY'}.get(t1) if t1==t4 else None
            else: side={'down':'SELL','up':'BUY'}.get(t1) if t4 in (t1,'none') else None
            if not side: continue
            o=setup(now,side,variant)
            if not o: continue
            if tag=='pm' and out and out[-1]['day']==d and abs(out[-1]['entry']-o['entry'])<3: continue
            o.update(day=d,tag=tag,now=now,exp=exp);out.append(o)
        d+=timedelta(days=1)
    return out
def evaluate(name,variant,os_=None,period=None):
    os_=os_ or orders(variant)
    res=[]
    for o in os_:
        if period and not(period[0]<=o['day']<=period[1]): continue
        sh=variant.get('shift')
        if sh=='r': sh=0.4*o['risk']
        st1,r1=simulate(o,o['entry'],o['now'],o['exp'])
        legs=[(st1,r1,o['risk'])]
        if sh:
            e2=o['entry']-sh if o['side']=='SELL' else o['entry']+sh
            if (o['side']=='SELL' and e2>o['p']) or (o['side']=='BUY' and e2<o['p']):
                st2,r2=simulate(o,e2,o['now'],o['exp']);legs.append((st2,r2,abs(o['sl']-e2)))
        if len(legs)==1: usd=r1*o['risk'];risk=o['risk']
        else: usd=sum(0.5*r*k for s,r,k in legs);risk=sum(0.5*k for s,r,k in legs)
        filled=any(s in('TP','SL','OPEN') for s,_,_ in legs)
        tp=any(s=='TP' for s,_,_ in legs);sl=filled and not tp and any(s=='SL' for s,_,_ in legs)
        res.append(dict(o=o,filled=filled,tp=tp,sl=sl,R=usd/risk,usd=usd))
    n=len(res);f=sum(r['filled'] for r in res);tp=sum(r['tp'] for r in res);sl=sum(r['sl'] for r in res)
    R=sum(r['R'] for r in res);usd=sum(r['usd'] for r in res)
    line=f"{name:40s} ord {n:3d} fill {100*f/max(n,1):3.0f}% TP {tp:3d} SL {sl:3d} WR {100*tp/max(tp+sl,1):3.0f}% ΣR {R:+7.2f} R/ord {R/max(n,1):+.3f} Σ$ {usd:+8.1f}"
    print(line,flush=True);return res
