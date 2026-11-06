import sys;sys.path.insert(0,'/tmp/claude-0/-home-user-926-bot/62a67582-ef00-5144-a021-fd06a5610889/scratchpad/hist')
import engine,run
from run import *
engine.trend_bos.__defaults__=(2,);run.trend=engine.trend_bos
for k in (5,3):
    print(f'=== leg pivot k={k}')
    base=dict(k=k);os_all=orders(base);os_conf=orders(dict(k=k,conf=True))
    from collections import Counter;print('naked share',Counter(o['conf'] for o in os_all))
    evaluate('A v1 (как есть)',base,os_all)
    evaluate('B П1 конфлюэнс',dict(k=k,conf=True),os_conf)
    evaluate('П2 сплит 8$',dict(k=k,shift=8),os_all)
    evaluate('C v3 = П1+сплит 8$',dict(k=k,conf=True,shift=8),os_conf)
    evaluate('v3 сплит 0.4R',dict(k=k,conf=True,shift='r'),os_conf)
    Q=[(date(2025,11,1),date(2026,1,31)),(date(2026,2,1),date(2026,4,30)),(date(2026,5,1),date(2026,7,31)),(date(2026,8,1),date(2026,11,5))]
    for q in Q:
        evaluate(f'  A {q[0]:%m.%y}-{q[1]:%m.%y}',base,os_all,q)
        evaluate(f'  C {q[0]:%m.%y}-{q[1]:%m.%y}',dict(k=k,conf=True,shift=8),os_conf,q)
