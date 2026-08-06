"""
decision_engine.py — Core 22 Five-Signal Decision Engine
Replaces simple weight-gap comparison with composite scoring.

ADD THESE ROUTES TO main.py:
  import decision_engine
  decision_engine.register_routes(app)
"""

import math
from datetime import datetime
from typing import Optional


# ── ASOS fundamental scores (static — update weekly via Screener.in) ──────
FUNDAMENTALS = {
    "NIFTYBEES":  {"roce": 0,    "de": 0,   "note": "Index ETF"},
    "MON100":     {"roce": 0,    "de": 0,   "note": "Index ETF"},
    "JUNIORBEES": {"roce": 0,    "de": 0,   "note": "Index ETF"},
    "GOLDBEES":   {"roce": 0,    "de": 0,   "note": "ETF"},
    "SILVERETF":  {"roce": 0,    "de": 0,   "note": "ETF"},
    "CGPOWER":    {"roce": 38,   "de": 0.1, "note": "Power infra"},
    "TATAPOWER":  {"roce": 14,   "de": 1.1, "note": "Renewables — D/E elevated"},
    "BDL":        {"roce": 29,   "de": 0.0, "note": "Defence PSU"},
    "HBLENGINE":  {"roce": 31,   "de": 0.2, "note": "Battery tech"},
    "HINDCOPPER": {"roce": 18,   "de": 0.4, "note": "PSU copper"},
    "HINDALCO":   {"roce": 17,   "de": 0.7, "note": "Aluminium"},
    "ANGELONE":   {"roce": 35,   "de": 0.1, "note": "Wealth tech"},
    "FINCABLES":  {"roce": 26,   "de": 0.2, "note": "Cables"},
    "GRANULES":   {"roce": 22,   "de": 0.4, "note": "API pharma"},
    "SONACOMS":   {"roce": 28,   "de": 0.1, "note": "EV drivetrain"},
    "PRICOLLTD":  {"roce": 24,   "de": 0.3, "note": "Precision auto"},
    "INDUSINDBK": {"roce": 15,   "de": 8.0, "note": "⚠ NPA concerns — thesis review"},
    "RELIANCE":   {"roce": 12,   "de": 0.4, "note": "Conglomerate"},
    "PIRAMALFIN": {"roce": 11,   "de": 4.0, "note": "NBFC rebuild"},
    "HSCL":       {"roce": 19,   "de": 0.5, "note": "Spec chemicals"},
    "SHILCHAR":   {"roce": 38,   "de": 0.0, "note": "Transformers"},
    "GMDCLTD":    {"roce": 22,   "de": 0.1, "note": "Mining"},
}

# Stocks that can never be trimmed (Crisis Reserve + Index ETFs)
NEVER_TRIM = {"GOLDBEES", "SILVERETF", "NIFTYBEES", "JUNIORBEES", "MON100"}

# Stocks that require mandatory human review if fundamentals score −2
THESIS_REVIEW_STOCKS = {"INDUSINDBK", "TATAPOWER", "PIRAMALFIN", "RELIANCE"}


# ── Signal scoring functions ───────────────────────────────────────────────

def score_weight(current_pct: float, target_pct: float) -> int:
    """Signal 1 — Portfolio weight gap."""
    if target_pct == 0:
        return 0
    ratio = current_pct / target_pct
    if ratio > 1.20:   return -2   # >20% overweight
    if ratio > 1.10:   return -1   # 10-20% overweight
    if ratio >= 0.90:  return  0   # within ±10%
    if ratio >= 0.80:  return +1   # 10-20% underweight
    return +2                       # >20% underweight


def score_rsi(rsi: float) -> int:
    """Signal 2 — RSI momentum."""
    if rsi > 70:  return -2
    if rsi > 60:  return -1
    if rsi >= 40: return  0
    if rsi >= 30: return +1
    return +2


def score_valuation(pct_from_high: float) -> int:
    """
    Signal 3 — Valuation via 52-week high proximity.
    pct_from_high is negative (e.g., −5 means 5% below 52wk high).
    """
    p = abs(pct_from_high)  # distance below ATH
    if p < 5:   return -2   # near ATH
    if p < 15:  return -1
    if p < 25:  return  0
    if p < 35:  return +1
    return +2                # deep correction


