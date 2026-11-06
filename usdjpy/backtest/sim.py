import csv, json, os, sys
from datetime import datetime, timedelta
ST="/tmp/smc-state/usdjpy"
P=0.01
m5=[(r['t'],float(r['o']),float(r['h']),float(r['l']),float(r['c'])) for r in csv.DictReader(open("m5.csv"))]
T=[r[0] for r in m5]
d1=[]  # D1 from M5, Mon-Fri only
days={}
for t,o,h,l,c in m5:
    d=t[:10]
    if datetime.strptime(d,"%Y-%m-%d").weekday()>=5: continue
    x=days.setdefault(d,[o,h,l,c]); x[1]=max(x[1],h); x[2]=min(x[2],l); x[3]=c
# need 20 days before 10-05: use morning-run D1 csv for earlier
for r in csv.DictReader(open("../1day.csv")):
    if datetime.strptime(r['t'],"%Y-%m-%d").weekday()<5 and r['t'] not in days:
        days[r['t']]=[float(r['o']),float(r['h']),float(r['l']),float(r['c'])]
D=sorted(days)
def d1pos(day,price):
    prev=[days[d] for d in D if d<day][-20:]
    hi=max(x[1] for x in prev); lo=min(x[2] for x in prev); return (price-lo)/(hi-lo)*100
def h1_closes(t0,t1):
    # H1 closes from M5 in [t0,t1)
    out={}
    for t,o,h,l,c in m5:
        if t0<=t<t1: out[t[:13]]=(t,c)
    return sorted(out.values())
orders=[]
for x in json.load(open(f"{ST}/trades.json")):
    if x['status']=='PENDING': continue
    d=x['id'][:10]; f=f"{ST}/{d}.json" if x['id'].endswith('am') else f"{ST}/{d}-midday.json"
    o=(json.load(open(f)).get('order') or {}) if os.path.exists(f) else {}
    sl=o.get('sl',x['sl'])
    if x['id']=='2026-11-04-am': sl=158.712
    orders.append(dict(id=x['id'],side=x['side'],basis=x['basis'],entry=x['entry'],sl=sl,tp1=x['tp1'],tp2=o.get('tp2'),
        zl=x['zone_low'],zh=x['zone_high'],px=x['price_at_creation'],created=x['created_at'][:16].replace('T',' '),journal=x['status'],jr=x['result_r']))
def run(od,entry,sl,tp1,tp2,partial,hold_days=3):
    s=1 if od['side']=='BUY' else -1
    risk=(entry-sl)*s
    day=od['created'][:10]; exp=day+" 23:59"
    i0=next(i for i,t in enumerate(T) if t>=od['created'])
    # pending phase
    filled=None; closest=None
    far = od['zl'] if s==1 else od['zh']
    lasth=None
    for i in range(i0,len(m5)):
        t,o,h,l,c=m5[i]
        if t>exp: break
        # invalidation: H1 body close beyond far edge (check at H1 close = last M5 of hour)
        if i+1<len(m5) and m5[i+1][0][:13]!=t[:13]:
            if (s==1 and c<far) or (s==-1 and c>far): return dict(st="INVALIDATED",r=None,close=closest)
        hit = (l<=entry) if s==1 else (h>=entry)
        if hit: filled=i; break
        dist=((l-entry) if s==1 else (entry-h))/P
        closest=dist if closest is None else min(closest,dist)
    if filled is None: return dict(st="EXPIRED",r=None,close=round(closest,1) if closest is not None else None)
    # open phase
    end=(datetime.strptime(day,"%Y-%m-%d")+timedelta(days=hold_days)).strftime("%Y-%m-%d 23:59")
    rr1=(tp1-entry)*s/risk; rr2=((tp2-entry)*s/risk) if tp2 else None
    got1=False; cur_sl=sl
    for i in range(filled,len(m5)):
        t,o,h,l,c=m5[i]
        if t>end: break
        slhit=(l<=cur_sl) if s==1 else (h>=cur_sl)
        if i==filled:
            # fill bar: conservative — SL counts if touched in same bar
            pass
        if not got1:
            tphit=(h>=tp1) if s==1 else (l<=tp1)
            if slhit: return dict(st="SL",r=-1.0,t=t)
            if tphit:
                if not partial: return dict(st="TP",r=round(rr1,2),t=t)
                got1=True; cur_sl=entry
                # same bar tp2?
                if rr2 and ((h>=tp2) if s==1 else (l<=tp2)): return dict(st="TP",r=round(0.5*rr1+0.5*rr2,2),t=t,run="TP2")
                continue
        else:
            if slhit: return dict(st="TP",r=round(0.5*rr1,2),t=t,run="BE")
            if (h>=tp2) if s==1 else (l<=tp2): return dict(st="TP",r=round(0.5*rr1+0.5*rr2,2),t=t,run="TP2")
    last=m5[min(i,len(m5)-1)][4]
    fr=(last-entry)*s/risk
    if got1: return dict(st="TP",r=round(0.5*rr1+0.5*fr,2),run="MKT")
    return dict(st="OPEN_END",r=round(fr,2))
def v2params(od,use=("R1","R2","R3","R4")):
    s=1 if od['side']=='BUY' else -1
    e=od['entry']; sl=od['sl']
    if "R2" in use:
        near = od['zh'] if s==1 else od['zl']
        e2 = round(near + s*0.02,3)
        # never deeper than original, never beyond price at creation
        if (e2-e)*s>0 and (od['px']-e2)*s>0: e=e2
    risk=(e-sl)*s/P
    if "R3" in use:
        if risk<22: sl=round(e-s*0.22,3); risk=22
        if risk>50: return None,"risk>50"
    if "R1" in use:
        pos=d1pos(od['created'][:10],e)
        if (s==1 and pos>55) or (s==-1 and pos<45): return None,f"D1 {pos:.0f}%"
    rr1=(od['tp1']-e)*s/((e-sl)*s)
    if "R4" in use and rr1<1.0: return None,f"RR1 {rr1:.2f}"
    return (e,sl),None
def summarize(name,res):
    fl=[r for r in res if r and r['st'] in("TP","SL","OPEN_END")]
    tp=sum(r['st']=="TP" for r in fl); sl=sum(r['st']=="SL" for r in fl)
    n=sum(1 for r in res if r is not None)
    R=sum(r['r'] for r in fl)
    print(f"{name:34} ордеров {n:2} | исполн {len(fl):2} ({len(fl)/n*100 if n else 0:3.0f}%) | TP {tp:2} SL {sl:2} | WR {tp/(tp+sl)*100 if tp+sl else 0:3.0f}% | ΣR {R:+.2f} | avgR {R/len(fl) if fl else 0:+.2f}")
    return dict(n=n,filled=len(fl),tp=tp,sl=sl,R=round(R,2))
