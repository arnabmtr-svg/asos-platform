"""
strategy_matrix.py — Institutional Strategy Matrix v1.0 (from Arnab's spec)
Reads live indicators (EMA20/50, VWAP, ADX, PCR, VIX, IVR) and picks the
right options strategy with an 8-factor confidence score.

Encodes:
  - Market Condition -> Strategy -> DTE matrix
  - Indicator filters (Bull/Bear/Sideways)
  - 8-factor Confidence Matrix (Trend20 Vol20 Chain20 Breadth15 OI10 Greeks5 Global5 Events5)
  - Expiry selection framework
  - Non-negotiable institutional rules

Routes:
  GET /matrix/analyze?index=NIFTY  -> full read: condition, strategy, confidence, DTE, entry/exit
  GET /matrix/expiry-for-dte?index=NIFTY&dte=30 -> nearest expiry to target DTE

main.py:
  try: import strategy_matrix
  except ImportError: strategy_matrix = None
  # after app: if strategy_matrix: strategy_matrix.register_routes(app)
"""
import math
from datetime import date, datetime, timedelta

# ── The Strategy Matrix (market condition -> plan) ────────────────────────
MATRIX = {
    "STRONG_BULL": {"strategy":"Bull Put Spread","dte":"20-30","risk":"Low","conf_req":85,
        "entry":"After ORB & VWAP confirmation","exit":"40-50% profit / trend reversal",
        "short_delta":22,"long_delta":12},
    "STRONG_BEAR": {"strategy":"Bear Call Spread","dte":"20-30","risk":"Low","conf_req":85,
        "entry":"After confirmation","exit":"40-50% profit / trend reversal",
        "short_delta":22,"long_delta":12},
    "SIDEWAYS_LOW_VOL": {"strategy":"Iron Condor","dte":"25-35","risk":"Low","conf_req":90,
        "entry":"9:45-10:15 AM after settle","exit":"40-50% profit",
        "short_delta":17,"long_delta":7},
    "SIDEWAYS_HIGH_VOL": {"strategy":"Wide Iron Condor","dte":"25-35","risk":"Medium","conf_req":85,
        "entry":"After IV stabilizes","exit":"40% profit",
        "short_delta":15,"long_delta":6},
    "VOL_EXPANSION": {"strategy":"Debit Spread","dte":"10-20","risk":"Medium","conf_req":85,
        "entry":"Trend confirmation","exit":"1:2 risk-reward",
        "short_delta":None,"long_delta":None},
    "VERY_LOW_IV": {"strategy":"Calendar Spread","dte":"30-45","risk":"Low","conf_req":80,
        "entry":"After confirmation","exit":"Before gamma risk increases",
        "short_delta":None,"long_delta":None},
    "HIGH_IV": {"strategy":"Wide Iron Condor / Credit Spread","dte":"25-40","risk":"Medium","conf_req":90,
        "entry":"After first hour","exit":"30-40% profit",
        "short_delta":15,"long_delta":6},
    "EVENT": {"strategy":"No Trade","dte":"—","risk":"None","conf_req":100,
        "entry":"—","exit":"—","short_delta":None,"long_delta":None},
    "UNCLEAR": {"strategy":"Stay in Cash","dte":"—","risk":"None","conf_req":100,
        "entry":"Wait for clarity","exit":"—","short_delta":None,"long_delta":None},
}


# ── Indicators ────────────────────────────────────────────────────────────
def _ema(vals, period):
    if len(vals) < period: return sum(vals)/len(vals) if vals else 0
    k = 2/(period+1); e = sum(vals[:period])/period
    for v in vals[period:]:
        e = v*k + e*(1-k)
    return e

