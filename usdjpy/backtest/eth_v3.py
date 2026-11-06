from sim import *
NAKED={"2026-10-20-pm","2026-10-23-am","2026-10-27-am","2026-10-29-am","2026-11-03-pm"}
def split(o,e1,sl,tp,off,hold_days=3):
    s=1 if o['side']=='BUY' else -1
    e2=round(e1+s*off,3)
    if off<=0 or (o['px']-e2)*s<=0: e2=None
    legs=[e1]+([e2] if e2 else [])
    day=o['created'][:10]; exp=day+" 23:59"
    end=(datetime.strptime(day,"%Y-%m-%d")+timedelta(days=hold_days)).strftime("%Y-%m-%d 23:59")
    far=o['zl'] if s==1 else o['zh']
    i0=next(i for i,t in enumerate(T) if t>=o['created'])
    filled=set()
    for i in range(i0,len(m5)):
        t,op,h,l,c=m5[i]
        if t>end: break
        if not filled and t>exp: return dict(st="EXPIRED",r=None)
        if t<=exp:
            for k,e in enumerate(legs):
                if k not in filled and ((l<=e) if s==1 else (h>=e)): filled.add(k)
        if not filled:
            if i+1<len(m5) and m5[i+1][0][:13]!=t[:13] and ((s==1 and c<far) or (s==-1 and c>far)): return dict(st="INVALIDATED",r=None)
            continue
        slhit=(l<=sl) if s==1 else (h>=sl); tphit=(h>=tp) if s==1 else (l<=tp)
        if slhit or tphit:
            px=sl if slhit else tp
            usd=sum(0.5*(px-legs[k])*s for k in filled)
            den=sum(0.5*(legs[k]-sl)*s for k in range(len(legs)))
            return dict(st="SL" if slhit else "TP",r=round(usd/den,2),legs=len(filled))
    last=m5[i][4]; usd=sum(0.5*(last-legs[k])*s for k in filled); den=sum(0.5*(legs[k]-sl)*s for k in range(len(legs)))
    return dict(st="OPEN_END",r=round(usd/den,2))
def variant(name,naked=False,off=0,r1=False,r2=False,r3=False,quiet=True):
    res=[]
    for o in orders:
        if naked and o['id'] in NAKED: res.append(None); continue
        s=1 if o['side']=='BUY' else -1; e=o['entry']; sl=o['sl']
        if r2:
            near=o['zh'] if s==1 else o['zl']; e2=round(near+s*0.02,3)
            if (e2-e)*s>0 and (o['px']-e2)*s>0: e=e2
        if r3:
            if (e-sl)*s/P<22: sl=round(e-s*0.22,3)
            if (e-sl)*s/P>50: res.append(None); continue
        if r1:
            pos=d1pos(o['created'][:10],e)
            if (s==1 and pos>55) or (s==-1 and pos<45): res.append(None); continue
        res.append(split(o,e,sl,o['tp1'],off))
    return summarize(name,res),res
variant("v1 механически",)
variant("П1 без голого FVG",naked=True)
for off in (5,8,10,12,15): variant(f"П2 сплит, L2 на {off} п ближе",off=off*P)
for off in (8,10,12): variant(f"v3 = П1+П2 ({off} п)",naked=True,off=off*P)
print("--- против USDJPY v2.1")
variant("R2+R3 (без D1)",r2=True,r3=True)
variant("v2.1 R1+R2+R3",r1=True,r2=True,r3=True)
variant("R2+R3 + сплит 10 п",r2=True,r3=True,off=0.10)
variant("v2.1 + сплит 10 п",r1=True,r2=True,r3=True,off=0.10)
variant("П2 (10 п) + R3",r3=True,off=0.10)
variant("П2 (10 п) + R1 + R3",r1=True,r3=True,off=0.10)
print("--- половины периода")
_,a=variant("v3 (10 п)",naked=True,off=0.10)
_,b=variant("R2+R3",r2=True,r3=True)
for nm,res in (("v3 10п",a),("R2+R3",b)):
    for lo,hi in (("2026-10-01","2026-10-20"),("2026-10-21","2026-11-06")):
        summarize(f"{nm} {lo[5:]}–{hi[5:]}",[r if lo<=o['id'][:10]<=hi else None for o,r in zip(orders,res)])