def score_market(vix: float) -> int:
    """Signal 4 — Market conditions via VIX."""
    if vix < 12:   return -2   # complacent
    if vix < 15:   return -1
    if vix < 20:   return  0
    if vix < 25:   return +1
    return +2                   # panic


def score_fundamentals(ticker: str) -> tuple[int, str]:
    """
    Signal 5 — Fundamental quality.
    Returns (score, note). Banks/NBFCs: D/E naturally high, use different thresholds.
    """
    f = FUNDAMENTALS.get(ticker, {"roce": 22, "de": 0.5, "note": ""})
    roce = f.get("roce", 22)
    de   = f.get("de",   0.5)
    note = f.get("note", "")

    # ETF override — always neutral on fundamentals
    if ticker in ("NIFTYBEES","MON100","JUNIORBEES","GOLDBEES","SILVERETF"):
        return 0, "ETF — no fundamental signal"

    # Banking/NBFC override — D/E is leverage by nature
    if ticker in ("INDUSINDBK","PIRAMALFIN"):
        if roce > 18: return 0, "Bank/NBFC — ROCE adequate"
        return -1, f"Bank/NBFC — ROCE {roce}% below 18%"

    # Standard scoring
    if   roce > 35 and de < 0.2:   return +2, f"ROCE {roce}% exceptional, D/E {de}"
    elif roce > 28 and de < 0.5:   return +1, f"ROCE {roce}%, D/E {de}"
    elif roce > 22 and de < 1.0:   return  0, f"ROCE {roce}%, D/E {de}"
    elif roce > 18 and de < 1.5:   return -1, f"ROCE {roce}% below threshold"
    else:                           return -2, f"⚠ ROCE {roce}%, D/E {de} — thesis review"


# ── Composite engine ──────────────────────────────────────────────────────

