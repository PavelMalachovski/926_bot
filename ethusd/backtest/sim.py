import json,os,sys
from datetime import datetime,timezone,timedelta
from zoneinfo import ZoneInfo
sys.path.insert(0,'..'); from lib import load
PR=ZoneInfo('Europe/Prague')
bars=[]
for f in ['a.json','b.json']:
    for r in json.load(open(f))['values']:
        t=datetime.strptime(r['datetime'],'%Y-%m-%d %H:%M:%S').replace(tzinfo=timezone.utc)
        bars.append(dict(t=t,o=float(r['open']),h=float(r['high']),l=float(r['low']),c=float(r['close'])))
d={b['t']:b for b in bars}; bars=sorted(d.values(),key=lambda b:b['t'])[:-1]
gaps=[(bars[i-1]['t'],bars[i]['t']) for i in range(1,len(bars)) if bars[i]['t']-bars[i-1]['t']>timedelta(minutes=5)]
# H1 from M5
h1={}
for b in bars:
    k=b['t'].replace(minute=0); x=h1.setdefault(k,dict(t=k,o=b['o'],h=b['h'],l=b['l'],c=b['c'],n=0)); x['h']=max(x['h'],b['h']);x['l']=min(x['l'],b['l']);x['c']=b['c'];x['n']+=1
S='/tmp/smc-state/ethusd/'
T=[r for r in json.load(open(S+'trades.json')) if r['id']!='2026-11-06-am']
def plan_sl(r):
    f=S+r['id'][:10]+('.json' if r['id'].endswith('am') else '-midday.json')
    try:
        j=json.load(open(f));o=j.get('order') or j.get('new_order')
        return float(o['sl'])
    except Exception: return None
d1=load('d1')
def piv(b,k=2):
    hs,ls=[],[]
    for i in range(k,len(b)-k):
        if all(b[i]['h']>b[j]['h'] for j in range(i-k,i+k+1) if j!=i): hs.append((i,b[i]['h']))
        if all(b[i]['l']<b[j]['l'] for j in range(i-k,i+k+1) if j!=i): ls.append((i,b[i]['l']))
    return hs,ls
def d1ctx(created):
    b=[x for x in d1 if x['t']+timedelta(days=1)<=created]   # closed days
    hs,ls=piv(b)
    tr='mixed'
    if len(hs)>=2 and len(ls)>=2:
        if hs[-1][1]<hs[-2][1] and ls[-1][1]<ls[-2][1]: tr='down'
        elif hs[-1][1]>hs[-2][1] and ls[-1][1]>ls[-2][1]: tr='up'
    # CHoCH by close beyond the last swing after it formed
    lh,ll=hs[-1],ls[-1]
    after=b[max(lh[0],ll[0])+1:]
    if tr!='up' and any(x['c']>lh[1] for x in b[lh[0]+1:]): tr='up'
    elif tr!='down' and any(x['c']<ll[1] for x in b[ll[0]+1:]): tr='down'
    hi=max(x['h'] for x in b[min(lh[0],ll[0]):]);lo=min(x['l'] for x in b[min(lh[0],ll[0]):])
    return tr,hi,lo
def run(side,entry,sl,tp,zl,zh,created,expires):
    buy=side.startswith('BUY');st='EXPIRED';fill=None;ca=None
    for b in bars:
        if b['t']<created: continue
        if fill is None:
            if b['t']>=expires: return dict(st='EXPIRED',ca=ca)
            if b['t'].minute==55:   # H1 close
                hb=h1[b['t'].replace(minute=0)]
                if (not buy and hb['c']>zh) or (buy and hb['c']<zl):
                    if not ((buy and b['l']<=entry) or ((not buy) and b['h']>=entry)): return dict(st='INVALIDATED',ca=ca)
            touch=b['l']<=entry if buy else b['h']>=entry
            dist=(b['l']-entry) if buy else (entry-b['h'])
            ca=dist if ca is None else min(ca,dist)
            if touch:
                fill=b['t']
                if (buy and b['l']<=sl) or ((not buy) and b['h']>=sl): return dict(st='SL',fill=fill,closed=b['t'],ca=0)
            continue
        hsl=(b['l']<=sl) if buy else (b['h']>=sl); htp=(b['h']>=tp) if buy else (b['l']<=tp)
        if hsl: return dict(st='SL',fill=fill,closed=b['t'],amb=htp,ca=0)
        if htp: return dict(st='TP',fill=fill,closed=b['t'],ca=0)
    return dict(st='OPEN',fill=fill,ca=ca)
