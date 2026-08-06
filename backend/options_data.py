"""
options_data.py — REAL option chain data layer for ATHENA Options Desk
Kite returns live LTP + OI (confirmed working). We add Black-Scholes greeks
(delta/theta/gamma/vega) computed from each strike's implied vol.

Routes:
  GET /options/chain?index=NIFTY&expiry=nearest  -> full chain w/ greeks, OI, max-pain, PCR
  GET /options/expiries?index=NIFTY              -> available expiries
  GET /options/build-ic?index=NIFTY&delta=16     -> auto-suggest Iron Condor from live chain

main.py:
  try: import options_data
  except ImportError: options_data = None
  # after app: if options_data: options_data.register_routes(app)
"""
import math
from datetime import date, datetime

LOT_SIZE = {"NIFTY": 75, "BANKNIFTY": 35, "FINNIFTY": 65, "SENSEX": 20}
STRIKE_STEP = {"NIFTY": 50, "BANKNIFTY": 100, "FINNIFTY": 50, "SENSEX": 100}
SPOT_SYM = {"NIFTY": "NSE:NIFTY 50", "BANKNIFTY": "NSE:NIFTY BANK",
            "FINNIFTY": "NSE:NIFTY FIN SERVICE", "SENSEX": "BSE:SENSEX"}

_instr_cache = {"at": 0, "data": {}}


# ── Black-Scholes greeks ──────────────────────────────────────────────────
def _norm_cdf(x): return 0.5 * (1 + math.erf(x / math.sqrt(2)))
def _norm_pdf(x): return math.exp(-x*x/2) / math.sqrt(2*math.pi)

def _greeks(S, K, T, r, sigma, opt):
    if T <= 0 or sigma <= 0 or S <= 0 or K <= 0:
        return {"delta": 0, "theta": 0, "gamma": 0, "vega": 0}
    d1 = (math.log(S/K) + (r + sigma**2/2)*T) / (sigma*math.sqrt(T))
    d2 = d1 - sigma*math.sqrt(T)
    if opt == "CE":
        delta = _norm_cdf(d1)
        theta = (-S*_norm_pdf(d1)*sigma/(2*math.sqrt(T)) - r*K*math.exp(-r*T)*_norm_cdf(d2))/365
    else:
        delta = _norm_cdf(d1) - 1
        theta = (-S*_norm_pdf(d1)*sigma/(2*math.sqrt(T)) + r*K*math.exp(-r*T)*_norm_cdf(-d2))/365
    gamma = _norm_pdf(d1)/(S*sigma*math.sqrt(T))
    vega = S*_norm_pdf(d1)*math.sqrt(T)/100
    return {"delta": round(delta, 3), "theta": round(theta, 2),
            "gamma": round(gamma, 5), "vega": round(vega, 2)}

def _implied_vol(price, S, K, T, r, opt):
    """Newton-ish bisection for IV from market price."""
    if price <= 0 or T <= 0:
        return None
    lo, hi = 0.01, 3.0
    for _ in range(40):
        mid = (lo+hi)/2
        bs = _bs_price(S, K, T, r, mid, opt)
        if abs(bs-price) < 0.5:
            return mid
        if bs > price: hi = mid
        else: lo = mid
    return mid

def _bs_price(S, K, T, r, sigma, opt):
    if T<=0 or sigma<=0: return max(0,S-K) if opt=="CE" else max(0,K-S)
    d1=(math.log(S/K)+(r+sigma**2/2)*T)/(sigma*math.sqrt(T)); d2=d1-sigma*math.sqrt(T)
    if opt=="CE": return S*_norm_cdf(d1)-K*math.exp(-r*T)*_norm_cdf(d2)
    return K*math.exp(-r*T)*_norm_cdf(-d2)-S*_norm_cdf(-d1)


def _get_instruments(k, index):
    """Cache NFO/BFO option instruments for the index."""
    import time
    now = time.time()
    if now - _instr_cache["at"] < 3600 and index in _instr_cache["data"]:
        return _instr_cache["data"][index]
    exch = "BFO" if index == "SENSEX" else "NFO"
    allins = k.instruments(exch)
    opts = [i for i in allins if i.get("name") == index
            and i.get("instrument_type") in ("CE", "PE")]
    _instr_cache["data"][index] = opts
    _instr_cache["at"] = now
    return opts


