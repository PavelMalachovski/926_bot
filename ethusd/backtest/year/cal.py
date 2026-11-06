import sys;sys.path.insert(0,'/tmp/claude-0/-home-user-926-bot/62a67582-ef00-5144-a021-fd06a5610889/scratchpad/hist')
import engine,run
from run import *
for tk in (2,3):
    engine.trend_bos.__defaults__=(tk,);run.trend=engine.trend_bos
    for k in (2,3,5):
        for pick in ('shallow','ote'):
            v=dict(k=k,pick=pick)
            os_=orders(v)
            evaluate(f'tk{tk} k{k} {pick} CAL',v,os_,(date(2026,10,4),date(2026,11,5)))
            evaluate(f'tk{tk} k{k} {pick} YEAR',v,os_)