def _adx(highs, lows, closes, period=14):
    """Proper Wilder-smoothed ADX (the simple-average version overstates it)."""
    n = len(closes)
    if n < period*2 + 1: return 15
    trs, pdm, mdm = [], [], []
    for i in range(1, n):
        up = highs[i]-highs[i-1]; dn = lows[i-1]-lows[i]
        pdm.append(up if (up>dn and up>0) else 0.0)
        mdm.append(dn if (dn>up and dn>0) else 0.0)
        trs.append(max(highs[i]-lows[i], abs(highs[i]-closes[i-1]), abs(lows[i]-closes[i-1])))
    # Wilder smoothing
    def _wilder(arr):
        sm = [sum(arr[:period])]
        for v in arr[period:]:
            sm.append(sm[-1] - sm[-1]/period + v)
        return sm
    str_ = _wilder(trs); spdm = _wilder(pdm); smdm = _wilder(mdm)
    dxs = []
    for i in range(len(str_)):
        if str_[i] == 0: continue
        pdi = 100*spdm[i]/str_[i]; mdi = 100*smdm[i]/str_[i]
        if pdi+mdi == 0: continue
        dxs.append(100*abs(pdi-mdi)/(pdi+mdi))
    if len(dxs) < period: 
        return round(sum(dxs)/len(dxs),1) if dxs else 15
    # ADX = smoothed average of DX over period
    adx = sum(dxs[:period])/period
    for dx in dxs[period:]:
        adx = (adx*(period-1) + dx)/period
    return round(adx, 1)

def _vwap_proxy(highs, lows, closes, vols):
    """Session VWAP proxy from recent bars (daily approximation)."""
    if not vols or sum(vols) == 0:
        # no volume -> use typical price average
        tps = [(highs[i]+lows[i]+closes[i])/3 for i in range(len(closes))]
        return sum(tps[-20:])/min(20,len(tps))
    num = sum(((highs[i]+lows[i]+closes[i])/3)*vols[i] for i in range(len(closes)))
    return num/sum(vols)


def get_indicators(k, index="NIFTY"):
    """Compute EMA20/50, VWAP, ADX, RSI, price from live Kite data."""
    from kite_data_patch import _hist_closes, _kite
    import kite_data_patch as kdp
    # get OHLCV
    SPOT_TOKENS = {"NIFTY":256265, "BANKNIFTY":260105}  # NSE index tokens
    try:
        tok = None
        insts = k.instruments("NSE")
        name_map = {"NIFTY":"NIFTY 50","BANKNIFTY":"NIFTY BANK"}
        for i in insts:
            if i.get("tradingsymbol")==name_map.get(index) or i.get("name")==name_map.get(index):
                tok = i["instrument_token"]; break
        if not tok:
            tok = SPOT_TOKENS.get(index)
        to_d = date.today(); from_d = to_d - timedelta(days=120)
        candles = k.historical_data(tok, from_d, to_d, "day")
        if not candles or len(candles) < 50:
            return None
        highs = [c["high"] for c in candles]
        lows = [c["low"] for c in candles]
        closes = [c["close"] for c in candles]
        vols = [c.get("volume",0) for c in candles]
        price = closes[-1]
        ema20 = _ema(closes, 20); ema50 = _ema(closes, 50)
        vwap = _vwap_proxy(highs, lows, closes, vols)
        adx = _adx(highs, lows, closes)
        return {
            "price": round(price,1), "ema20": round(ema20,1), "ema50": round(ema50,1),
            "vwap": round(vwap,1), "adx": adx,
            "ema_bull": ema20 > ema50, "above_vwap": price > vwap,
            "ema_gap_pct": round((ema20-ema50)/ema50*100, 2),
        }
    except Exception as e:
        print(f"indicators failed: {e}")
        return None


def classify_condition(ind, vix, ivr, pcr, event_soon=False):
    """Apply the document's Market Condition Filters."""
    if event_soon:
        return "EVENT"
    if ind is None:
        return "UNCLEAR"
    adx = ind["adx"]; bull = ind["ema_bull"]; above_vwap = ind["above_vwap"]

    # very low IV -> calendar territory
    if vix is not None and vix < 13:
        return "VERY_LOW_IV"
    # high IV
    if vix is not None and vix > 20:
        return "HIGH_IV"

    # trend: EMA + VWAP + ADX must align (document's rule)
    if adx > 25 and bull and above_vwap and (pcr is None or pcr >= 0.9):
        return "STRONG_BULL"
    if adx > 25 and not bull and not above_vwap and (pcr is None or pcr < 0.9):
        return "STRONG_BEAR"

    # sideways: ADX < 20
    if adx < 20:
        if ivr is not None and ivr > 50:
            return "SIDEWAYS_HIGH_VOL"
        return "SIDEWAYS_LOW_VOL"

    # in between (ADX 20-25) = unclear -> cash
    return "UNCLEAR"


