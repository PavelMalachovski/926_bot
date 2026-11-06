"""Механическая копия рутины USDJPY (v1 / v2.1 / ETH-v3) для длинного бэктеста.
Приближение дискреционной рутины, не она сама: тренд H4+H1 по BOS, плечо H1 от
непробитого swing до экстремума, зоны OB / FVG / OTE, SL за swing +10 п,
TP1 = ближайший незанятый пул (Asia/London/PDL-PDH/H1 swing), лимит до 23:59."""
import csv, bisect, sys
from datetime import datetime, timedelta
P=0.01
F=sys.argv[1] if len(sys.argv)>1 else "m5.csv"
m5=[(r['t'],float(r['o']),float(r['h']),float(r['l']),float(r['c'])) for r in csv.DictReader(open(F))]
T=[r[0] for r in m5]
def agg(key):
    out=[]; idx={}
    for t,o,h,l,c in m5:
        k=key(t)
        if k not in idx: idx[k]=len(out); out.append([k,o,h,l,c,t])
        else:
            b=out[idx[k]]; b[2]=max(b[2],h); b[3]=min(b[3],l); b[4]=c; b[5]=t
    return out   # [start, o,h,l,c, last_m5_start]
h1=agg(lambda t:t[:13]+":00")
def h4key(t):
    d=datetime.strptime(t[:13],"%Y-%m-%d %H")-timedelta(hours=1)
    return (d.replace(hour=d.hour//4*4)+timedelta(hours=1)).strftime("%Y-%m-%d %H:00")
h4=agg(h4key)
d1=[b for b in agg(lambda t:t[:10]) if datetime.strptime(b[0],"%Y-%m-%d").weekday()<5]
H1T=[b[0] for b in h1]; H4T=[b[0] for b in h4]; D1T=[b[0] for b in d1]
def pivots(b,n=2):
    hs=[];ls=[]   # (index, price, confirm_index)
    for i in range(n,len(b)-n):
        if all(b[i][2]>b[j][2] for j in range(i-n,i+n+1) if j!=i): hs.append((i,b[i][2],i+n))
        if all(b[i][3]<b[j][3] for j in range(i-n,i+n+1) if j!=i): ls.append((i,b[i][3],i+n))
    return hs,ls
def trend_arr(b,n=2):
    hs,ls=pivots(b,n); hi=None; lo=None; tr=0; out=[]; ph=0; pl=0
    for i in range(len(b)):
        while ph<len(hs) and hs[ph][2]<=i: hi=hs[ph][1]; ph+=1
        while pl<len(ls) and ls[pl][2]<=i: lo=ls[pl][1]; pl+=1
        c=b[i][4]
        if hi is not None and c>hi: tr=1; hi=None
        if lo is not None and c<lo: tr=-1; lo=None
        out.append(tr)
    return out
H1TR=trend_arr(h1); H4TR=trend_arr(h4)
STRICT_TREND=False
H1HS,H1LS=pivots(h1)
def closed_idx(TT,run,hours):
    # last bar whose close (start+hours) <= run
    lim=(datetime.strptime(run,"%Y-%m-%d %H:%M")-timedelta(hours=hours)).strftime("%Y-%m-%d %H:%M")
    return bisect.bisect_right(TT,lim)-1
def d1pos(day,price):
    i=bisect.bisect_left(D1T,day); prev=d1[max(0,i-20):i]
    hi=max(b[2] for b in prev); lo=min(b[3] for b in prev); return (price-lo)/(hi-lo)*100
def session_range(day,a,b):
    i=bisect.bisect_left(T,f"{day} {a}"); j=bisect.bisect_left(T,f"{day} {b}")
    if j<=i: return None
    return max(r[2] for r in m5[i:j]), min(r[3] for r in m5[i:j])
def setup(run):
    """Возвращает dict сетапа v1 или None."""
    i1=closed_idx(H1T,run,1); i4=closed_idx(H4T,run,4)
    if i1<50 or i4<20: return None
    s=H4TR[i4]
    if s==0: s=H1TR[i1]
    if s==0 or (STRICT_TREND and H1TR[i1]!=s): return None
    im=bisect.bisect_left(T,run)-1; px=m5[im][4]
    b=h1
    # плечо: последний подтверждённый swing (для SELL — high), не пробитый с тех пор
    piv=H1HS if s==-1 else H1LS
    cand=[p for p in piv if p[2]<=i1]
    leg=None
    for j,pr,_ in reversed(cand[-12:]):
        after=b[j+1:i1+1]
        if not after: continue
        if s==-1 and max(x[2] for x in after)<pr:
            ext=min(range(j+1,i1+1),key=lambda k:b[k][3]); leg=(j,pr,ext,b[ext][3]); break
        if s==1 and min(x[3] for x in after)>pr:
            ext=max(range(j+1,i1+1),key=lambda k:b[k][2]); leg=(j,pr,ext,b[ext][2]); break
    if not leg: return None
    j,top,k,bot=leg     # top = origin (swing), bot = extreme (для BUY наоборот по смыслу)
    size=abs(top-bot)/P
    if size<25: return None
    lvl=lambda f: bot+f*(top-bot)        # f от экстремума к origin
    ote=(lvl(0.62),lvl(0.79))
    frac=(px-bot)/(top-bot)
    if frac>=0.79: return None
    lo_,hi_=min(ote),max(ote)
    zones=[]
    # FVG внутри плеча (от origin до сейчас), заполнение < 50%
    for i in range(j+2,i1+1):
        if s==-1 and b[i][2]<b[i-2][3]: z=(b[i][2],b[i-2][3])
        elif s==1 and b[i][3]>b[i-2][2]: z=(b[i-2][2],b[i][3])
        else: continue
        after=b[i+1:i1+1]
        if after:
            if s==-1: pen=max(x[2] for x in after)-z[0]
            else: pen=z[1]-min(x[3] for x in after)
            if pen>0.5*(z[1]-z[0]): continue
        if (z[1]-z[0])/P<2: continue
        zones.append(("FVG",z,i))
    # OB: последняя противоположная свеча у origin
    ob=None
    for i in range(j,max(j-6,0),-1):
        o,c=b[i][1],b[i][4]
        if (s==-1 and c>o) or (s==1 and c<o): ob=(min(o,c),max(o,c)); break
    def inter(a,c): 
        lo=max(a[0],c[0]); hi=min(a[1],c[1]); return (lo,hi) if hi>lo else None
    def mk(basis,zone,entry,conf):
        return dict(run=run,side="SELL" if s==-1 else "BUY",s=s,basis=basis,conf=conf,zl=zone[0],zh=zone[1],
                    entry=round(entry,3),origin=top,px=px,leg_ext=bot)
    st=None
    # 1. FVG ∩ OTE (ближний к цене)
    fo=[(z,inter(z,(lo_,hi_))) for _,z,_ in zones if inter(z,(lo_,hi_))]
    if fo:
        z,it=min(fo,key=lambda a:abs((a[1][0]+a[1][1])/2-px))
        e=min(max(lvl(0.705),it[0]),it[1]); st=mk("FVG",z,e,True)
    elif ob and inter(ob,(min(lvl(0.5),top),max(lvl(0.5),top))):
        st=mk("OB",ob,(ob[0]+ob[1])/2,True)
    else:
        naked=[z for _,z,_ in zones if ((z[0]+z[1])/2-bot)/(top-bot)>=0.5]
        if naked:
            z=min(naked,key=lambda z:abs((z[0]+z[1])/2-px)); st=mk("FVG",z,(z[0]+z[1])/2,False)
        else:
            st=mk("OTE",(lo_,hi_),lvl(0.705),True)
    if (st['entry']-px)*s>=0: return None     # лимит должен быть по ту сторону цены
    st['sl']=round(top-s*0.10,3)
    # пулы ликвидности для TP
    day=run[:10]; pools=[]
    for a,bb in (("01:00","08:00"),("08:00","14:00")):
        if bb<=run[11:16]:
            r=session_range(day,a,bb)
            if r: pools.append(r[1] if s==-1 else r[0])
    id_=bisect.bisect_left(D1T,day)
    if id_>0: pools.append(d1[id_-1][3] if s==-1 else d1[id_-1][2])
    for p in (H1LS if s==-1 else H1HS):
        if p[2]<=i1 and p[0]>=i1-120:
            after=b[p[0]+1:i1+1]
            if s==-1 and after and min(x[3] for x in after)>p[1]: pools.append(p[1])
            if s==1 and after and max(x[2] for x in after)<p[1]: pools.append(p[1])
    pools.append(bot)
    # только непробитые с момента их появления — упрощение: строго за текущей ценой
    pools=sorted({round(p,3) for p in pools if (p-px)*s>0.03},key=lambda p:abs(px-p))
    if not pools: return None
    st['tp1']=pools[0]; st['tp2']=pools[1] if len(pools)>1 else None
    return st
def simulate(st,entry,sl,tp1,tp2=None,partial=False,split=0,hold_days=3,exp=None):
    s=st['s']; day=st['run'][:10]; exp=exp or day+" 23:59"
    end=(datetime.strptime(day,"%Y-%m-%d")+timedelta(days=hold_days)).strftime("%Y-%m-%d 23:59")
    legs=[entry]
    if split:
        e2=round(entry+s*split,3)
        if (st['px']-e2)*s>0: legs.append(e2)
    far=st['zh'] if s==-1 else st['zl']
    i0=bisect.bisect_left(T,st['run'])
    filled=set(); got1=False; cur=sl; ft=None; risk=lambda e:(e-sl)*s
    den=sum(risk(e) for e in legs)/len(legs)
    i=i0
    for i in range(i0,len(m5)):
        t,o,h,l,c=m5[i]
        if t>end: break
        if not filled and t>exp: return dict(st="EXPIRED")
        if t<=exp and not got1:
            for k,e in enumerate(legs):
                if k not in filled and ((l<=e) if s==1 else (h>=e)):
                    filled.add(k); ft=ft or t
        if not filled:
            if i+1<len(m5) and m5[i+1][0][:13]!=t[:13] and ((s==1 and c<far) or (s==-1 and c>far)):
                return dict(st="INVALIDATED")
            continue
        w=1/len(legs)
        slhit=(l<=cur) if s==1 else (h>=cur)
        if not got1:
            tph=(h>=tp1) if s==1 else (l<=tp1)
            if slhit: return dict(ft=ft,st="SL",r=round(sum(w*(sl-legs[k])*s for k in filled)/den,2),t=t)
            if tph:
                if not (partial and tp2):
                    return dict(ft=ft,st="TP",r=round(sum(w*(tp1-legs[k])*s for k in filled)/den,2),t=t)
                got1=True; cur=entry; r1=sum(w*(tp1-legs[k])*s for k in filled)/den
                continue
        else:
            if slhit: return dict(ft=ft,st="TP",r=round(0.5*r1,2),t=t)
            if (h>=tp2) if s==1 else (l<=tp2):
                return dict(ft=ft,st="TP",r=round(0.5*r1+0.5*sum(w*(tp2-legs[k])*s for k in filled)/den,2),t=t)
    last=m5[i][4]; fr=sum((1/len(legs))*(last-legs[k])*s for k in filled)/den
    if got1: return dict(ft=ft,st="TP",r=round(0.5*r1+0.5*fr,2),t=m5[i][0])
    return dict(ft=ft,st="OPEN_END",r=round(fr,2),t=m5[i][0])
def apply(st,rules):
    """rules: set из v1 / R1 R2 R3 R4 / P1 P2 → (params) или None"""
    s=st['s']; e=st['entry']; sl=st['sl']
    if "P1" in rules and not st['conf']: return None
    if "R2" in rules:
        near=st['zh'] if s==1 else st['zl']; e2=round(near+s*0.02,3)
        if (e2-e)*s>0 and (st['px']-e2)*s>0: e=e2
    risk=(e-sl)*s/P
    if risk<12: return None
    if "R3" in rules:
        if risk<22: sl=round(e-s*0.22,3)
        if (e-sl)*s/P>50: return None
    if "R1" in rules:
        pos=d1pos(st['run'][:10],e)
        if (s==1 and pos>55) or (s==-1 and pos<45): return None
    rr1=(st['tp1']-e)*s/((e-sl)*s)
    if rr1<(0.0 if "R4" in rules else 0.8): return None
    return dict(entry=e,sl=sl,tp1=st['tp1'],tp2=st['tp2'],partial="R4" in rules,split=0.08 if "P2" in rules else 0,rr1=rr1)
def backtest(rules,start,end):
    out=[]; d=datetime.strptime(start,"%Y-%m-%d")
    while d<=datetime.strptime(end,"%Y-%m-%d"):
        if d.weekday()<5:
            day=d.strftime("%Y-%m-%d"); am=None
            for hh in ("08:00","14:00"):
                run=f"{day} {hh}"
                if bisect.bisect_left(T,run)>=len(T)-1: break
                st=setup(run); p=apply(st,rules) if st else None
                if not p: continue
                if hh=="14:00" and am:
                    ft=am['res'].get('ft')
                    if ft and ft<run: continue          # утренний исполнен — второй ордер не ставим
                    if am['res']['st'] not in ("INVALIDATED",):
                        # утренний ещё висит → снимаем (CANCELLED), ставим новый
                        out.remove(am)
                res=simulate(st,p['entry'],p['sl'],p['tp1'],p['tp2'],p['partial'],p['split'])
                rec=dict(id=f"{day}-{'am' if hh=='08:00' else 'pm'}",side=st['side'],basis=st['basis'],conf=st['conf'],res=res,st=st,**p)
                if hh=="08:00": am=rec
                out.append(rec)
        d+=timedelta(days=1)
    return out
def stats(recs):
    fl=[r for r in recs if r['res']['st'] in ("TP","SL","OPEN_END")]
    tp=sum(r['res']['st']=="TP" for r in fl); sl=sum(r['res']['st']=="SL" for r in fl)
    R=[r['res']['r'] for r in fl]; eq=0; pk=0; dd=0; streak=0; mx=0
    for x in R:
        eq+=x; pk=max(pk,eq); dd=min(dd,eq-pk)
        streak=streak+1 if x<0 else 0; mx=max(mx,streak)
    n=len(recs)
    return dict(n=n,filled=len(fl),fill=round(len(fl)/n*100) if n else 0,tp=tp,sl=sl,
                wr=round(tp/(tp+sl)*100,1) if tp+sl else 0,R=round(sum(R),2),avg=round(sum(R)/len(R),2) if R else 0,
                dd=round(dd,2),streak=mx)
