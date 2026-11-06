exec(open('/tmp/claude-0/-home-user-926-bot/62a67582-ef00-5144-a021-fd06a5610889/scratchpad/hist/filters.py').read().split("tests=[")[0])
W=lambda o:o['wd'];R13=lambda o:o['risk']>=13;D25=lambda o:o['dist']<=25;AM=lambda o:o['tag']=='am';D40=lambda o:o['dist']<=40
C=[('будни+риск≥13',lambda o:W(o) and R13(o)),
   ('будни+риск≥13+дист≤25',lambda o:W(o) and R13(o) and D25(o)),
   ('будни+риск≥13+дист≤40',lambda o:W(o) and R13(o) and D40(o)),
   ('будни+риск≥13+am',lambda o:W(o) and R13(o) and AM(o)),
   ('будни+риск≥13+am+дист≤25',lambda o:W(o) and R13(o) and AM(o) and D25(o))]
for n,f in C:
    test(n,f);test(n,f,shift=8)