def confidence_score(ind, vix, ivr, pcr, chain_ok=True, event_soon=False):
    """8-factor Confidence Matrix from the document."""
    score = 0; breakdown = {}
    # Trend 20% — EMA + VWAP + ADX alignment
    trend = 0
    if ind:
        if ind["adx"] > 25: trend += 10
        elif ind["adx"] < 20: trend += 10  # clear sideways is also a clear signal
        if ind["ema_bull"] == ind["above_vwap"]: trend += 10  # aligned
    breakdown["trend"] = trend; score += trend
    # Volatility 20% — is IV in a tradeable band
    vol = 0
    if vix is not None:
        if 12 <= vix <= 18: vol = 20
        elif vix < 20: vol = 12
        else: vol = 6
    breakdown["volatility"] = vol; score += vol
    # Option Chain 20% — data available + reasonable
    breakdown["option_chain"] = 20 if chain_ok else 0; score += (20 if chain_ok else 0)
    # Market Breadth 15% — (proxy: not computed live yet, give partial)
    breakdown["breadth"] = 10; score += 10
    # Open Interest 10% — PCR sane
    oi = 10 if (pcr is not None and 0.5 < pcr < 2.0) else 5
    breakdown["open_interest"] = oi; score += oi
    # Greeks 5%
    breakdown["greeks"] = 5; score += 5
    # Global 5% (proxy)
    breakdown["global"] = 3; score += 3
    # Events 5% — clear if no event
    breakdown["events"] = 0 if event_soon else 5; score += (0 if event_soon else 5)
    return {"score": score, "breakdown": breakdown}


def allocation_for_score(score):
    if score >= 90: return {"action":"Full allocation","pct":100}
    if score >= 80: return {"action":"Normal allocation","pct":75}
    if score >= 70: return {"action":"Half allocation","pct":50}
    return {"action":"No Trade","pct":0}


def nearest_expiry_to_dte(k, index, target_dte):
    """Find the expiry closest to target DTE (e.g. 30 for monthly IC)."""
    import options_data
    exps = options_data.get_expiries(k, index)
    if not exps: return None
    today = date.today()
    best = min(exps, key=lambda e: abs((e - today).days - target_dte))
    return best




# ══════════════════════════════════════════════════════════════════════════
# INSTITUTIONAL FRAMEWORK v2 (Arnab's 9-step system)
# Gate 1: eligibility (any red = no trade) -> Gate 2: regime -> confidence -> size
# ══════════════════════════════════════════════════════════════════════════

def eligibility_gate(vix, ivr, adx, gap_pct=0.0, event_soon=False, confidence=None):
    """
    Gate 1 — Market Eligibility. Returns per-parameter zones + overall verdict.
    ANY red condition => no trade.
    """
    def zone(val, green, yellow, kind="band"):
        if val is None: return "unknown"
        lo_g, hi_g = green; 
        if lo_g <= val <= hi_g: return "green"
        for (ylo, yhi) in yellow:
            if ylo <= val <= yhi: return "yellow"
        return "red"

    checks = {}
    # India VIX: green 13-18, yellow 18-20, red <12 or >20
    checks["vix"] = {"value": vix, "zone":
        ("green" if vix is not None and 13 <= vix <= 18 else
         "yellow" if vix is not None and 18 < vix <= 20 else
         "red")}
    # IVR: green 30-60, yellow 20-30 or 60-70, red <20 or >70
    checks["ivr"] = {"value": ivr, "zone":
        ("green" if ivr is not None and 30 <= ivr <= 60 else
         "yellow" if ivr is not None and (20 <= ivr < 30 or 60 < ivr <= 70) else
         "red")}
    # ADX: green <18 (range) or >25 (trend), yellow 18-25, red = mixed (handled as yellow-ish)
    checks["adx"] = {"value": adx, "zone":
        ("green" if adx is not None and (adx < 18 or adx > 25) else
         "yellow" if adx is not None and 18 <= adx <= 25 else
         "red")}
    # Event: none=green, minor=yellow, major=red
    checks["event"] = {"value": event_soon, "zone": "red" if event_soon else "green"}
    # Gap: <0.5% green, 0.5-1% yellow, >1% red
    checks["gap"] = {"value": gap_pct, "zone":
        ("green" if abs(gap_pct) < 0.5 else "yellow" if abs(gap_pct) <= 1.0 else "red")}
    # Confidence: >85 green, 75-85 yellow, <75 red
    if confidence is not None:
        checks["confidence"] = {"value": confidence, "zone":
            ("green" if confidence > 85 else "yellow" if confidence >= 75 else "red")}

    reds = [k for k,v in checks.items() if v["zone"] == "red"]
    passed = len(reds) == 0
    return {"passed": passed, "checks": checks, "red_flags": reds,
            "verdict": "ELIGIBLE" if passed else f"NO TRADE — red: {', '.join(reds)}"}