def get_expiries(k, index):
    opts = _get_instruments(k, index)
    exps = sorted(set(i["expiry"] for i in opts if i.get("expiry")))
    today = date.today()
    exps = [e for e in exps if (e if isinstance(e, date) else datetime.strptime(str(e),"%Y-%m-%d").date()) >= today]
    return exps


def get_chain(k, index, expiry=None, width=15):
    """Full option chain with live LTP/OI + computed greeks + IV."""
    opts = _get_instruments(k, index)
    exps = get_expiries(k, index)
    if not exps:
        return {"error": "no expiries"}
    target = None
    if expiry and isinstance(expiry, str):
        try:
            # only accept clean YYYY-MM-DD; ignore "Loading...", ellipsis, etc.
            target = datetime.strptime(expiry.strip()[:10], "%Y-%m-%d").date()
            if target not in exps:
                target = None  # not a valid expiry -> fall back
        except (ValueError, TypeError):
            target = None
    if target is None:
        target = exps[0]
    # spot
    spot_q = k.quote([SPOT_SYM[index]])
    spot = spot_q[SPOT_SYM[index]]["last_price"]
    step = STRIKE_STEP[index]
    atm = round(spot/step)*step
    # strikes in window
    strikes = sorted(set(i["strike"] for i in opts
                         if (i["expiry"] if isinstance(i["expiry"],date)
                             else datetime.strptime(str(i["expiry"]),"%Y-%m-%d").date()) == target
                         and abs(i["strike"]-atm) <= width*step))
    # map strike -> {CE:sym, PE:sym}
    sym_map = {}
    for i in opts:
        e = i["expiry"] if isinstance(i["expiry"],date) else datetime.strptime(str(i["expiry"]),"%Y-%m-%d").date()
        if e == target and i["strike"] in strikes:
            sym_map.setdefault(i["strike"], {})[i["instrument_type"]] = i["tradingsymbol"]
    # batch quote all
    exch = "BFO" if index=="SENSEX" else "NFO"
    all_syms = []
    for s in strikes:
        for t in ("CE","PE"):
            if sym_map.get(s,{}).get(t):
                all_syms.append(f"{exch}:{sym_map[s][t]}")
    quotes = {}
    for i in range(0, len(all_syms), 200):
        quotes.update(k.quote(all_syms[i:i+200]))

    dte = (target - date.today()).days
    T = max(dte/365, 1/365)
    r = 0.065
    rows = []
    total_ce_oi = total_pe_oi = 0
    for s in strikes:
        row = {"strike": s, "is_atm": s == atm}
        for t in ("CE","PE"):
            sym = sym_map.get(s,{}).get(t)
            key = f"{exch}:{sym}" if sym else None
            if key and key in quotes:
                q = quotes[key]
                ltp = q.get("last_price",0)
                oi = q.get("oi",0)
                iv = _implied_vol(ltp, spot, s, T, r, t) if ltp>0 else None
                gk = _greeks(spot, s, T, r, iv or 0.14, t)
                row[t] = {"ltp": ltp, "oi": oi, "volume": q.get("volume",0),
                          "iv": round(iv*100,1) if iv else None, **gk}
                if t=="CE": total_ce_oi += oi
                else: total_pe_oi += oi
            else:
                row[t] = {"ltp":0,"oi":0,"iv":None,"delta":0,"theta":0,"gamma":0,"vega":0}
        rows.append(row)

    # max pain = strike where total option writer payout is minimized
    max_pain = _max_pain(rows, strikes)
    pcr = round(total_pe_oi/total_ce_oi, 2) if total_ce_oi else 0

    return {"index": index, "spot": spot, "atm": atm, "expiry": str(target),
            "dte": dte, "lot_size": LOT_SIZE[index],
            "rows": rows, "max_pain": max_pain, "pcr": pcr,
            "total_ce_oi": total_ce_oi, "total_pe_oi": total_pe_oi,
            "expiries": [str(e) for e in exps[:8]]}


def _max_pain(rows, strikes):
    best_strike, best_pain = None, float("inf")
    for expiry_strike in strikes:
        pain = 0
        for r in rows:
            s = r["strike"]
            ce_oi = r["CE"]["oi"]; pe_oi = r["PE"]["oi"]
            if expiry_strike > s:
                pain += (expiry_strike - s) * ce_oi
            if expiry_strike < s:
                pain += (s - expiry_strike) * pe_oi
        if pain < best_pain:
            best_pain = pain; best_strike = expiry_strike
    return best_strike


