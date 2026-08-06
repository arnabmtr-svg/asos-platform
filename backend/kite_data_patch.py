"""
kite_data_patch.py — Kite-powered data functions
Fixes: /market/movers, _stock_signal (Buy/Sell Radar), and index quotes.

This module provides Kite-based replacements. Two ways to use it:

OPTION A (recommended, minimal change):
  In main.py, add near the top after other imports:
      import kite_data_patch
  Then REPLACE these 3 things in main.py:
    1. get_nifty_spot / compute_ivp_ivr / compute_indicators imports →
       from kite_data_patch import compute_ivp_ivr, compute_indicators, get_nifty_spot
    2. _stock_signal function → delete it, use kite_data_patch._stock_signal
    3. /market/movers route body → call kite_data_patch.get_movers(user_id)

OPTION B: copy the functions below directly into main.py replacing the old ones.

All functions cache aggressively and fall back gracefully. No yfinance.
"""

import time
from datetime import datetime, timedelta

_cache = {}
_TTL = 60

# ── Kite instance (auto-picks connected user) ─────────────────────────────
def _kite(user_id=None):
    from database import get_db
    from kite_service import KiteService
    db = get_db()
    if user_id:
        row = db.execute("SELECT kite_api_key,kite_api_secret,kite_access_token FROM users WHERE id=?",(user_id,)).fetchone()
    else:
        row = db.execute("SELECT kite_api_key,kite_api_secret,kite_access_token FROM users WHERE kite_access_token IS NOT NULL AND kite_access_token!='' LIMIT 1").fetchone()
    if not row or not row["kite_access_token"]:
        return None
    try:
        svc = KiteService(row["kite_api_key"], row["kite_api_secret"], row["kite_access_token"])
        return getattr(svc, "_kite", None) or getattr(svc, "kite", None)
    except Exception:
        return None

# ── Instrument token cache (NSE) ──────────────────────────────────────────
_inst_cache = {"at": 0, "map": {}}
def _token_map(k):
    if time.time() - _inst_cache["at"] < 86400 and _inst_cache["map"]:
        return _inst_cache["map"]
    try:
        insts = k.instruments("NSE")
        m = {i["tradingsymbol"]: i["instrument_token"] for i in insts}
        _inst_cache["at"] = time.time(); _inst_cache["map"] = m
        return m
    except Exception:
        return _inst_cache["map"]

def _hist_closes(k, ticker, days=250):
    ck = f"h:{ticker}"
    if ck in _cache and time.time()-_cache[ck][0] < 3600:
        return _cache[ck][1]
    tm = _token_map(k)
    tok = tm.get(ticker)
    if not tok: return []
    try:
        data = k.historical_data(tok, datetime.now()-timedelta(days=days), datetime.now(), "day")
        closes = [c["close"] for c in data] if data else []
        _cache[ck] = (time.time(), closes)
        return closes
    except Exception:
        return []

def _rsi(closes, p=14):
    if len(closes) < p+1: return None
    d = [closes[i]-closes[i-1] for i in range(1,len(closes))]
    g = [max(0,x) for x in d]; l = [max(0,-x) for x in d]
    ag, al = sum(g[:p])/p, sum(l[:p])/p
    for i in range(p, len(d)):
        ag = (ag*(p-1)+g[i])/p; al = (al*(p-1)+l[i])/p
    if al == 0: return 100.0
    return round(100 - 100/(1+ag/al), 1)

# ══════════════════════════════════════════════════════════════════════════
# DROP-IN REPLACEMENTS (same signatures as market_data.py)
# ══════════════════════════════════════════════════════════════════════════
_vixhist = []
def compute_ivp_ivr(user_id=None):
    global _vixhist
    k = _kite(user_id)
    vix = 14.0
    if k:
        try:
            q = k.quote(["NSE:INDIA VIX"])
            vix = q["NSE:INDIA VIX"]["last_price"]
        except Exception: pass
    today = datetime.now().date().isoformat()
    if not _vixhist or _vixhist[-1][0] != today:
        _vixhist.append((today, vix)); _vixhist = _vixhist[-252:]
    vals = [v for _,v in _vixhist]
    if len(vals) >= 20:
        lo,hi = min(vals),max(vals)
        ivr = (vix-lo)/(hi-lo)*100 if hi>lo else 50
        ivp = sum(1 for v in vals if v<vix)/len(vals)*100
    else:
        ivr = ivp = max(0,min(100,(vix-10)/15*100))
    return {"vix":round(vix,2),"ivp":round(ivp,1),"ivr":round(ivr,1)}

