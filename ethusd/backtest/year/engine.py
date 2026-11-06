import json,glob,bisect
from datetime import datetime,timezone,timedelta
from zoneinfo import ZoneInfo
PR=ZoneInfo('Europe/Prague')
def load():
    d={}
    for f in sorted(glob.glob('/tmp/claude-0/-home-user-926-bot/62a67582-ef00-5144-a021-fd06a5610889/scratchpad/hist/p*.json')):
        for r in json.load(open(f))['values']:
            t=datetime.strptime(r['datetime'],'%Y-%m-%d %H:%M:%S').replace(tzinfo=timezone.utc)
            d[t]=(float(r['open']),float(r['high']),float(r['low']),float(r['close']))
    ts=sorted(d)[:-1]
    return ts,[d[t] for t in ts]
TS,M5=load()
T5=[t.timestamp() for t in TS]
def agg(hours):
    out={}
    for t,(o,h,l,c) in zip(TS,M5):
        k=t.replace(minute=0,hour=t.hour-t.hour%hours)
        if k not in out: out[k]=[o,h,l,c]
        else:
            x=out[k];x[1]=max(x[1],h);x[2]=min(x[2],l);x[3]=c
    ks=sorted(out);return ks,[out[k] for k in ks]
H1T,H1=agg(1);H4T,H4=agg(4)
H1s=[t.timestamp() for t in H1T];H4s=[t.timestamp() for t in H4T]
def closed(ts_list,arr,now,dur,n):
    i=bisect.bisect_right(ts_list,now.timestamp()-dur)  # bars with t+dur<=now
    return arr[max(0,i-n):i],max(0,i-n)
TK=2
def pivots(b,k=2):
    hs,ls=[],[]
    for i in range(k,len(b)-k):
        if all(b[i][1]>b[j][1] for j in range(i-k,i+k+1) if j!=i): hs.append(i)
        if all(b[i][2]<b[j][2] for j in range(i-k,i+k+1) if j!=i): ls.append(i)
    return hs,ls
def trend(b):
    hs,ls=pivots(b)
    if len(hs)<2 or len(ls)<2: return 'none'
    if b[hs[-1]][1]<b[hs[-2]][1] and b[ls[-1]][2]<b[ls[-2]][2]: return 'down'
    if b[hs[-1]][1]>b[hs[-2]][1] and b[ls[-1]][2]>b[ls[-2]][2]: return 'up'
    return 'none'
def setup(now,side,variant):
    """returns dict(entry,sl,tp,zl,zh,kind,conf) or None. side 'SELL'/'BUY'"""
    b,_=closed(H1s,H1,now,3600,240)
    if len(b)<50: return None
    sgn=1 if side=='SELL' else -1
    # mirror: work in 'sell space' by negating prices for BUY
    def tr(x): return x if sgn==1 else -x
    hi=[tr(x[1]) if sgn==1 else tr(x[2]) for x in b]   # extreme towards SL side
    lo=[tr(x[2]) if sgn==1 else tr(x[1]) for x in b]
    op=[tr(x[0]) for x in b];cl=[tr(x[3]) for x in b]
    p=cl[-1]
    # last confirmed pivot high (in sell space)
    ih=None;K=variant.get('k',2)
    for i in range(len(b)-1-K,K-1,-1):
        if all(hi[i]>hi[j] for j in range(i-K,i+K+1) if j!=i): ih=i;break
    if ih is None: return None
    H=hi[ih];L=min(lo[ih:]);R=H-L
    if R<=0 or p>=L+0.5*R: return None
    band=(L+0.62*R,L+0.79*R)
    cands=[(L+0.62*R,'OTE',True,band[1])]
    for i in range(ih,max(ih-5,0),-1):
        if cl[i]<op[i]:   # in sell space: 'bullish' original candle == close>open originally -> cl>op in tr space for sell; for buy tr negates, bearish orig -> cl>op too
            pass
    obi=None
    for i in range(ih,max(ih-5,-1),-1):
        if cl[i]>op[i]: obi=i;break
    if obi is not None:
        ol,oh=lo[obi],hi[obi]; mid=(ol+oh)/2
        cands.append((mid,'OB',not(oh<band[0] or ol>band[1]),oh))
    for j in range(ih,len(b)-2):
        a_l=lo[j];c_h=hi[j+2]
        if c_h<a_l:
            gl,gh=c_h,a_l
            if j+3<len(b) and max(hi[j+3:])>=gh: continue
            cands.append((gl,'FVG',not(gh<band[0] or gl>band[1]),gh))
    ok=[c for c in cands if c[0]>=L+0.5*R and c[0]<H and c[0]>p+2]
    if variant.get('conf'): ok=[c for c in ok if c[2]]
    if not ok: return None
    if variant.get('pick')=='ote': 
        okk=[c for c in ok if c[0]>=band[0]] or ok
        e,kind,conf,zh=min(okk,key=lambda c:c[0])
    else: e,kind,conf,zh=min(ok,key=lambda c:c[0])
    sl=H+5;risk=sl-e
    if risk<3: return None
    # pools below price (sell space): unswept H1 pivot lows + leg low
    hs,ls=pivots(b)
    pools=[L]
    for i in ls:
        if lo[i]<p and min(lo[i+1:])>=lo[i]: pools.append(lo[i])
    # session low today (Asia / London) from M5
    pools=sorted(set(x for x in pools if x<p),reverse=True)
    tp=None
    for x in pools:
        if (e-x)/risk>=variant.get('minrr',1.5): tp=x;break
    if tp is None or (e-tp)/risk>6: return None
    back=lambda x: x if sgn==1 else -x
    return dict(side=side,entry=back(e),sl=back(sl),tp=back(tp),zh=back(zh),kind=kind,conf=conf,risk=risk,rr=(e-tp)/risk,p=back(p))
def simulate(o,entry,now,expires,maxdays=10):
    buy=o['side']=='BUY';sl,tp,zh=o['sl'],o['tp'],o['zh']
    i=bisect.bisect_left(T5,now.timestamp())
    fill=None
    end=expires.timestamp();lim=now.timestamp()+maxdays*86400
    while i<len(M5):
        t=T5[i];op,h,l,c=M5[i]
        if fill is None:
            if t>=end: return 'EXPIRED',0.0
            touch=(l<=entry) if buy else (h>=entry)
            if touch:
                fill=t
                if (buy and l<=sl) or ((not buy) and h>=sl): return 'SL',-1.0
            elif TS[i].minute==55:
                hc=c   # H1 close == this M5 close
                if (not buy and hc>zh) or (buy and hc<zh): return 'INVALIDATED',0.0
        else:
            hs=(l<=sl) if buy else (h>=sl); ht=(h>=tp) if buy else (l<=tp)
            if hs: return 'SL',-1.0
            if ht: return 'TP',abs(tp-entry)/abs(entry-sl)
            if t>lim: return 'OPEN',((c-entry) if buy else (entry-c))/abs(entry-sl)
        i+=1
    return ('OPEN',((M5[-1][3]-entry) if buy else (entry-M5[-1][3]))/abs(entry-sl)) if fill else ('EXPIRED',0.0)

def trend_bos(b,k=TK):
    hs,ls=pivots(b,k)
    hs=set(hs);ls=set(ls)
    lastH=lastL=None;tr='none'
    for i in range(len(b)):
        # pivots at i become known at i+k
        j=i-k
        if j in hs: lastH=b[j][1]
        if j in ls: lastL=b[j][2]
        c=b[i][3]
        if lastH is not None and c>lastH: tr='up';lastH=None
        elif lastL is not None and c<lastL: tr='down';lastL=None
    return tr
trend=trend_bos