def build_iron_condor(k, index, target_delta=16, wing_step=5):
    """Auto-suggest an IC from the live chain at ~target delta short strikes."""
    chain = get_chain(k, index, width=20)
    if chain.get("error"):
        return chain
    rows = chain["rows"]; step = STRIKE_STEP[index]
    td = target_delta/100
    # find short call (delta closest to +td) and short put (delta closest to -td)
    sc = min((r for r in rows if r["CE"]["delta"]>0), key=lambda r: abs(r["CE"]["delta"]-td), default=None)
    sp = min((r for r in rows if r["PE"]["delta"]<0), key=lambda r: abs(abs(r["PE"]["delta"])-td), default=None)
    if not sc or not sp:
        return {"error": "could not find delta strikes"}
    lc_strike = sc["strike"] + wing_step*step
    lp_strike = sp["strike"] - wing_step*step
    def _find(strike, t):
        return next((r for r in rows if r["strike"]==strike), None)
    lc = _find(lc_strike,"CE"); lp = _find(lp_strike,"PE")
    sc_px = sc["CE"]["ltp"]; sp_px = sp["PE"]["ltp"]
    lc_px = lc["CE"]["ltp"] if lc else 0
    lp_px = lp["PE"]["ltp"] if lp else 0
    lot = chain["lot_size"]
    net_credit = (sc_px - lc_px + sp_px - lp_px) * lot
    width_pts = wing_step*step
    max_loss = (width_pts - (sc_px-lc_px+sp_px-lp_px)) * lot
    return {
        "index": index, "spot": chain["spot"], "expiry": chain["expiry"], "dte": chain["dte"],
        "legs": {
            "short_call": {"strike": sc["strike"], "ltp": sc_px, "delta": sc["CE"]["delta"]},
            "long_call": {"strike": lc_strike, "ltp": lc_px},
            "short_put": {"strike": sp["strike"], "ltp": sp_px, "delta": sp["PE"]["delta"]},
            "long_put": {"strike": lp_strike, "ltp": lp_px},
        },
        "net_credit": round(net_credit,0), "max_loss": round(max_loss,0),
        "max_profit": round(net_credit,0),
        "profit_target_50": round(net_credit*0.5,0),
        "stop_1x": round(net_credit,0),
        "breakeven_up": sc["strike"] + (sc_px-lc_px+sp_px-lp_px),
        "breakeven_dn": sp["strike"] - (sc_px-lc_px+sp_px-lp_px),
        "lot_size": lot, "wing_width": width_pts,
        "pop_estimate": round((1 - abs(sc["CE"]["delta"]) - abs(sp["PE"]["delta"]))*100,0),
    }




