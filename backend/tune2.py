"""
tune2.py — The real edge test:
  A) A1 Iron Condor but ONLY when IV is rich (IVR filter) — proves "sell premium when expensive"
  B) A3 Bull Put Spread in TREND_UP — does directional theta work in trending tape?
Run from backend: venv\\Scripts\\python.exe tune2.py
"""
import math

def _norm_cdf(x): return 0.5*(1+math.erf(x/math.sqrt(2)))
def bs_price(S,K,T,r,sig,opt="CE"):
    if T<=0 or sig<=0: return max(0,S-K) if opt=="CE" else max(0,K-S)
    d1=(math.log(S/K)+(r+sig**2/2)*T)/(sig*math.sqrt(T)); d2=d1-sig*math.sqrt(T)
    if opt=="CE": return S*_norm_cdf(d1)-K*math.exp(-r*T)*_norm_cdf(d2)
    return K*math.exp(-r*T)*_norm_cdf(-d2)-S*_norm_cdf(-d1)
def bs_delta(S,K,T,r,sig,opt="CE"):
    if T<=0 or sig<=0: return 0.5
    d1=(math.log(S/K)+(r+sig**2/2)*T)/(sig*math.sqrt(T))
    return _norm_cdf(d1) if opt=="CE" else _norm_cdf(d1)-1
def fds(S,T,r,sig,td,opt,step=50):
    best,bd=S,999
    for K in range(int(S*0.80),int(S*1.20),step):
        d=abs(bs_delta(S,K,T,r,sig,opt))
        if abs(d-td)<bd: bd=abs(d-td);best=K
    return best
def _rvol(closes,days):
    if len(closes)<days+1: return 15.0
    rets=[math.log(closes[i]/closes[i-1]) for i in range(len(closes)-days,len(closes))]
    m=sum(rets)/len(rets); sd=math.sqrt(sum((x-m)**2 for x in rets)/len(rets))
    return sd*math.sqrt(252)*100
def _ivr(closes, i, lookback=180):
    """IVR proxy from realized vol percentile at day i."""
    series=[_rvol(closes[:j],10) for j in range(max(30,i-lookback),i)]
    if not series: return 50
    cur=_rvol(closes[:i],10)
    return sum(1 for x in series if x<cur)/len(series)*100

def stats(trades):
    if not trades: return None
    wins=[t for t in trades if t>0]; losses=[t for t in trades if t<=0]
    wr=len(wins)/len(trades)*100
    aw=sum(wins)/len(wins) if wins else 0; al=sum(losses)/len(losses) if losses else 0
    exp=wr/100*aw+(1-wr/100)*al
    cum=0;peak=0;dd=0
    for t in trades: cum+=t;peak=max(peak,cum);dd=min(dd,cum-peak)
    return {"n":len(trades),"wr":wr,"aw":aw,"al":al,"exp":exp,"total":sum(trades),"dd":dd}

def test_ic_ivr(bars, ie, ivr_min):
    """A1 IC only when IVR >= ivr_min."""
    from backtest import _quick_features
    closes=[b["close"] for b in bars]; r=0.065; trades=[]; i=60
    while i<len(bars)-35:
        reg=ie.classify_regime(_quick_features(bars[:i+1],ie))["regime"]
        if reg!="RANGE": i+=1; continue
        if _ivr(closes,i) < ivr_min: i+=1; continue
        S=closes[i]; iv=_rvol(closes[:i+1],20)/100
        if iv<=0 or iv>1: i+=1; continue
        T=30/365
        sc=fds(S,T,r,iv,0.16,"CE"); sp=fds(S,T,r,iv,0.16,"PE"); lc=sc+250; lp=sp-250
        credit=(bs_price(S,sc,T,r,iv,"CE")-bs_price(S,lc,T,r,iv,"CE")
                +bs_price(S,sp,T,r,iv,"PE")-bs_price(S,lp,T,r,iv,"PE")); cr=credit*65
        if cr<=0: i+=1; continue
        out=None
        for j in range(i+1,min(i+31,len(bars))):
            Sj=closes[j]; Tj=max((30-(j-i))/365,1/365)
            val=(bs_price(Sj,sc,Tj,r,iv,"CE")-bs_price(Sj,lc,Tj,r,iv,"CE")
                 +bs_price(Sj,sp,Tj,r,iv,"PE")-bs_price(Sj,lp,Tj,r,iv,"PE"))
            pnl=(credit-val)*65
            if pnl>=cr*0.5: out=(pnl,j);break
            if pnl<=-cr*1.0: out=(pnl,j);break
        if out is None:
            Sf=closes[min(i+30,len(bars)-1)]
            val=max(0,Sf-sc)-max(0,Sf-lc)+max(0,sp-Sf)-max(0,lp-Sf); out=((credit-val)*65,i+30)
        trades.append(out[0]); i=out[1]+1
    return stats(trades)