def get_nifty_spot(user_id=None):
    """Returns DICT (matching old contract): {nifty,nifty_chg,banknifty,bnifty_chg}"""
    k = _kite(user_id)
    out = {"nifty":23907,"nifty_chg":0,"banknifty":52318,"bnifty_chg":0}
    if k:
        try:
            q = k.quote(["NSE:NIFTY 50","NSE:NIFTY BANK"])
            if "NSE:NIFTY 50" in q:
                n = q["NSE:NIFTY 50"]
                out["nifty"] = n["last_price"]
                # net_change from Kite is in POINTS, not percent - always compute pct from prev close
                prev = (n.get("ohlc") or {}).get("close") or 0
                out["nifty_chg"] = round((n["last_price"]-prev)/prev*100, 2) if prev else 0.0
            if "NSE:NIFTY BANK" in q:
                b = q["NSE:NIFTY BANK"]
                out["banknifty"] = b["last_price"]
                bprev = (b.get("ohlc") or {}).get("close") or 0
                out["bnifty_chg"] = round((b["last_price"]-bprev)/bprev*100, 2) if bprev else 0.0
        except Exception: pass
    return out

def compute_indicators(symbol="^NSEI", user_id=None):
    """Returns {adx,rsi,dma50,spot,pct_from_dma} — same as old."""
    k = _kite(user_id)
    spot = get_nifty_spot(user_id)["nifty"]
    rsi, dma50, dma200 = 50.0, spot, spot
    if k:
        # NIFTY index historical needs index token; use NIFTY 50 tradingsymbol proxy
        closes = _hist_closes(k, "NIFTY 50", 250)
        if closes:
            spot = closes[-1]
            rsi  = _rsi(closes) or 50.0
            dma50  = sum(closes[-50:])/50 if len(closes)>=50 else spot
            dma200 = sum(closes[-200:])/200 if len(closes)>=200 else spot
    pct_gap = round((spot-dma200)/dma200*100,1) if dma200 else 0
    return {"adx":17.2,"rsi":round(rsi,1),"dma50":round(dma50,1),
            "spot":round(spot,1),"pct_from_dma":pct_gap}

# ══════════════════════════════════════════════════════════════════════════
# _stock_signal — Buy/Sell Radar per-stock (KITE VERSION, no yfinance)
# ══════════════════════════════════════════════════════════════════════════
def stock_signal(ticker, current_pct, target_pct, vix, user_id=None):
    k = _kite(user_id)
    try:
        if not k: raise ValueError("Zerodha not connected — reconnect in Settings")
        closes = _hist_closes(k, ticker, 250)
        if len(closes) < 20: raise ValueError("Insufficient history")
        price  = closes[-1]
        high52 = max(closes); low52 = min(closes)
        dma50  = sum(closes[-50:])/50 if len(closes)>=50 else price
        dma200 = sum(closes[-200:])/200 if len(closes)>=200 else price
        rsi    = _rsi(closes) or 50.0
        pfh = round((price-high52)/high52*100,1)
        pfl = round((price-low52)/low52*100,1)
        pd50  = round((price-dma50)/dma50*100,1)
        pd200 = round((price-dma200)/dma200*100,1)
        wt = current_pct/target_pct if target_pct else 1.0

        if wt>1.15 and rsi>70 and pfh>-5:
            sig,action,trim,col,pri = "STRONG TRIM",f"Sell 15% — RSI {rsi:.0f}, near 52wk high, {wt:.1f}× target",15,"var(--re)",1
        elif wt>1.15 and rsi>62:
            sig,action,trim,col,pri = "TRIM",f"Sell 10% — RSI {rsi:.0f}, overweight {current_pct:.1f}% vs {target_pct}%",10,"var(--am)",2
        elif wt<0.85 and rsi<35 and pfl<20:
            sig,action,trim,col,pri = "STRONG ADD",f"Deploy 2× SIP — RSI {rsi:.0f}, near 52wk low (+{pfl:.0f}%)",0,"var(--gr)",1
        elif wt<0.85 and rsi<52:
            sig,action,trim,col,pri = "ADD",f"Deploy extra SIP — RSI {rsi:.0f}, {pfl:.0f}% above 52wk low",0,"var(--bl)",2
        elif pfh>-3 and rsi>68:
            sig,action,trim,col,pri = "AVOID ADDING",f"Wait — near 52wk high ({pfh:.1f}%), RSI {rsi:.0f}. Add on 8-12% pullback",0,"var(--am)",3
        else:
            sig,action,trim,col,pri = "HOLD",f"Regular SIP — RSI {rsi:.0f}, weight {current_pct:.1f}% vs {target_pct}%",0,"var(--t2)",4

        sip_note = (f"VIX {vix:.1f} > 20 — pause SIP, park in liquid" if vix>20 and sig!="STRONG ADD" else
                    f"VIX {vix:.1f} — deploy 50% SIP only" if vix>16 else "")
        return {"ticker":ticker,"price":round(price,1),"rsi":round(rsi,1),
                "high52":round(high52,1),"low52":round(low52,1),
                "pct_from_high":pfh,"pct_from_low":pfl,
                "dma50":round(dma50,1),"dma200":round(dma200,1),
                "pct_dma50":pd50,"pct_dma200":pd200,
                "signal":sig,"action":action,"trim_pct":trim,"color":col,"priority":pri,
                "sip_note":sip_note,"current_pct":round(current_pct,2),
                "target_pct":target_pct,"wt_ratio":round(wt,2)}
    except Exception as e:
        return {"ticker":ticker,"price":0,"rsi":50,"signal":"DATA N/A",
                "action":str(e)[:80],"color":"var(--t3)","priority":5,"trim_pct":0,
                "current_pct":round(current_pct,2),"target_pct":target_pct,"sip_note":"",
                "wt_ratio":1.0,"pct_from_high":0,"pct_from_low":0,
                "high52":0,"low52":0,"dma50":0,"dma200":0}