# ══════════════════════════════════════════════════════════════════════════
# AI TRADE BUILDER — natural language -> structured spec -> real priced trade
# The AI parses INTENT only. The engine prices everything from the live chain.
# ══════════════════════════════════════════════════════════════════════════
def build_from_spec(k, spec: dict):
    """
    Build a real, priced trade from a structured spec (from AI or manual form).
    spec = {structure, index, short_delta?, strikes?, expiry?, wing?, lots?}
    Returns the same shape as build_iron_condor but for any supported structure.
    """
    structure = (spec.get("structure") or "iron_condor").lower().replace(" ","_")
    index = (spec.get("index") or "NIFTY").upper()
    expiry = spec.get("expiry")
    if expiry in ("nearest","this_week","weekly",None,""):
        expiry = None
    lots = int(spec.get("lots", 1))

    chain = get_chain(k, index, expiry, width=25)
    if chain.get("error"):
        return chain
    rows = chain["rows"]; spot = chain["spot"]; lot = chain["lot_size"] * lots
    step = STRIKE_STEP[index]
    r = 0.065

    def _strike_row(strike):
        return next((x for x in rows if x["strike"]==strike), None)
    def _by_delta(target, opt):
        cands = [x for x in rows if x[opt]["delta"]!=0]
        if opt=="CE":
            return min(cands, key=lambda x: abs(x["CE"]["delta"]-target/100), default=None)
        return min(cands, key=lambda x: abs(abs(x["PE"]["delta"])-target/100), default=None)

    sd = spec.get("short_delta", 16)
    wing = int(spec.get("wing", 5))
    strikes = spec.get("strikes") or {}

    legs = []
    def leg(action, opt, strike):
        row = _strike_row(strike)
        px = row[opt]["ltp"] if row else 0
        legs.append({"action":action,"type":opt,"strike":strike,"ltp":px,
                     "delta":row[opt]["delta"] if row else 0})
        return px

    net = 0
    if structure in ("iron_condor","ic"):
        scr = _by_delta(sd,"CE"); spr = _by_delta(sd,"PE")
        if not scr or not spr: return {"error":"could not find delta strikes"}
        sc,sp = scr["strike"], spr["strike"]
        net += leg("SELL","CE",sc);  net -= leg("BUY","CE",sc+wing*step)
        net += leg("SELL","PE",sp);  net -= leg("BUY","PE",sp-wing*step)
        max_loss = (wing*step - net)*lot
    elif structure in ("bull_put_spread","bull_put","put_credit_spread"):
        sp = strikes.get("short") or _by_delta(spec.get("short_delta",30),"PE")["strike"]
        lp = strikes.get("long") or (sp - wing*step)
        net += leg("SELL","PE",sp); net -= leg("BUY","PE",lp)
        max_loss = ((sp-lp) - net)*lot
    elif structure in ("bear_call_spread","bear_call","call_credit_spread"):
        sc = strikes.get("short") or _by_delta(spec.get("short_delta",30),"CE")["strike"]
        lc = strikes.get("long") or (sc + wing*step)
        net += leg("SELL","CE",sc); net -= leg("BUY","CE",lc)
        max_loss = ((lc-sc) - net)*lot
    elif structure in ("short_strangle","strangle"):
        scr=_by_delta(sd,"CE"); spr=_by_delta(sd,"PE")
        net += leg("SELL","CE",scr["strike"]); net += leg("SELL","PE",spr["strike"])
        max_loss = None  # undefined risk
    elif structure in ("short_straddle","straddle"):
        atm = chain["atm"]
        net += leg("SELL","CE",atm); net += leg("SELL","PE",atm)
        max_loss = None
    elif structure in ("iron_butterfly","iron_fly"):
        atm = chain["atm"]
        net += leg("SELL","CE",atm); net += leg("SELL","PE",atm)
        net -= leg("BUY","CE",atm+wing*step); net -= leg("BUY","PE",atm-wing*step)
        max_loss = (wing*step - net)*lot
    else:
        return {"error": f"unsupported structure: {structure}"}

    net_credit = round(net*lot, 0)
    return {
        "structure": structure, "index": index, "spot": spot,
        "expiry": chain["expiry"], "dte": chain["dte"], "lot_size": lot,
        "legs": legs, "net_credit": net_credit,
        "max_loss": round(max_loss,0) if max_loss is not None else None,
        "max_profit": net_credit,
        "profit_target_50": round(net_credit*0.5,0),
        "stop_1x": net_credit if net_credit else 0,
        "defined_risk": max_loss is not None,
        "spec_used": spec,
    }


# Lightweight rule-based intent parser (fallback when no AI key / for common phrases)
import re as _re
def parse_intent_local(prompt: str) -> dict:
    p = prompt.lower()
    spec = {"index":"NIFTY"}
    for idx in ("banknifty","finnifty","sensex","nifty"):
        if idx in p: spec["index"]=idx.upper(); break
    if "iron condor" in p or " ic " in p or p.strip()=="ic": spec["structure"]="iron_condor"
    elif "bull put" in p or "put credit" in p: spec["structure"]="bull_put_spread"
    elif "bear call" in p or "call credit" in p: spec["structure"]="bear_call_spread"
    elif "iron butterfly" in p or "iron fly" in p: spec["structure"]="iron_butterfly"
    elif "strangle" in p: spec["structure"]="short_strangle"
    elif "straddle" in p: spec["structure"]="short_straddle"
    else: spec["structure"]="iron_condor"
    m = _re.search(r'(\d+)\s*delta', p)
    if m: spec["short_delta"]=int(m.group(1))
    m = _re.search(r'wing[s]?\s*(\d+)', p)
    if m: spec["wing"]=int(m.group(1))
    # explicit strikes: "short 23500 long 23300"
    sm = _re.search(r'short\s*(\d{4,6})', p); lm = _re.search(r'long\s*(\d{4,6})', p)
    if sm or lm:
        spec["strikes"]={}
        if sm: spec["strikes"]["short"]=int(sm.group(1))
        if lm: spec["strikes"]["long"]=int(lm.group(1))
    m = _re.search(r'(\d+)\s*lot', p)
    if m: spec["lots"]=int(m.group(1))
    return spec