def test_bull_put(bars, ie):
    """A3 Bull Put Spread in TREND_UP."""
    from backtest import _quick_features
    closes=[b["close"] for b in bars]; r=0.065; trades=[]; i=60
    while i<len(bars)-35:
        reg=ie.classify_regime(_quick_features(bars[:i+1],ie))["regime"]
        if reg!="TREND_UP": i+=1; continue
        S=closes[i]; iv=_rvol(closes[:i+1],20)/100
        if iv<=0 or iv>1: i+=1; continue
        T=30/365
        sp=fds(S,T,r,iv,0.30,"PE"); lp=sp-200   # sell 30d put, buy protection
        credit=(bs_price(S,sp,T,r,iv,"PE")-bs_price(S,lp,T,r,iv,"PE")); cr=credit*65
        if cr<=0: i+=1; continue
        out=None
        for j in range(i+1,min(i+31,len(bars))):
            Sj=closes[j]; Tj=max((30-(j-i))/365,1/365)
            val=bs_price(Sj,sp,Tj,r,iv,"PE")-bs_price(Sj,lp,Tj,r,iv,"PE")
            pnl=(credit-val)*65
            if pnl>=cr*0.5: out=(pnl,j);break
            if pnl<=-cr*1.5: out=(pnl,j);break
        if out is None:
            Sf=closes[min(i+30,len(bars)-1)]
            val=max(0,sp-Sf)-max(0,lp-Sf); out=((credit-val)*65,i+30)
        trades.append(out[0]); i=out[1]+1
    return stats(trades)

def main():
    import income_engine as ie
    from database import get_db
    db=get_db(); uid=db.execute("SELECT id FROM users LIMIT 1").fetchone()["id"]
    bars=ie._ohlc("NIFTY",500,uid)
    if not bars or len(bars)<100: print("Need Kite"); return
    print("="*70)
    print("THE REAL EDGE TEST [MODELED]")
    print(f"On {len(bars)} days real Nifty history")
    print("="*70)

    print("\nA) IRON CONDOR — only when IV is RICH (the key filter)")
    print(f"{'IVR filter':<18}{'Trades':<8}{'Win%':<7}{'AvgWin':<9}{'AvgLoss':<10}{'Expect':<9}{'Total':<10}")
    print("-"*71)
    for ivr_min in [0, 30, 40, 50, 60, 70]:
        s=test_ic_ivr(bars, ie, ivr_min)
        label=f"IVR >= {ivr_min}" if ivr_min else "no filter"
        if not s: print(f"{label:<18}no trades"); continue
        flag=" <-- POSITIVE" if s["exp"]>0 else ""
        print(f"{label:<18}{s['n']:<8}{s['wr']:<7.0f}{s['aw']:<9.0f}{s['al']:<10.0f}{s['exp']:<9.0f}{s['total']:<10.0f}{flag}")

    print("\nB) BULL PUT SPREAD (A3) — directional theta in TREND_UP")
    s=test_bull_put(bars, ie)
    if s:
        flag=" <-- POSITIVE" if s["exp"]>0 else ""
        print(f"{'A3 bull-put':<18}{s['n']:<8}{s['wr']:<7.0f}{s['aw']:<9.0f}{s['al']:<10.0f}{s['exp']:<9.0f}{s['total']:<10.0f}{flag}")
    else:
        print("  No TREND_UP trades")

    print("\n"+"="*70)
    print("INTERPRETATION")
    print("="*70)
    print("  If IC turns positive at high IVR -> ship rule: A1 only fires when IVR>=threshold")
    print("  If A3 bull-put is positive -> trending tape rewards directional theta")
    print("  Together: regime engine steers to the RIGHT arm for the RIGHT market")
    print("  [MODELED] — paper-trade to confirm real fills")

if __name__=="__main__": main()
