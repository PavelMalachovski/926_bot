from sim import *
import json
rows=[]; base=[]; v2=[]
variants={"R2 только (край зоны)":("R2",),"R1 только (D1)":("R1",),"R3 только (SL≥22)":("R3",),"R4 только (50% TP1+BE→TP2)":("R4",),
          "R1+R2":("R1","R2"),"R1+R2+R3":("R1","R2","R3"),"v2 полный (R1–R4)":("R1","R2","R3","R4")}
out={}
b=[run(o,o['entry'],o['sl'],o['tp1'],o['tp2'],False) for o in orders]
out['v1']=summarize("v1 механически (как в журнале)",b)
for name,use in variants.items():
    res=[]
    for o in orders:
        p,why=v2params(o,use)
        if p is None: res.append(None); continue
        res.append(run(o,p[0],p[1],o['tp1'],o['tp2'],"R4" in use and o['tp2'] is not None))
    out[name]=summarize(name,res)
    if name.startswith("v2"):
        full=res
print()
for o,r1,r2 in zip(orders,b,full):
    p,why=v2params(o)
    print(f"{o['id']:14} {o['side']:4} журнал {o['journal']:11} | v1 {r1['st']:11} {str(r1.get('r')):6} | v2 ", (f"{r2['st']:11} {str(r2.get('r')):6} e{p[0]} sl{p[1]} {r2.get('run','')}" if r2 else f"НЕТ СЕТАПА ({why})"))
json.dump(out,open("summary.json","w"),ensure_ascii=False,indent=1)
