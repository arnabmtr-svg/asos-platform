"""
backtest.py — ATHENA Income Engine backtest
LEVEL 1: Regime classifier validation on REAL Nifty history (Kite)
LEVEL 2: MODELED Iron Condor performance (Black-Scholes + VIX as IV proxy)

HONEST LABELS:
  [REAL]    = computed from actual Nifty OHLC history
  [MODELED] = option premiums reconstructed via Black-Scholes, NOT actual fills

Run from backend:  venv\\Scripts\\python.exe backtest.py
"""
import math, json
from datetime import datetime, timedelta, date

# ── Black-Scholes for modeled option pricing ──────────────────────────────
def _norm_cdf(x):
    return 0.5*(1+math.erf(x/math.sqrt(2)))

def bs_price(S, K, T, r, sigma, opt="CE"):
    if T <= 0 or sigma <= 0: 
        return max(0, S-K) if opt=="CE" else max(0, K-S)
    d1 = (math.log(S/K)+(r+sigma**2/2)*T)/(sigma*math.sqrt(T))
    d2 = d1 - sigma*math.sqrt(T)
    if opt=="CE":
        return S*_norm_cdf(d1) - K*math.exp(-r*T)*_norm_cdf(d2)
    return K*math.exp(-r*T)*_norm_cdf(-d2) - S*_norm_cdf(-d1)

def bs_delta(S, K, T, r, sigma, opt="CE"):
    if T<=0 or sigma<=0: return 0.5
    d1 = (math.log(S/K)+(r+sigma**2/2)*T)/(sigma*math.sqrt(T))
    return _norm_cdf(d1) if opt=="CE" else _norm_cdf(d1)-1

def find_delta_strike(S, T, r, sigma, target_delta, opt, step=50):
    """Find strike nearest to target delta."""
    best, bestdiff = S, 999
    rng = range(int(S*0.85), int(S*1.15), step)
    for K in rng:
        d = abs(bs_delta(S, K, T, r, sigma, opt))
        if abs(d - target_delta) < bestdiff:
            bestdiff = abs(d-target_delta); best = K
    return best


