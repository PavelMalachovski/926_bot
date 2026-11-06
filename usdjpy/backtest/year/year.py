from mech import *
import json
V={"v1 (как сейчас)":{"v1"},
   "R2 край зоны":{"R2"},"R3 SL≥22":{"R3"},"R1 D1-фильтр":{"R1"},"R4 50%TP1→TP2":{"R4"},
   "R2+R3":{"R2","R3"},"R1+R2+R3":{"R1","R2","R3"},"v2.1 полный (R1–R4)":{"R1","R2","R3","R4"},
   "ETH П1 (без голого FVG)":{"P1"},"ETH П2 (сплит 8 п)":{"P2"},"ETH v3 (П1+П2)":{"P1","P2"}}
Q=[("2025-11-06","2026-01-31"),("2026-02-01","2026-04-30"),("2026-05-01","2026-07-31"),("2026-08-01","2026-11-05")]
out={}
print(f"{'вариант':26} {'ордер':>5} {'fill':>5} {'TP':>4} {'SL':>4} {'WR':>6} {'ΣR':>7} {'avgR':>6} {'maxDD':>6} {'серия':>5} | WR по кварталам / ΣR")
for name,rules in V.items():
    recs=backtest(rules,"2025-11-06","2026-11-05"); s=stats(recs)
    qs=[stats([r for r in recs if a<=r['id'][:10]<=b]) for a,b in Q]
    out[name]=dict(total=s,quarters=qs)
    print(f"{name:26} {s['n']:5} {s['fill']:4}% {s['tp']:4} {s['sl']:4} {s['wr']:5}% {s['R']:+7.2f} {s['avg']:+6.2f} {s['dd']:6.2f} {s['streak']:5} | "+"  ".join(f"{q['wr']:.0f}%/{q['R']:+.1f}" for q in qs))
json.dump(out,open("year.json","w"),ensure_ascii=False,indent=1)