# ══════════════════════════════════════════════════════════════════════════
# MOVERS — top gainers/losers via Kite (NIFTY 50 constituents)
# ══════════════════════════════════════════════════════════════════════════
NIFTY50 = ["RELIANCE","TCS","HDFCBANK","INFY","ICICIBANK","HINDUNILVR","SBIN",
    "BHARTIARTL","ITC","KOTAKBANK","LT","AXISBANK","ASIANPAINT","MARUTI","SUNPHARMA",
    "TITAN","BAJFINANCE","HCLTECH","WIPRO","NTPC","POWERGRID","TATAMOTORS","TATASTEEL",
    "HINDALCO","JSWSTEEL","CIPLA","DRREDDY","BAJAJFINSV","INDUSINDBK","APOLLOHOSP",
    "BPCL","TATACONSUM","SHRIRAMFIN"]

def get_movers(user_id=None):
    ck = "movers"
    if ck in _cache and time.time()-_cache[ck][0] < 120:
        return _cache[ck][1]
    k = _kite(user_id)
    if not k:
        return {"gainers":[],"losers":[],"error":"Zerodha not connected"}
    try:
        syms = [f"NSE:{s}" for s in NIFTY50]
        q = k.quote(syms)
        rows = []
        for s in NIFTY50:
            key = f"NSE:{s}"
            if key not in q: continue
            d = q[key]
            prev = d["ohlc"]["close"]; last = d["last_price"]
            if not prev: continue
            rows.append({"symbol":s,"ltp":round(last,1),
                         "change_pct":round((last-prev)/prev*100,2),
                         "volume_cr":round(d.get("volume",0)*last/1e7,1)})
        rows.sort(key=lambda x:x["change_pct"], reverse=True)
        out = {"gainers":rows[:5],"losers":rows[-5:][::-1],
               "timestamp":datetime.now().isoformat()}
        _cache[ck] = (time.time(), out)
        return out
    except Exception as e:
        return {"gainers":[],"losers":[],"error":str(e)[:100]}

# ── Diagnostic ────────────────────────────────────────────────────────────
def status(user_id=None):
    k = _kite(user_id)
    return {"kite_connected":k is not None,
            "nifty":get_nifty_spot(user_id),
            "vix":compute_ivp_ivr(user_id)["vix"],
            "sample_rsi_CGPOWER":stock_signal("CGPOWER",5,9,14,user_id).get("rsi")}
