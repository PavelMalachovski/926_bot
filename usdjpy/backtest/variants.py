import sim
from sim import *
def gen(off=0.02,d1thr=55,minsl=22,rrmin=1.0,partial=True,r1=True,r2=True,r3=True):
    res=[]
    for o in orders:
        s=1 if o['side']=='BUY' else -1; e=o['entry']; sl=o['sl']
        if r2:
            near=o['zh'] if s==1 else o['zl']; e2=round(near+s*off,3)
            if (e2-e)*s>0 and (o['px']-e2)*s>0: e=e2
        risk=(e-sl)*s/P
        if r3:
            if risk<minsl: sl=round(e-s*minsl*P,3)
            if (e-sl)*s/P>50: res.append(None); continue
        if r1:
            pos=d1pos(o['created'][:10],e)
            if (s==1 and pos>d1thr) or (s==-1 and pos<100-d1thr): res.append(None); continue
        rr1=(o['tp1']-e)*s/((e-sl)*s)
        if rr1<rrmin: res.append(None); continue
        res.append(run(o,e,sl,o['tp1'],o['tp2'],partial and o['tp2'] is not None))
    return res
print("--- R4: доля частичной фиксации и порог RR")
summarize("R1-3, полный TP1, RR≥0",gen(rrmin=0,partial=False))
summarize("R1-3, 50% TP1→TP2, RR≥0",gen(rrmin=0,partial=True))
summarize("R1-3, 50% TP1→TP2, RR≥0.7",gen(rrmin=0.7,partial=True))
summarize("R1-3, 50% TP1→TP2, RR≥1.0 (v2)",gen())
print("--- чувствительность D1-порога (R1-3, partial, RR≥0.7)")
for thr in (50,55,60,65,70): summarize(f"D1 порог {thr}%",gen(d1thr=thr,rrmin=0.7))
print("--- упреждение R2 (R1-3, partial, RR≥0.7)")
for off in (0,0.02,0.04): summarize(f"упреждение {off*100:.0f} п",gen(off=off,rrmin=0.7))
print("--- без D1 (R2+R3+partial, RR≥0.7) — на всех 35")
summarize("R2+R3+partial, без R1",gen(r1=False,rrmin=0.7))
summarize("R2+R3, полный TP1, без R1",gen(r1=False,rrmin=0,partial=False))