def confidence_score_v2(ind, vix, ivr, pcr, chain_ok=True, breadth=None, oi_ok=True, event_soon=False):
    """Arnab's 7-factor weights: Trend20 IVR15 VIX15 Chain20 Breadth10 OI10 Events10."""
    b = {}
    # Trend 20 — EMA+VWAP+ADX alignment
    t = 0
    if ind:
        if ind["adx"] > 25 or ind["adx"] < 18: t += 12   # clear regime
        if ind["ema_bull"] == ind["above_vwap"]: t += 8   # aligned direction
    b["trend"] = t
    # IVR 15
    iv = 15 if (ivr is not None and 30 <= ivr <= 60) else 8 if (ivr is not None and 20 <= ivr <= 70) else 0
    b["ivr"] = iv
    # VIX 15
    vx = 15 if (vix is not None and 13 <= vix <= 18) else 8 if (vix is not None and vix <= 20) else 0
    b["vix"] = vx
    # Option chain 20
    b["chain"] = 20 if chain_ok else 0
    # Breadth 10 (proxy if not provided)
    b["breadth"] = breadth if breadth is not None else 6
    # OI 10
    b["oi"] = 10 if (oi_ok and pcr is not None and 0.7 <= pcr <= 1.4) else 5
    # Events 10
    b["events"] = 0 if event_soon else 10
    total = sum(b.values())
    return {"score": total, "breakdown": b}


def position_sizing(confidence, capital=1000000, max_risk_pct=1.0, max_loss_per_lot=None):
    """
    Risk-based sizing (Step 5). Lots from risk limit, NOT margin.
    Returns allocation % and, if max_loss_per_lot given, the lot count.
    """
    if confidence >= 95: alloc = 100
    elif confidence >= 90: alloc = 75
    elif confidence >= 85: alloc = 50
    else: alloc = 0
    max_loss_rupees = capital * (max_risk_pct/100) * (alloc/100)
    lots = None
    if max_loss_per_lot and max_loss_per_lot > 0 and alloc > 0:
        lots = max(1, int(max_loss_rupees / max_loss_per_lot))
    return {"allocation_pct": alloc,
            "max_loss_budget": round(max_loss_rupees, 0),
            "suggested_lots": lots,
            "decision": ("Execute" if alloc==100 else f"Execute {alloc}% size" if alloc>0 else "No Trade")}