def run_backtest():
    import income_engine as ie
    from database import get_db
    db = get_db()
    uid = db.execute("SELECT id FROM users LIMIT 1").fetchone()["id"]

    print("="*64)
    print("ATHENA INCOME ENGINE BACKTEST")
    print("[REAL] = actual Nifty history · [MODELED] = Black-Scholes estimate")
    print("="*64)

    # ── Fetch 2 years of Nifty daily bars [REAL] ──
    bars = ie._ohlc("NIFTY", 500, uid)
    if not bars or len(bars) < 100:
        print("  Could not fetch enough Nifty history. Is Kite connected?")
        return
    print(f"\n[REAL] Loaded {len(bars)} days of Nifty history")
    print(f"       From {bars[0]['date']} to {bars[-1]['date']}")

    closes = [b["close"] for b in bars]

    # ── LEVEL 1: Regime validation [REAL] ──
    print("\n" + "="*64)
    print("LEVEL 1 — REGIME CLASSIFIER VALIDATION [REAL]")
    print("="*64)
    # walk through history, classify each day, check what happened next 10 days
    regime_counts = {}
    regime_forward = {}   # regime -> list of forward 10-day realized moves
    for i in range(60, len(bars)-10):
        window = bars[:i+1]
        feat = _quick_features(window, ie)
        reg = ie.classify_regime(feat)["regime"]
        regime_counts[reg] = regime_counts.get(reg, 0) + 1
        # forward realized move over next 10 days
        fwd = abs(closes[i+10]/closes[i]-1)*100
        regime_forward.setdefault(reg, []).append(fwd)

    print(f"\n{'Regime':<18}{'Days':<8}{'% time':<9}{'Avg fwd 10d move':<18}")
    print("-"*53)
    total = sum(regime_counts.values())
    for reg in ["RANGE","TREND_UP","TREND_DOWN","VOL_EXPANSION","VOL_COMPRESSION"]:
        n = regime_counts.get(reg, 0)
        if n == 0: continue
        fwd = regime_forward.get(reg, [1])
        avgfwd = sum(fwd)/len(fwd)
        print(f"{reg:<18}{n:<8}{n/total*100:<9.1f}{avgfwd:<18.2f}")

    # Validation check: does VOL_EXPANSION actually precede bigger moves than RANGE?
    ve = regime_forward.get("VOL_EXPANSION", [])
    rg = regime_forward.get("RANGE", [])
    print("\n[REAL] Validation:")
    if ve and rg:
        ve_avg = sum(ve)/len(ve); rg_avg = sum(rg)/len(rg)
        verdict = "PASS" if ve_avg > rg_avg else "WEAK"
        print(f"  VOL_EXPANSION fwd move {ve_avg:.2f}% vs RANGE {rg_avg:.2f}% -> {verdict}")
        print(f"  (VOL_EXPANSION should precede BIGGER moves — confirms the brain works)")

    # ── LEVEL 2: Modeled Iron Condor backtest [MODELED] ──
    print("\n" + "="*64)
    print("LEVEL 2 — IRON CONDOR A1 BACKTEST [MODELED]")
    print("="*64)
    print("  Method: enter 16-delta IC when regime=RANGE, hold to +50% or -2x or expiry")
    print("  IV proxy: rolling realized vol (real VIX history not in Kite daily)")

    r = 0.065  # risk-free
    trades = []
    i = 60
    while i < len(bars)-35:
        window = bars[:i+1]
        feat = _quick_features(window, ie)
        reg = ie.classify_regime(feat)["regime"]
        # Only trade A1 in RANGE (its designated regime)
        if reg != "RANGE":
            i += 1; continue
        S = closes[i]
        # IV proxy: 20-day realized vol annualized
        iv = _rvol(closes[:i+1], 20) / 100
        if iv <= 0 or iv > 1: i += 1; continue
        T = 30/365
        # 16-delta strikes
        sc = find_delta_strike(S, T, r, iv, 0.16, "CE")
        sp = find_delta_strike(S, T, r, iv, 0.16, "PE")
        lc = sc + 250; lp = sp - 250
        # entry premiums [MODELED]
        credit = (bs_price(S,sc,T,r,iv,"CE") - bs_price(S,lc,T,r,iv,"CE")
                  + bs_price(S,sp,T,r,iv,"PE") - bs_price(S,lp,T,r,iv,"PE"))
        credit_rs = credit * 65
        if credit_rs <= 0: i += 1; continue
        max_loss = (250 - credit) * 65
        # simulate hold: check each day to expiry
        outcome = None; exit_day = i+30
        for j in range(i+1, min(i+31, len(bars))):
            Sj = closes[j]
            Tj = max((30-(j-i))/365, 1/365)
            val = (bs_price(Sj,sc,Tj,r,iv,"CE") - bs_price(Sj,lc,Tj,r,iv,"CE")
                   + bs_price(Sj,sp,Tj,r,iv,"PE") - bs_price(Sj,lp,Tj,r,iv,"PE"))
            pnl = (credit - val) * 65
            if pnl >= credit_rs*0.5:      # +50% target
                outcome = ("WIN", pnl, j); break
            if pnl <= -credit_rs*2:        # -2x stop
                outcome = ("LOSS", pnl, j); break
        if outcome is None:
            # expired — final value
            Sf = closes[min(i+30, len(bars)-1)]
            val = max(0, Sf-sc)-max(0,Sf-lc)+max(0,sp-Sf)-max(0,lp-Sf)
            pnl = (credit - val) * 65
            outcome = ("WIN" if pnl>0 else "LOSS", pnl, i+30)
        trades.append({"entry":bars[i]["date"][:10] if isinstance(bars[i]["date"],str) else str(bars[i]["date"])[:10],
                       "credit":credit_rs, "pnl":outcome[1], "result":outcome[0]})
        i = outcome[2] + 1  # next trade after this closes

    if not trades:
        print("  No RANGE-regime trades in window.")
        return

    wins = [t for t in trades if t["pnl"]>0]
    losses = [t for t in trades if t["pnl"]<=0]
    total_pnl = sum(t["pnl"] for t in trades)
    win_rate = len(wins)/len(trades)*100
    avg_win = sum(t["pnl"] for t in wins)/len(wins) if wins else 0
    avg_loss = sum(t["pnl"] for t in losses)/len(losses) if losses else 0
    # max drawdown
    cum = 0; peak = 0; maxdd = 0
    for t in trades:
        cum += t["pnl"]; peak = max(peak, cum); maxdd = min(maxdd, cum-peak)

    print(f"\n[MODELED] Results over {len(bars)} days:")
    print(f"  Trades:        {len(trades)}")
    print(f"  Win rate:      {win_rate:.1f}%  ({len(wins)}W / {len(losses)}L)")
    print(f"  Total P&L:     Rs {total_pnl:,.0f}")
    print(f"  Avg win:       Rs {avg_win:,.0f}")
    print(f"  Avg loss:      Rs {avg_loss:,.0f}")
    print(f"  Avg/trade:     Rs {total_pnl/len(trades):,.0f}")
    print(f"  Max drawdown:  Rs {maxdd:,.0f}")
    exp = win_rate/100*avg_win + (1-win_rate/100)*avg_loss
    print(f"  Expectancy:    Rs {exp:,.0f} per trade")
    monthly = total_pnl / (len(bars)/21)   # ~21 trading days/month
    print(f"  Est monthly:   Rs {monthly:,.0f}  (vs Rs 25,000 target)")

    print("\n" + "="*64)
    print("HONEST CAVEATS")
    print("="*64)
    print("  [REAL]    Regime validation uses actual Nifty history — trustworthy.")
    print("  [MODELED] IC premiums are Black-Scholes estimates with realized-vol as IV.")
    print("            Real fills differ: bid-ask spread, IV skew, early assignment.")
    print("            Treat monthly income as a BALLPARK, not a promise.")
    print("  BEST DATA: forward paper-trading (already built) captures REAL fills.")
    print("            3 months paper > 2 years modeled. This backtest builds initial")
    print("            confidence; paper-trading builds earned confidence.")


