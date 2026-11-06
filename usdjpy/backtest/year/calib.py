from mech import *
import json
J={x['id']:x for x in json.load(open('/tmp/smc-state/usdjpy/trades.json'))}
recs=backtest({"v1"},"2026-10-05","2026-11-05")
print(stats(recs))
match=0
for r in recs:
    j=J.get(r['id'])
    same = j and j['side']==r['side'] and abs(j['entry']-r['entry'])<0.15
    match+=bool(same)
    print(r['id'],r['side'],r['basis'],r['entry'],r['sl'],r['tp1'],f"rr{r['rr1']:.2f}",r['res']['st'],r['res'].get('r'),"| журнал:",(j['side'],j['entry'],j['status']) if j else "-", "✓" if same else "")
print("совпало по стороне и входу ±15 п:",match,"из",len(recs),"; журнал:",len([k for k in J if '2026-10-05'<=k[:10]<='2026-11-05']))
jd=[k for k in J if '2026-10-05'<=k[:10]<='2026-11-05']
sides=sum(1 for r in recs if r['id'] in J and J[r['id']]['side']==r['side'])
print("та же сторона в тот же ран:",sides)