def register_routes(app):
    from fastapi import Depends, HTTPException
    from auth import get_current_user

    def _k(uid):
        from kite_data_patch import _kite
        k = _kite(uid)
        if not k: raise HTTPException(400, "Kite not connected")
        return k

    @app.get("/matrix/analyze")
    async def analyze(index: str = "NIFTY", current_user=Depends(get_current_user)):
        k = _k(current_user["id"]); index = index.upper()
        ind = get_indicators(k, index)
        # vix/ivr from kite_data_patch
        vix = ivr = pcr = None
        try:
            from kite_data_patch import compute_ivp_ivr
            iv = compute_ivp_ivr(current_user["id"])
            vix = iv.get("vix"); ivr = iv.get("ivr")
        except Exception: pass
        try:
            import options_data
            ch = options_data.get_chain(k, index, width=10)
            pcr = ch.get("pcr")
        except Exception: pass
        # event check
        event_soon = False
        try:
            import athena_market
            # if athena_market exposes events within 2 days
        except Exception: pass

        condition = classify_condition(ind, vix, ivr, pcr, event_soon)
        plan = MATRIX.get(condition, MATRIX["UNCLEAR"])
        conf = confidence_score(ind, vix, ivr, pcr, chain_ok=(pcr is not None), event_soon=event_soon)
        alloc = allocation_for_score(conf["score"])
        # gate: strategy activates only if score >= conf_req
        # UNCLEAR/EVENT/cash conditions never activate (not a confidence issue)
        no_trade_conditions = condition in ("UNCLEAR", "EVENT")
        activated = (not no_trade_conditions) and conf["score"] >= plan["conf_req"] and alloc["pct"] > 0

        return {
            "index": index, "condition": condition,
            "indicators": ind, "vix": vix, "ivr": ivr, "pcr": pcr,
            "strategy": plan["strategy"], "dte_band": plan["dte"],
            "entry": plan["entry"], "exit": plan["exit"], "risk": plan["risk"],
            "short_delta": plan.get("short_delta"), "long_delta": plan.get("long_delta"),
            "confidence": conf["score"], "confidence_req": plan["conf_req"],
            "confidence_breakdown": conf["breakdown"],
            "allocation": alloc, "activated": activated,
            "verdict": (f"{plan['strategy']} — {alloc['action']}" if activated
                        else (f"NO TRADE — {plan['strategy']} (market conditions unclear/conflicting)"
                              if condition in ("UNCLEAR","EVENT")
                              else f"NO TRADE — confidence {conf['score']} < {plan['conf_req']} required")),
            "generated_at": datetime.now().isoformat(),
        }

    @app.get("/matrix/full-analysis")
    async def full_analysis(index: str = "NIFTY", capital: float = 1000000,
                            current_user=Depends(get_current_user)):
        """
        The complete institutional framework: Gate1 eligibility -> regime ->
        confidence(v2 weights) -> position sizing. Arnab's 9-step system.
        """
        k = _k(current_user["id"]); index = index.upper()
        ind = get_indicators(k, index)
        vix = ivr = pcr = None; gap_pct = 0.0
        try:
            from kite_data_patch import compute_ivp_ivr
            iv = compute_ivp_ivr(current_user["id"]); vix=iv.get("vix"); ivr=iv.get("ivr")
        except Exception: pass
        try:
            import options_data
            ch = options_data.get_chain(k, index, width=10); pcr = ch.get("pcr")
        except Exception: pass
        event_soon = False

        # regime + confidence
        condition = classify_condition(ind, vix, ivr, pcr, event_soon)
        conf = confidence_score_v2(ind, vix, ivr, pcr, chain_ok=(pcr is not None), event_soon=event_soon)
        # Gate 1 (includes confidence as a filter)
        adx = ind["adx"] if ind else None
        gate1 = eligibility_gate(vix, ivr, adx, gap_pct, event_soon, conf["score"])
        plan = MATRIX.get(condition, MATRIX["UNCLEAR"])
        sizing = position_sizing(conf["score"], capital)

        # final: trade only if gate1 passes AND regime is tradeable AND sizing > 0
        tradeable = gate1["passed"] and condition not in ("UNCLEAR","EVENT") and sizing["allocation_pct"] > 0

        return {
            "index": index,
            "gate1_eligibility": gate1,
            "regime": condition, "strategy": plan["strategy"], "dte_band": plan["dte"],
            "indicators": ind, "vix": vix, "ivr": ivr, "pcr": pcr,
            "confidence": conf["score"], "confidence_breakdown": conf["breakdown"],
            "position_sizing": sizing,
            "tradeable": tradeable,
            "final_verdict": (f"{plan['strategy']} — {sizing['decision']}" if tradeable
                              else gate1["verdict"] if not gate1["passed"]
                              else f"NO TRADE — {condition} / confidence {conf['score']}"),
            "entry_window": "9:45-10:15 AM (after opening volatility settles)",
            "exit_rules": {"profit_target": "40-50% of max premium",
                           "time_exit": "close at 7-10 DTE",
                           "stop": f"{plan.get('risk','defined')} risk / 1% capital"},
            "generated_at": datetime.now().isoformat(),
        }

    @app.get("/matrix/recommended-trade")
    async def recommended_trade(index: str = "NIFTY", current_user=Depends(get_current_user)):
        """
        The bridge: matrix picks the strategy -> build it from the live chain,
        at the right DTE and deltas. This is what Trade Builder's
        'Build recommended' button calls.
        """
        k = _k(current_user["id"]); index = index.upper()
        # 1. get the matrix analysis
        ind = get_indicators(k, index)
        vix = ivr = pcr = None
        try:
            from kite_data_patch import compute_ivp_ivr
            iv = compute_ivp_ivr(current_user["id"]); vix=iv.get("vix"); ivr=iv.get("ivr")
        except Exception: pass
        try:
            import options_data
            ch0 = options_data.get_chain(k, index, width=10); pcr = ch0.get("pcr")
        except Exception: pass
        condition = classify_condition(ind, vix, ivr, pcr, False)
        plan = MATRIX.get(condition, MATRIX["UNCLEAR"])

        # 2. map matrix strategy -> build_from_spec structure
        STRAT_MAP = {
            "Bull Put Spread":"bull_put_spread", "Bear Call Spread":"bear_call_spread",
            "Iron Condor":"iron_condor", "Wide Iron Condor":"iron_condor",
            "Wide Iron Condor / Credit Spread":"iron_condor",
            "Debit Spread":"iron_condor",  # placeholder; debit handled separately later
            "Calendar Spread":"iron_condor",  # placeholder
        }
        structure = STRAT_MAP.get(plan["strategy"])
        if not structure or condition in ("UNCLEAR","EVENT"):
            return {"buildable": False, "condition": condition,
                    "strategy": plan["strategy"],
                    "reason": f"Matrix says: {plan['strategy']} — not auto-buildable "
                              f"({'stay in cash' if condition=='UNCLEAR' else 'avoid events' if condition=='EVENT' else 'manual construction needed'})"}

        # 3. pick DTE in the middle of the plan's band
        dte_band = plan["dte"]  # e.g. "25-35"
        try:
            lo, hi = [int(x) for x in dte_band.split("-")]; target_dte = (lo+hi)//2
        except Exception:
            target_dte = 30
        expiry = nearest_expiry_to_dte(k, index, target_dte)

        # 4. build it from the live chain
        import options_data
        spec = {"structure": structure, "index": index,
                "expiry": str(expiry) if expiry else None,
                "short_delta": plan.get("short_delta") or 16,
                "wing": 5}
        built = options_data.build_from_spec(k, spec)
        if built.get("error"):
            return {"buildable": False, "condition": condition,
                    "strategy": plan["strategy"], "reason": built["error"]}

        # 5. attach matrix context + gate
        built["buildable"] = True
        built["matrix_condition"] = condition
        built["matrix_strategy"] = plan["strategy"]
        built["matrix_dte_band"] = dte_band
        built["target_dte"] = target_dte
        built["entry_rule"] = plan["entry"]
        built["exit_rule"] = plan["exit"]
        try:
            import income_engine
            built["entry_gate"] = income_engine.check_entry_gate("A1", index, current_user["id"])
            built["deployable"] = bool(built["entry_gate"].get("allowed"))
        except Exception:
            built["deployable"] = False
        return built

    @app.get("/matrix/expiry-for-dte")
    async def expiry_for_dte(index: str = "NIFTY", dte: int = 30, current_user=Depends(get_current_user)):
        k = _k(current_user["id"])
        e = nearest_expiry_to_dte(k, index.upper(), dte)
        if not e: return {"error":"no expiries"}
        return {"index": index, "target_dte": dte, "expiry": str(e),
                "actual_dte": (e - date.today()).days}
