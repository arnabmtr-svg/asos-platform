"""
tune.py — Parameter tuning for A1 Iron Condor
Tests combinations of: short delta, profit target %, stop multiple, regime-flip exit
Finds the config with best expectancy + acceptable drawdown.
All [MODELED] via Black-Scholes.

Run from backend:  venv\\Scripts\\python.exe tune.py
"""
import math
from datetime import date

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
def find_delta_strike(S,T,r,sig,td,opt,step=50):
    best,bd=S,999
    for K in range(int(S*0.82),int(S*1.18),step):
        d=abs(bs_delta(S,K,T,r,sig,opt))
        if abs(d-td)<bd: bd=abs(d-td); best=K
    return best

def _rvol(closes,days):
    if len(closes)<days+1: return 15.0
    rets=[math.log(closes[i]/closes[i-1]) for i in range(len(closes)-days,len(closes))]
    m=sum(rets)/len(rets); sd=math.sqrt(sum((x-m)**2 for x in rets)/len(rets))
    return sd*math.sqrt(252)*100

def backtest_config(bars, ie, short_delta, pt_pct, stop_x, wing, regime_flip_exit):
    """Run one config, return stats."""
    from backtest import _quick_features
    closes=[b["close"] for b in bars]
    r=0.065; trades=[]; i=60
    while i < len(bars)-35:
        feat=_quick_features(bars[:i+1], ie)
        reg=ie.classify_regime(feat)["regime"]
        if reg!="RANGE": i+=1; continue
        S=closes[i]; iv=_rvol(closes[:i+1],20)/100
        if iv<=0 or iv>1: i+=1; continue
        T=30/365
        sc=find_delta_strike(S,T,r,iv,short_delta,"CE")
        sp=find_delta_strike(S,T,r,iv,short_delta,"PE")
        lc=sc+wing; lp=sp-wing
        credit=(bs_price(S,sc,T,r,iv,"CE")-bs_price(S,lc,T,r,iv,"CE")
                +bs_price(S,sp,T,r,iv,"PE")-bs_price(S,lp,T,r,iv,"PE"))
        credit_rs=credit*65
        if credit_rs<=0: i+=1; continue
        outcome=None
        for j in range(i+1,min(i+31,len(bars))):
            Sj=closes[j]; Tj=max((30-(j-i))/365,1/365)
            # regime-flip exit check
            if regime_flip_exit:
                fj=_quick_features(bars[:j+1], ie)
                if ie.classify_regime(fj)["regime"]=="VOL_EXPANSION":
                    val=(bs_price(Sj,sc,Tj,r,iv,"CE")-bs_price(Sj,lc,Tj,r,iv,"CE")
                         +bs_price(Sj,sp,Tj,r,iv,"PE")-bs_price(Sj,lp,Tj,r,iv,"PE"))
                    pnl=(credit-val)*65
                    outcome=("FLIP_EXIT",pnl,j); break
            val=(bs_price(Sj,sc,Tj,r,iv,"CE")-bs_price(Sj,lc,Tj,r,iv,"CE")
                 +bs_price(Sj,sp,Tj,r,iv,"PE")-bs_price(Sj,lp,Tj,r,iv,"PE"))
            pnl=(credit-val)*65
            if pnl>=credit_rs*pt_pct/100: outcome=("WIN",pnl,j); break
            if pnl<=-credit_rs*stop_x: outcome=("LOSS",pnl,j); break
        if outcome is None:
            Sf=closes[min(i+30,len(bars)-1)]
            val=max(0,Sf-sc)-max(0,Sf-lc)+max(0,sp-Sf)-max(0,lp-Sf)
            pnl=(credit-val)*65; outcome=("EXPIRE",pnl,i+30)
        trades.append(outcome[1]); i=outcome[2]+1
    if not trades: return None
    wins=[t for t in trades if t>0]; losses=[t for t in trades if t<=0]
    wr=len(wins)/len(trades)*100
    aw=sum(wins)/len(wins) if wins else 0
    al=sum(losses)/len(losses) if losses else 0
    exp=wr/100*aw+(1-wr/100)*al
    cum=0;peak=0;maxdd=0
    for t in trades: cum+=t;peak=max(peak,cum);maxdd=min(maxdd,cum-peak)
    return {"trades":len(trades),"wr":wr,"aw":aw,"al":al,"exp":exp,
            "total":sum(trades),"maxdd":maxdd}