def compute_decision(
    ticker:          str,
    current_pct:     float,
    target_pct:      float,
    rsi:             float,
    pct_from_high:   float,    # negative = below high
    vix:             float,
    nifty_below_200: bool  = False,
    nifty_rsi_below35: bool = False,
    days_to_results: int   = 99,
    is_sip_week:     bool  = False,
) -> dict:
    """
    Full 5-signal decision engine.
    Returns score, action, detail, and override flags.
    """
    etf = ticker in ("NIFTYBEES","MON100","JUNIORBEES","GOLDBEES","SILVERETF")

    # ── Compute individual signals ────────────────────────────────────────
    s1 = score_weight(current_pct, target_pct)
    s2 = 0 if etf else score_rsi(rsi)
    s3 = 0 if etf else score_valuation(pct_from_high)
    s4 = score_market(vix)
    s5, f5_note = score_fundamentals(ticker)

    base_score = s1 + s2 + s3 + s4 + s5

    # ── Market adjustments ────────────────────────────────────────────────
    adj = 0
    adj_notes = []
    if nifty_below_200:
        adj += 1
        adj_notes.append("Nifty below 200 DMA +1")
    if nifty_rsi_below35 and etf:
        adj += 2
        adj_notes.append("Nifty RSI < 35: ETF +2 (broad panic)")

    total = base_score + adj

    # ── Hard overrides ────────────────────────────────────────────────────
    overrides = []
    final_action = None

    # Never trim crisis reserve
    if ticker in NEVER_TRIM and total < 0:
        total     = max(total, 0)
        overrides.append("Crisis reserve / index ETF — trim blocked")

    # Fundamentals collapse → mandatory review
    if s5 == -2 and ticker in THESIS_REVIEW_STOCKS:
        final_action = "THESIS REVIEW"
        overrides.append(f"Fundamental score −2: {f5_note}")

    # No individual stock buys in panic (VIX > 25) — ETFs are fine
    if vix > 25 and not etf and total > 0:
        total     = min(total, +1)
        overrides.append(f"VIX {vix:.1f} > 25: individual stock buy capped at +1")

    # Earnings proximity — cap at HOLD
    if days_to_results <= 5:
        total     = min(total, 0)
        overrides.append(f"Results in {days_to_results}d — SIP only, no lump sum")

    # Structural downtrend override (>30% below 200 DMA — captured via pct_from_high proxy)
    if pct_from_high < -30 and s2 < 0:
        total = min(total, -1)
        overrides.append("Structural downtrend: score capped at −1")

    # ── Score to action ───────────────────────────────────────────────────
    if final_action is None:
        if   total >= 8:   final_action = "LADDER ENTRY"
        elif total >= 5:   final_action = "STRONG ADD"
        elif total >= 2:   final_action = "ADD"
        elif total >= -1:  final_action = "HOLD"
        elif total >= -4:  final_action = "REDUCE SIP"
        elif total >= -7:  final_action = "TRIM"
        else:              final_action = "STRONG TRIM"

    # SIP week boost — ADD becomes priority
    sip_priority = is_sip_week and final_action in ("ADD","STRONG ADD")

    # ── Action details ────────────────────────────────────────────────────
    ACTION_DETAILS = {
        "LADDER ENTRY": "Set A/B/C/D levels. Buy 2% corpus at each. Immediate GTT alert.",
        "STRONG ADD":   "Deploy 2× monthly SIP allocation this month.",
        "ADD":          "Deploy regular SIP here. Prioritise in monthly allocation.",
        "HOLD":         "Continue regular SIP. No extra allocation.",
        "REDUCE SIP":   "Skip this month's SIP here. Redeploy to ADD/STRONG ADD stocks.",
        "TRIM":         "Sell 10% of current position. Redeploy to underweight stocks.",
        "STRONG TRIM":  "Sell 20% of current position immediately.",
        "THESIS REVIEW":"Pause all activity. Review fundamentals before next action.",
    }

    # ── Color for UI ──────────────────────────────────────────────────────
    ACTION_COLORS = {
        "LADDER ENTRY": "var(--gr)", "STRONG ADD":   "var(--gr)",
        "ADD":          "var(--bl)", "HOLD":          "var(--t2)",
        "REDUCE SIP":   "var(--am)", "TRIM":          "var(--re)",
        "STRONG TRIM":  "var(--re)", "THESIS REVIEW": "var(--re)",
    }

    return {
        "ticker":       ticker,
        "action":       final_action,
        "score":        total,
        "signals": {
            "weight":       {"score": s1, "current_pct": round(current_pct, 2), "target_pct": target_pct},
            "rsi":          {"score": s2, "rsi": round(rsi, 1)},
            "valuation":    {"score": s3, "pct_from_high": round(pct_from_high, 1)},
            "market":       {"score": s4, "vix": vix},
            "fundamentals": {"score": s5, "note": f5_note},
        },
        "adjustments":  adj_notes,
        "overrides":    overrides,
        "detail":       ACTION_DETAILS.get(final_action, ""),
        "color":        ACTION_COLORS.get(final_action, "var(--t2)"),
        "sip_priority": sip_priority,
        "etf":          etf,
    }


# ── FastAPI routes ────────────────────────────────────────────────────────

