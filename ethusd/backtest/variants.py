import sys;sys.path.insert(0,'.')
from sim import *
NAKED={'2026-10-08-pm','2026-10-11-pm','2026-10-13-am','2026-10-16-am','2026-10-17-pm','2026-10-22-am','2026-10-22-pm','2026-10-28-am','2026-10-29-pm','2026-11-02-am','2026-11-05-am'}
rows=[]
for r in T:
    c=datetime.fromisoformat(r['created_at']);e=datetime.fromisoformat(r['expires'])
    tr,hi,lo=d1ctx(c);pd=max(0,min(100,100*(r['price_at_creation']-lo)/(hi-lo)))
    rows.append(dict(r=r,c=c,e=e,sl=plan_sl(r) or r['sl'],tr=tr,pd=pd,buy=r['side'].startswith('BUY')))
def leg(x,entry):
    r=x['r'];buy=x['buy'];p=r['price_at_creation']
    if (buy and entry>=p) or ((not buy) and entry<=p): entry=p
    risk=abs(entry-x['sl']);
    if risk<=0: return None
    o=run(r['side'],entry,x['sl'],r['tp1'],r['zone_low'],r['zone_high'],x['c'],x['e'])
    rr=abs(r['tp1']-entry)/risk
    o['R']={'TP':rr,'SL':-1}.get(o['st'],0.0);o['usd']=o['R']*risk;o['risk']=risk;o['entry']=entry;o['rr']=rr
    return o
def near(x): r=x['r']; return r['zone_high'] if x['buy'] else r['zone_low']
def shift(x,k): r=x['r']; return r['entry']+k if x['buy'] else r['entry']-k
def evaluate(name,skip=lambda x:False,entry=lambda x:x['r']['entry'],split=None,cap=None,show=False):
    res=[]
    for x in rows:
        if skip(x): continue
        en=entry(x)
        if cap and abs(x['r']['price_at_creation']-en)>cap: continue
        if split:
            a=leg(x,en);b=leg(x,split(x))
            if a is None or b is None: continue
            # 0.5 ETH each, R vs combined risk of 1 ETH avg
            fills=[o for o in (a,b) if o['st'] in('TP','SL','OPEN')]
            usd=0.5*a['usd']+0.5*b['usd']; risk=0.5*a['risk']+0.5*b['risk']
            st='FILL' if fills else a['st']
            res.append(dict(id=x['r']['id'],st=st,R=usd/risk,usd=usd,detail=(a['st'],b['st']),tp=a['st']=='TP' or b['st']=='TP',sl=any(o['st']=='SL' for o in fills) and not any(o['st']=='TP' for o in fills)))
        else:
            o=leg(x,en)
            if o is None: continue
            res.append(dict(id=x['r']['id'],st=o['st'],R=o['R'],usd=o['usd'],rr=o['rr'],tp=o['st']=='TP',sl=o['st']=='SL'))
    n=len(res);filled=[q for q in res if q['st'] in('TP','SL','OPEN','FILL')]
    tp=sum(q['tp'] for q in filled);sl=sum(q['sl'] for q in filled)
    R=sum(q['R'] for q in res);usd=sum(q['usd'] for q in res)
    print(f"{name:44s} ord {n:2d} fill {len(filled):2d} ({100*len(filled)/max(n,1):4.0f}%) TP {tp:2d} SL {sl:2d} WR {100*tp/max(tp+sl,1):4.0f}% ΣR {R:+6.2f} R/ord {R/max(n,1):+.2f} Σ$ {usd:+7.1f}")
    if show:
        for q in res: print('   ',q['id'],q['st'],round(q['R'],2),q.get('rr') and round(q['rr'],2),q.get('detail',''))
    return res