def main():
    import income_engine as ie
    from database import get_db
    db=get_db(); uid=db.execute("SELECT id FROM users LIMIT 1").fetchone()["id"]
    bars=ie._ohlc("NIFTY",500,uid)
    if not bars or len(bars)<100:
        print("Need Kite connected"); return
    print("="*70)
    print("A1 IRON CONDOR PARAMETER TUNING [MODELED]")
    print(f"On {len(bars)} days of real Nifty history")
    print("="*70)

    configs=[
        # (label, short_delta, pt%, stop_x, wing, regime_flip_exit)
        ("BASELINE (current)",       0.16, 50, 2.0, 250, False),
        ("Tight stop 1x",            0.16, 50, 1.0, 250, False),
        ("Tight stop 1x + early PT", 0.16, 40, 1.0, 250, False),
        ("Far OTM 12d + stop 1x",    0.12, 50, 1.0, 300, False),
        ("Far OTM 10d + stop 1x",    0.10, 50, 1.0, 300, False),
        ("12d + flip-exit + stop1x", 0.12, 50, 1.0, 300, True),
        ("10d + flip-exit + PT40",   0.10, 40, 1.0, 300, True),
        ("12d + flip-exit + PT35",   0.12, 35, 0.8, 300, True),
    ]
    print(f"\n{'Config':<28}{'Trades':<8}{'Win%':<7}{'AvgWin':<9}{'AvgLoss':<10}{'Expect':<9}{'MaxDD':<10}")
    print("-"*81)
    results=[]
    for label,sd,pt,sx,wing,flip in configs:
        r=backtest_config(bars,ie,sd,pt,sx,wing,flip)
        if not r:
            print(f"{label:<28}no trades"); continue
        results.append((label,r,(sd,pt,sx,wing,flip)))
        print(f"{label:<28}{r['trades']:<8}{r['wr']:<7.1f}{r['aw']:<9.0f}{r['al']:<10.0f}{r['exp']:<9.0f}{r['maxdd']:<10.0f}")

    # pick best by expectancy among positive, then lowest drawdown
    pos=[x for x in results if x[1]["exp"]>0]
    print("\n"+"="*70)
    if pos:
        best=max(pos,key=lambda x:x[1]["exp"])
        label,r,params=best
        sd,pt,sx,wing,flip=params
        print(f"BEST CONFIG: {label}")
        print(f"  Expectancy Rs {r['exp']:.0f}/trade · Win {r['wr']:.0f}% · MaxDD Rs {r['maxdd']:.0f}")
        print(f"  Params: short_delta={sd}, pt_pct={pt}, stop_x={sx}, wing={wing}, regime_flip_exit={flip}")
        monthly=r["total"]/(len(bars)/21)
        print(f"  Est monthly: Rs {monthly:.0f}")
        print(f"\n  -> These become A1's default params in income_engine.py")
    else:
        print("NO config produced positive expectancy on this period.")
        print("This means: in the last ~16 months, short-premium ICs on Nifty were")
        print("structurally hard (low IV, trending tape). Options:")
        print("  1. Trade A1 only when IVR>40 (rich premium) — add entry filter")
        print("  2. Favor directional arms (A3 bull-put) in trending regimes")
        print("  3. The regime engine already knows this — let it steer arm selection")
        # show least-bad
        best=max(results,key=lambda x:x[1]["exp"])
        print(f"\n  Least-bad: {best[0]} at Rs {best[1]['exp']:.0f}/trade")

    print("\n[MODELED] caveat holds — real fills differ. Paper-trade to confirm.")

if __name__=="__main__":
    main()