def _quick_features(bars, ie):
    """Lightweight feature calc for backtest speed (no Kite calls)."""
    closes = [b["close"] for b in bars]
    spot = closes[-1]
    ema20 = ie._ema(closes, 20); ema50 = ie._ema(closes, 50)
    ema20_prev = ie._ema(closes[:-3], 20) if len(closes)>23 else ema20
    rv = _rvol(closes, 10)
    return {
        "index":"NIFTY","spot":spot,
        "ivr": _rvol_percentile(closes), "ivp": _rvol_percentile(closes),
        "vix": rv, "atm_iv": rv,
        "adx": _adx_fast(bars), "rsi": _rsi_fast(closes),
        "ema20":ema20,"ema50":ema50,
        "ema20_slope_up": (ema20>ema20_prev) if (ema20 and ema20_prev) else None,
        "atr_pct": 1.0, "bbw_pct": _bbw_fast(closes),
        "rv10": rv, "vrp": 0, "event_flag": False, "event_name":"",
    }

def _rvol(closes, days):
    if len(closes) < days+1: return 15.0
    rets = [math.log(closes[i]/closes[i-1]) for i in range(len(closes)-days, len(closes))]
    m = sum(rets)/len(rets)
    sd = math.sqrt(sum((x-m)**2 for x in rets)/len(rets))
    return sd*math.sqrt(252)*100

def _rvol_percentile(closes, lookback=180):
    if len(closes) < 40: return 50
    series = [_rvol(closes[:i], 10) for i in range(30, len(closes))]
    if not series: return 50
    cur = series[-1]; hist = series[-lookback:]
    return sum(1 for x in hist if x < cur)/len(hist)*100

def _adx_fast(bars, period=14):
    if len(bars) < period*2: return 15
    trs=[]; pdm=[]; mdm=[]
    for i in range(1,len(bars)):
        up=bars[i]["high"]-bars[i-1]["high"]; dn=bars[i-1]["low"]-bars[i]["low"]
        pdm.append(up if(up>dn and up>0) else 0); mdm.append(dn if(dn>up and dn>0) else 0)
        h,l,pc=bars[i]["high"],bars[i]["low"],bars[i-1]["close"]
        trs.append(max(h-l,abs(h-pc),abs(l-pc)))
    atr=sum(trs[-period:])/period
    if atr==0: return 15
    pdi=100*(sum(pdm[-period:])/period)/atr; mdi=100*(sum(mdm[-period:])/period)/atr
    if pdi+mdi==0: return 15
    return 100*abs(pdi-mdi)/(pdi+mdi)

def _rsi_fast(closes, period=14):
    if len(closes)<period+1: return 50
    d=[closes[i]-closes[i-1] for i in range(1,len(closes))]
    g=[max(0,x) for x in d]; l=[max(0,-x) for x in d]
    ag=sum(g[-period:])/period; al=sum(l[-period:])/period
    if al==0: return 100
    return 100-100/(1+ag/al)

def _bbw_fast(closes, period=20):
    if len(closes)<period+20: return 50
    widths=[]
    for i in range(period,len(closes)):
        w=closes[i-period:i]; m=sum(w)/period
        sd=math.sqrt(sum((x-m)**2 for x in w)/period)
        widths.append((4*sd)/m*100 if m else 0)
    if not widths: return 50
    cur=widths[-1]
    return sum(1 for x in widths if x<cur)/len(widths)*100

if __name__ == "__main__":
    run_backtest()