def register_routes(app):
    from fastapi import Depends
    from auth import get_current_user

    @app.get("/portfolio/core22-engine")
    async def core22_engine(current_user=Depends(get_current_user)):
        """
        Full 5-signal decision engine for all Core 22 positions.
        Replaces the simple weight-gap comparison.
        """
        from main import (get_holdings, market_snapshot, CORE22_TARGETS,
                          get_db, datetime)

        # Get live data
        hold_resp = await get_holdings(current_user)
        holdings  = hold_resp.get("holdings", [])

        try:
            snap = await market_snapshot()
            vix            = snap.get("vix", 14.0)
            nifty_rsi      = snap.get("nifty_rsi") or snap.get("nifty", {}).get("rsi", 50)
            nifty_dma_gap  = snap.get("nifty_dma50_gap", 2.0)
        except Exception:
            vix, nifty_rsi, nifty_dma_gap = 14.0, 50.0, 2.0

        db   = get_db()
        row  = db.execute("SELECT * FROM user_settings WHERE user_id=?",
                          (current_user["id"],)).fetchone()
        withdraw  = row["withdrawal_amount"] if row else 0
        pending   = row["pending_credit"]    if row else 0
        corpus    = hold_resp.get("total_value", 0)
        effective = corpus - withdraw + pending

        # Market context for adjustments
        nifty_below_200  = nifty_dma_gap < 0
        nifty_rsi_low    = nifty_rsi < 35
        is_sip_week      = 1 <= datetime.now().day <= 7

        held = {h["tradingsymbol"].upper(): h for h in holdings}

        # Get RSI and 52wk data from stored buy/sell radar cache
        # (or compute fresh — slow but accurate)
        import yfinance as yf

        results = []
        for tgt in CORE22_TARGETS:
            ticker     = tgt["ticker"]
            target_pct = tgt["target_pct"]
            h          = held.get(ticker)
            val        = (h["last_price"] * h["quantity"]) if h else 0
            curr_pct   = (val / effective * 100) if effective else 0

            # Fetch RSI + 52wk for equities
            rsi         = 50.0
            pct_from_h  = -10.0   # default: 10% below high

            if ticker not in ("NIFTYBEES","MON100","JUNIORBEES","GOLDBEES","SILVERETF"):
                try:
                    hist = yf.download(ticker+".NS", period="1y", interval="1d",
                                       progress=False, auto_adjust=True)
                    if not hist.empty and len(hist) > 20:
                        close    = hist["Close"].squeeze().dropna()
                        price    = float(close.iloc[-1])
                        high52   = float(close.max())
                        delta    = close.diff()
                        gain     = delta.clip(lower=0).ewm(com=13, adjust=False).mean()
                        loss     = (-delta.clip(upper=0)).ewm(com=13, adjust=False).mean()
                        rsi      = float((100 - 100/(1+gain/loss)).iloc[-1])
                        pct_from_h = (price - high52) / high52 * 100
                except Exception:
                    pass

            decision = compute_decision(
                ticker          = ticker,
                current_pct     = curr_pct,
                target_pct      = target_pct,
                rsi             = rsi,
                pct_from_high   = pct_from_h,
                vix             = vix,
                nifty_below_200 = nifty_below_200,
                nifty_rsi_below35 = nifty_rsi_low,
                is_sip_week     = is_sip_week,
            )
            decision["monthly_sip"]  = tgt["sip"]
            decision["bucket"]       = tgt["bucket"]
            decision["role"]         = tgt["role"]
            decision["held"]         = bool(h)
            decision["current_val"]  = round(val, 0)
            decision["target_val"]   = round(effective * target_pct / 100, 0)
            results.append(decision)

        # Sort: TRIM/STRONG TRIM first, then LADDER/STRONG ADD, then HOLD
        priority = {"STRONG TRIM":0,"TRIM":1,"THESIS REVIEW":2,
                    "LADDER ENTRY":3,"STRONG ADD":4,"ADD":5,"HOLD":6,"REDUCE SIP":7}
        results.sort(key=lambda x: priority.get(x["action"], 9))

        # SIP allocation for this month
        sip_total = row["sip_amount"] if row else 100000
        add_stocks = [r for r in results if r["action"] in
                      ("STRONG ADD","ADD","LADDER ENTRY") and not r.get("etf")]
        if add_stocks:
            # Weight allocation by score (higher score = more SIP)
            total_w = sum(max(r["score"], 1) for r in add_stocks)
            for r in add_stocks:
                r["sip_this_month"] = round(sip_total * max(r["score"],1) / total_w / 1000) * 1000

        return {
            "decisions":      results,
            "vix":            vix,
            "nifty_rsi":      nifty_rsi,
            "nifty_phase":    "BEAR" if nifty_below_200 else "BULL",
            "effective_corpus": round(effective, 0),
            "is_sip_week":    is_sip_week,
            "trim_count":     sum(1 for r in results if "TRIM" in r["action"]),
            "add_count":      sum(1 for r in results if "ADD" in r["action"]),
            "timestamp":      datetime.now().isoformat(),
        }