def register_routes(app):
    from fastapi import Depends, HTTPException
    from auth import get_current_user

    def _k(uid):
        from kite_data_patch import _kite
        k = _kite(uid)
        if not k:
            raise HTTPException(400, "Kite not connected - reconnect Zerodha")
        return k

    @app.post("/options/ai-build")
    async def ai_build(data: dict, current_user=Depends(get_current_user)):
        """
        Natural-language trade builder. Parses intent (AI if key set, else local rules),
        then builds the REAL trade from the live chain + runs the entry gate.
        """
        k = _k(current_user["id"])
        prompt = (data.get("prompt") or "").strip()
        if not prompt:
            raise HTTPException(400, "prompt required")

        # 1. Parse intent — try Gemini if available, else local rule parser
        spec = None
        try:
            import ai_gemini
            key = ai_gemini._get_key(current_user["id"])
            if key:
                sys_p = ("Parse this Indian options trade request into JSON ONLY (no prose). "
                         "Fields: structure (iron_condor|bull_put_spread|bear_call_spread|"
                         "iron_butterfly|short_strangle|short_straddle), index (NIFTY|BANKNIFTY|"
                         "FINNIFTY|SENSEX), short_delta (int, default 16), wing (int strikes, default 5), "
                         "lots (int default 1), strikes ({short:int,long:int} if explicit). "
                         "Return only valid JSON.")
                res = await ai_gemini.call_gemini(key, prompt, system=sys_p, max_tokens=200,
                                                  temperature=0, user_id=current_user["id"])
                if not res.get("error"):
                    import json as _json
                    txt = res["text"].strip().replace("```json","").replace("```","").strip()
                    spec = _json.loads(txt)
        except Exception:
            spec = None
        if not spec:
            spec = parse_intent_local(prompt)

        # 2. Build the real trade
        built = build_from_spec(k, spec)
        if built.get("error"):
            return {"error": built["error"], "spec": spec}

        # 3. Run entry gate
        gate = {}
        try:
            import income_engine
            gate = income_engine.check_entry_gate("A1", built["index"], current_user["id"])
        except Exception:
            pass
        built["entry_gate"] = gate
        built["deployable"] = bool(gate.get("allowed"))
        built["parsed_from"] = prompt
        return built

    @app.post("/options/build-spec")
    async def build_spec(data: dict, current_user=Depends(get_current_user)):
        """Build a trade from an explicit spec (manual form / adjust existing)."""
        k = _k(current_user["id"])
        built = build_from_spec(k, data)
        if not built.get("error"):
            try:
                import income_engine
                built["entry_gate"] = income_engine.check_entry_gate("A1", built["index"], current_user["id"])
                built["deployable"] = bool(built["entry_gate"].get("allowed"))
            except Exception:
                pass
        return built

    @app.get("/options/expiries")
    async def expiries(index: str = "NIFTY", current_user=Depends(get_current_user)):
        k = _k(current_user["id"])
        return {"index": index, "expiries": [str(e) for e in get_expiries(k, index.upper())]}

    @app.get("/options/expiries-classified")
    async def expiries_classified(index: str = "NIFTY", current_user=Depends(get_current_user)):
        """Expiries labeled weekly vs monthly, with day-of-week, for the Builder's picker."""
        k = _k(current_user["id"])
        exps = get_expiries(k, index.upper())
        from datetime import date as _date
        out = []
        # monthly = last expiry of each month
        by_month = {}
        for e in exps:
            by_month.setdefault((e.year, e.month), []).append(e)
        monthlies = set(max(v) for v in by_month.values())
        for e in exps:
            dte = (e - _date.today()).days
            out.append({
                "date": str(e), "dte": dte,
                "day": e.strftime("%a"), "label": e.strftime("%d %b (%a)"),
                "type": "monthly" if e in monthlies else "weekly",
            })
        return {"index": index.upper(), "expiries": out}

    @app.get("/options/chain")
    async def chain(index: str = "NIFTY", expiry: str = None, width: int = 15,
                    current_user=Depends(get_current_user)):
        k = _k(current_user["id"])
        # sanitize expiry: reject non-date junk like "Loading..."
        if expiry and (len(expiry) < 8 or not expiry[:4].isdigit()):
            expiry = None
        try:
            return get_chain(k, index.upper(), expiry, width)
        except Exception as e:
            import traceback
            return {"error": f"{type(e).__name__}: {e}", "trace": traceback.format_exc()[-800:]}

    @app.get("/options/build-ic")
    async def build_ic(index: str = "NIFTY", delta: int = 16, wing: int = 5,
                       current_user=Depends(get_current_user)):
        k = _k(current_user["id"])
        return build_iron_condor(k, index.upper(), delta, wing)
