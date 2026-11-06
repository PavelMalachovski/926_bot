import sys;sys.path.insert(0,'/tmp/claude-0/-home-user-926-bot/62a67582-ef00-5144-a021-fd06a5610889/scratchpad/hist')
import engine,run
from run import *
engine.trend_bos.__defaults__=(2,);run.trend=engine.trend_bos
D1T,D1=agg(24) if False else (None,None)
# D1 by UTC day
dd={}
for t,(o,h,l,c) in zip(TS,M5):
    k=t.date()
    if k not in dd: dd[k]=[o,h,l,c]
    else: x=dd[k];x[1]=max(x[1],h);x[2]=min(x[2],l);x[3]=c
DK=sorted(dd);DB=[dd[k] for k in DK]
def d1ctx(now):
    n=bisect.bisect_left(DK,now.astimezone(timezone.utc).date())  # closed days
    b=DB[max(0,n-120):n]
    tr=engine.trend_bos(b,2)
    hs,ls=pivots(b,2)
    if not hs or not ls: return tr,None
    i0=min(hs[-1],ls[-1]);hi=max(x[1] for x in b[i0:]);lo=min(x[2] for x in b[i0:])
    return tr,hi,lo
def first_friday(d): 
    x=date(d.year,d.month,1)
    while x.weekday()!=4: x+=timedelta(days=1)
    return x
FOMC={date(2025,12,10),date(2026,1,28),date(2026,3,18),date(2026,4,29),date(2026,6,17),date(2026,7,29),date(2026,9,16),date(2026,10,28)}
def enrich(os_):
    for o in os_:
        tr,*r=d1ctx(o['now']);o['d1']=tr
        if r and r[0] is not None and r[0]>r[1]:
            o['pd']=100*(o['p']-r[1])/(r[0]-r[1])
        else: o['pd']=None
        o['news']=o['day']==first_friday(o['day']) or o['day'] in FOMC
        o['wd']=o['day'].weekday()<5
        o['dist']=abs(o['entry']-o['p'])
    return os_
base=dict(k=5)
OS=enrich(orders(base))
H1_=(date(2025,11,1),date(2026,4,30));H2_=(date(2026,5,1),date(2026,11,5))
def test(name,keep,shift=None):
    sub=[o for o in OS if keep(o)]
    v=dict(base,shift=shift) if shift else base
    a=evaluate(name+(' +50/50' if shift else ''),v,sub)
    import io,contextlib
    for per,lbl in ((H1_,'  ноя–апр'),(H2_,'  май–ноя')):
        evaluate(lbl,v,sub,per)
def premium_ok(o,th):  # SELL needs pd>=th, BUY needs pd<=100-th
    if o['pd'] is None: return True
    return o['pd']>=th if o['side']=='SELL' else o['pd']<=100-th
tests=[
 ('все (v1 мех.)',lambda o:True),
 ('D1 структура по сделке',lambda o:o['d1']==('down' if o['side']=='SELL' else 'up')),
 ('D1 структура не против',lambda o:o['d1']!=('up' if o['side']=='SELL' else 'down')),
 ('D1 PD: SELL≥50 / BUY≤50',lambda o:premium_ok(o,50)),
 ('D1 PD: SELL≥25 / BUY≤75',lambda o:premium_ok(o,25)),
 ('D1 PD против: SELL<50 / BUY>50',lambda o:not premium_ok(o,50)),
 ('без NFP/FOMC дней',lambda o:not o['news']),
 ('только am',lambda o:o['tag']=='am'),
 ('только pm',lambda o:o['tag']=='pm'),
 ('только будни',lambda o:o['wd']),
 ('риск ≥ 13$',lambda o:o['risk']>=13),
 ('риск ≥ 20$',lambda o:o['risk']>=20),
 ('риск < 13$',lambda o:o['risk']<13),
 ('дистанция ≤ 25$',lambda o:o['dist']<=25),
 ('дистанция ≤ 40$',lambda o:o['dist']<=40),
 ('дистанция > 40$',lambda o:o['dist']>40),
 ('SELL',lambda o:o['side']=='SELL'),('BUY',lambda o:o['side']=='BUY'),
]
for n,f in tests: test(n,f)
print(len(OS),'orders; pd None',sum(o['pd'] is None for o in OS))
