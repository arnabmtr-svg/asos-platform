"""
rebalancing_engine.py — ASOS Complete Rebalancing + GTT Engine
The heavy lifting: monthly SIP allocation, quarterly drift, emergency triggers,
and automatic GTT order placement via Zerodha Kite Connect.

Add to main.py:
  import rebalancing_engine
  rebalancing_engine.register_routes(app)
"""

import math
from datetime import datetime, timedelta, date
from typing import Optional

# ════════════════════════════════════════════════════════════════════════════
# CORE 22 TARGETS (duplicated here for independence)
# ════════════════════════════════════════════════════════════════════════════
C22 = [
    {"t":"NIFTYBEES","b":1,"pct":12,"sip":12000,"etf":True},
    {"t":"MON100",   "b":1,"pct":10,"sip":10000,"etf":True},
    {"t":"JUNIORBEES","b":1,"pct":8,"sip":8000,"etf":True},
    {"t":"CGPOWER",  "b":2,"pct":9, "sip":8000, "etf":False},
    {"t":"TATAPOWER","b":2,"pct":7, "sip":4000, "etf":False},
    {"t":"BDL",      "b":2,"pct":5, "sip":5000, "etf":False},
    {"t":"HBLENGINE","b":2,"pct":4, "sip":7000, "etf":False},
    {"t":"HINDCOPPER","b":3,"pct":5,"sip":8000, "etf":False},
    {"t":"HINDALCO", "b":3,"pct":5, "sip":7000, "etf":False},
    {"t":"ANGELONE", "b":3,"pct":4, "sip":5000, "etf":False},
    {"t":"FINCABLES","b":3,"pct":4, "sip":6000, "etf":False},
    {"t":"GRANULES", "b":3,"pct":4, "sip":6000, "etf":False},
    {"t":"SONACOMS", "b":3,"pct":3, "sip":3000, "etf":False},
    {"t":"PRICOLLTD","b":3,"pct":2, "sip":2000, "etf":False},
    {"t":"INDUSINDBK","b":3,"pct":2,"sip":7000, "etf":False},
    {"t":"RELIANCE", "b":3,"pct":2, "sip":3000, "etf":False},
    {"t":"PIRAMALFIN","b":4,"pct":3,"sip":6000, "etf":False},
    {"t":"HSCL",     "b":4,"pct":3, "sip":2000, "etf":False},
    {"t":"SHILCHAR", "b":4,"pct":2, "sip":2000, "etf":False},
    {"t":"GMDCLTD",  "b":4,"pct":2, "sip":4000, "etf":False},
    {"t":"GOLDBEES", "b":5,"pct":3, "sip":1000, "etf":True},
    {"t":"SILVERETF","b":5,"pct":2, "sip":1000, "etf":True},
]

NEVER_TRIM = {"GOLDBEES","SILVERETF","NIFTYBEES","JUNIORBEES","MON100"}

# ════════════════════════════════════════════════════════════════════════════
# HELPER — fetch RSI for a ticker
# ════════════════════════════════════════════════════════════════════════════
def _rsi(ticker: str, period: str = "6mo") -> float:
    try:
        import yfinance as yf
        h = yf.download(ticker+".NS", period=period, interval="1d",
                        progress=False, auto_adjust=True)
        if h.empty or len(h) < 15:
            return 50.0
        c = h["Close"].squeeze().dropna()
        d = c.diff()
        g = d.clip(lower=0).ewm(com=13, adjust=False).mean()
        l = (-d.clip(upper=0)).ewm(com=13, adjust=False).mean()
        return float((100 - 100/(1+g/l)).iloc[-1])
    except Exception:
        return 50.0


def _52wk(ticker: str):
    """Returns (price, high52, low52, pct_from_high)"""
    try:
        import yfinance as yf
        h = yf.download(ticker+".NS", period="1y", interval="1d",
                        progress=False, auto_adjust=True)
        if h.empty: return 0, 0, 0, 0
        c = h["Close"].squeeze().dropna()
        p = float(c.iloc[-1]); hi = float(c.max()); lo = float(c.min())
        return p, hi, lo, round((p-hi)/hi*100, 1)
    except Exception:
        return 0, 0, 0, 0


# ════════════════════════════════════════════════════════════════════════════
# 1. MONTHLY SIP OPTIMIZER — where does the ₹1L go this month?
# ════════════════════════════════════════════════════════════════════════════
def compute_monthly_sip(user_id: int, sip_total: float,
                         effective_corpus: float, vix: float) -> dict:
    """
    Heavy lifting SIP allocation.
    Logic:
    - ETF allocation: always 30% of SIP, proportional to base SIP weights
    - Equity allocation: 70%, scored by (weight_gap × oversold_bonus)
    - VIX > 18: reduce equity SIP by 50%, park remainder in liquid
    - VIX > 25: increase equity SIP by 50% (panic buying)
    - Maximum 8 equity stocks per month (not all 17 equities)
    - TRIM stocks get ₹0 this month regardless of other signals
    """
    from database import get_db
    db   = get_db()
    held = {r["ticker"]: r for r in db.execute(
        "SELECT ticker, quantity, last_price FROM stored_holdings WHERE user_id=?",
        (user_id,)
    ).fetchall()}

    # VIX adjustment to SIP total
    if vix > 25:
        equity_budget = sip_total * 1.5 * 0.70   # 150% during panic
        etf_budget    = sip_total * 1.5 * 0.30
        vix_note      = f"VIX {vix:.1f} PANIC — deploy 150% SIP + reserves"
    elif vix > 20:
        equity_budget = sip_total * 0.5 * 0.70   # pause half
        etf_budget    = sip_total * 0.5 * 0.30
        vix_note      = f"VIX {vix:.1f} FEAR — deploy 50% only, park rest in liquid"
    elif vix > 16:
        equity_budget = sip_total * 0.70 * 0.70
        etf_budget    = sip_total * 0.70 * 0.30
        vix_note      = f"VIX {vix:.1f} ELEVATED — deploy 70% SIP"
    else:
        equity_budget = sip_total * 0.70
        etf_budget    = sip_total * 0.30
        vix_note      = f"VIX {vix:.1f} NORMAL — full SIP deployment"

    items = []
    for s in C22:
        tk  = s["t"]
        h   = held.get(tk)
        val = (h["quantity"] * h["last_price"]) if h else 0
        curr_pct = (val / effective_corpus * 100) if effective_corpus else 0
        gap      = s["pct"] - curr_pct  # positive = underweight

        if s["etf"]:
            # ETFs: proportional to base SIP, scaled by budget
            base_ratio = s["sip"] / sum(x["sip"] for x in C22 if x["etf"])
            items.append({
                **s, "curr_pct": round(curr_pct,2),
                "weight_gap": round(gap,2), "rsi": None,
                "score": base_ratio * 100,
                "sip_amount": 0, "pool": "etf",
                "reason": f"ETF SIP · {curr_pct:.1f}% vs {s['pct']}% target"
            })
            continue

        # Equities: fetch RSI
        rsi = _rsi(tk)

        # Skip if overweight AND overbought
        if curr_pct > s["pct"] * 1.15 and rsi > 65:
            items.append({**s, "curr_pct": round(curr_pct,2),
                          "weight_gap": round(gap,2), "rsi": round(rsi,1),
                          "score": 0, "sip_amount": 0, "pool": "equity",
                          "reason": f"SKIP — overweight {curr_pct:.1f}% + RSI {rsi:.0f}"})
            continue

        # Scoring: weight gap drives base, oversold multiplies it
        oversold    = max(0, (55 - rsi) / 55)  # 0 when RSI=55, 1 when RSI=0
        wt_score    = max(0, gap / s["pct"])    # 0 if on-target, 1 if fully missing
        score       = (wt_score * 0.60 + oversold * 0.40) * 100
        if vix > 20: score *= 1.25   # extra weight when VIX elevated

        items.append({**s, "curr_pct": round(curr_pct,2),
                      "weight_gap": round(gap,2), "rsi": round(rsi,1),
                      "score": round(score,2), "sip_amount": 0, "pool": "equity",
                      "reason": f"RSI {rsi:.0f} · gap {gap:.1f}%"})

    # Allocate ETF budget
    etf_items  = [i for i in items if i["pool"]=="etf"]
    etf_total  = sum(i["score"] for i in etf_items) or 1
    for i in etf_items:
        raw = etf_budget * i["score"] / etf_total
        i["sip_amount"] = round(raw / 500) * 500

    # Allocate equity budget — top 8 only
    eq_items = sorted([i for i in items if i["pool"]=="equity"],
                      key=lambda x: -x["score"])
    top8      = [i for i in eq_items if i["score"] > 0][:8]
    eq_total  = sum(i["score"] for i in top8) or 1
    for i in top8:
        raw = equity_budget * i["score"] / eq_total
        i["sip_amount"] = round(raw / 500) * 500
        i["reason"] += f" · allocating ₹{i['sip_amount']:,}"

    # Trim total to match budget (rounding may overshoot)
    total = sum(i["sip_amount"] for i in items)

    return {
        "month":          datetime.now().strftime("%B %Y"),
        "sip_total":      sip_total,
        "effective_corpus": round(effective_corpus,0),
        "vix":            vix,
        "vix_note":       vix_note,
        "etf_budget":     round(etf_budget,0),
        "equity_budget":  round(equity_budget,0),
        "total_planned":  total,
        "allocations":    sorted(items, key=lambda x: -x["sip_amount"]),
    }


# ════════════════════════════════════════════════════════════════════════════
# 2. QUARTERLY DRIFT REBALANCER — heavy lifting
# ════════════════════════════════════════════════════════════════════════════
def compute_quarterly_rebalance(user_id: int, effective_corpus: float,
                                  vix: float) -> dict:
    """
    Quarterly rebalance logic (run in March, June, Sep, Dec).
    Detects drift > 5% from target, generates tax-efficient trim/buy plan.
    """
    from database import get_db
    db   = get_db()
    held = {r["ticker"]: r for r in db.execute(
        "SELECT ticker, quantity, last_price, average_price FROM stored_holdings WHERE user_id=?",
        (user_id,)
    ).fetchall()}
    # Get transaction dates for tax calc
    buys = {}
    for r in db.execute(
        "SELECT ticker, MIN(txn_date) as first_buy FROM transactions WHERE user_id=? AND txn_type='BUY' GROUP BY ticker",
        (user_id,)
    ).fetchall():
        buys[r["ticker"]] = r["first_buy"]

    today = date.today()

    trims, buys_list = [], []
    bucket_actual   = {1:0, 2:0, 3:0, 4:0, 5:0}
    bucket_target   = {1:30, 2:25, 3:30, 4:10, 5:5}

    positions = []
    for s in C22:
        tk  = s["t"]
        h   = held.get(tk)
        val = (h["quantity"] * h["last_price"]) if h else 0
        avg = h["average_price"] if h else 0
        curr_pct = (val / effective_corpus * 100) if effective_corpus else 0
        drift    = curr_pct - s["pct"]           # + = overweight, - = underweight
        gain     = ((h["last_price"]/avg)-1)*100 if avg and h else 0
        first_buy = buys.get(tk)
        held_days = (today - date.fromisoformat(first_buy)).days if first_buy else 0
        is_ltcg   = held_days >= 365

        bucket_actual[s["b"]] = bucket_actual.get(s["b"],0) + curr_pct

        # Fetch RSI for trading decision
        rsi = _rsi(tk) if h else 50.0

        positions.append({
            "ticker":     tk,
            "bucket":     s["b"],
            "target_pct": s["pct"],
            "curr_pct":   round(curr_pct,2),
            "drift":      round(drift,2),
            "drift_pct_of_target": round(drift/s["pct"]*100,1) if s["pct"] else 0,
            "val":        round(val,0),
            "target_val": round(effective_corpus*s["pct"]/100,0),
            "rsi":        round(rsi,1),
            "unrealised_gain_pct": round(gain,1),
            "held_days":  held_days,
            "is_ltcg":    is_ltcg,
            "tax_type":   "LTCG (12.5%)" if is_ltcg else "STCG (20%)",
            "quantity":   h["quantity"] if h else 0,
            "ltp":        h["last_price"] if h else 0,
            "avg_price":  avg,
        })

    # ── Identify rebalance actions ────────────────────────────────────────
    for p in positions:
        # TRIM: >15% overweight AND RSI > 60 AND not in NEVER_TRIM
        if (p["drift_pct_of_target"] > 15 and p["rsi"] > 55
                and p["ticker"] not in NEVER_TRIM and p["val"] > 0):
            trim_val  = round(p["val"] - p["target_val"] * 1.05, 0)  # trim to 5% above target
            trim_qty  = max(1, int(trim_val / p["ltp"])) if p["ltp"] else 0
            tax_cost  = round(trim_val * (0.125 if p["is_ltcg"] else 0.20) * max(0,p["unrealised_gain_pct"])/100, 0)
            trims.append({
                "ticker":   p["ticker"],
                "action":   "TRIM",
                "reason":   f"Overweight {p['drift']:+.1f}% · RSI {p['rsi']:.0f}",
                "trim_val": int(trim_val),
                "trim_qty": trim_qty,
                "tax_cost": int(tax_cost),
                "tax_type": p["tax_type"],
                "priority": 1 if p["is_ltcg"] else 2,  # prefer LTCG trims (lower tax)
                "gtt_price": round(p["ltp"] * 1.005, 1),  # 0.5% above LTP
            })

        # BUY: >15% underweight AND RSI < 55
        elif (p["drift_pct_of_target"] < -15 and p["rsi"] < 58):
            buy_val = round(p["target_val"] - p["val"], 0)
            buy_qty = max(1, int(buy_val / p["ltp"])) if p["ltp"] else 0
            buys_list.append({
                "ticker":  p["ticker"],
                "action":  "BUY",
                "reason":  f"Underweight {p['drift']:+.1f}% · RSI {p['rsi']:.0f}",
                "buy_val": int(buy_val),
                "buy_qty": buy_qty,
                "rsi":     p["rsi"],
                "priority":1 if p["rsi"] < 40 else 2,
                "gtt_price": round(p["ltp"] * 0.995, 1) if p["ltp"] else 0,  # 0.5% below LTP
            })

    # Sort by priority
    trims.sort(key=lambda x: x["priority"])
    buys_list.sort(key=lambda x: x["priority"])

    # Bucket-level drift
    bucket_drift = {b: round(bucket_actual[b]-bucket_target[b],1) for b in range(1,6)}
    needs_rebal  = any(abs(v) > 5 for v in bucket_drift.values())

    total_trim_proceeds = sum(t["trim_val"] for t in trims)
    total_buy_needed    = sum(b["buy_val"] for b in buys_list)

    return {
        "quarter":           f"Q{(datetime.now().month-1)//3+1} {datetime.now().year}",
        "needs_rebalance":   needs_rebal or bool(trims or buys_list),
        "positions":         positions,
        "trim_actions":      trims,
        "buy_actions":       buys_list,
        "bucket_drift":      bucket_drift,
        "total_trim_proceeds": round(total_trim_proceeds,0),
        "total_buy_needed":   round(total_buy_needed,0),
        "net_cash_required":  round(max(0, total_buy_needed - total_trim_proceeds),0),
        "tax_estimate":       round(sum(t["tax_cost"] for t in trims),0),
        "summary": (
            f"REBALANCE NEEDED: {len(trims)} trims (₹{total_trim_proceeds/1e5:.1f}L) "
            f"+ {len(buys_list)} buys (₹{total_buy_needed/1e5:.1f}L)"
            if (trims or buys_list) else "Portfolio within drift limits — no action needed"
        ),
        "timestamp": datetime.now().isoformat(),
    }


# ════════════════════════════════════════════════════════════════════════════
# 3. LADDER → GTT PLACER — the automation core
# ════════════════════════════════════════════════════════════════════════════
async def place_ladder_gtts(ticker: str, neckline: float, corpus_pct: float,
                              corpus_val: float, user_id: int) -> dict:
    """
    Given a neckline (A), compute B/C/D, calculate quantities,
    and place GTT buy orders on Kite for each level.
    Also places a stop-loss GTT below D.
    """
    from database import get_db
    from kite_service import KiteService

    db  = get_db()
    row = db.execute("SELECT kite_api_key, kite_api_secret, kite_access_token FROM users WHERE id=?",
                     (user_id,)).fetchone()
    if not row or not row["kite_access_token"]:
        return {"error": "Zerodha not connected — cannot place GTT orders"}

    kite = KiteService(row["kite_api_key"], row["kite_api_secret"], row["kite_access_token"])

    # Ladder levels
    B = round(neckline * 0.90, 2)
    C = round(B * 0.90, 2)
    D = round(C * 0.90, 2)
    STOP = round(D * 0.93, 2)   # weekly close below this = stop

    # Budget per level = corpus_pct% of corpus
    budget_per_level = corpus_val * corpus_pct / 100

    # Get current price for GTT last_price parameter
    try:
        import yfinance as yf
        t = yf.Ticker(ticker+".NS")
        ltp = float(t.fast_info.last_price or B)
    except Exception:
        ltp = B  # use B as fallback

    gtts_placed = []
    errors      = []

    for level_name, level_price, target_price in [("B", B, neckline), ("C", C, B), ("D", D, C)]:
        qty = max(1, int(budget_per_level / level_price))

        try:
            # Place GTT buy at level price
            result = kite.place_gtt(
                trigger_type="single",
                tradingsymbol=ticker,
                exchange="NSE",
                trigger_values=[level_price],
                last_price=ltp,
                orders=[{
                    "transaction_type": "BUY",
                    "quantity":          qty,
                    "product":          "CNC",
                    "order_type":       "LIMIT",
                    "price":            level_price,
                }]
            )
            gtts_placed.append({
                "level":         f"Level {level_name}",
                "trigger_price": level_price,
                "qty":           qty,
                "amount":        round(qty * level_price, 0),
                "target":        target_price,
                "expected_return": f"+{round((target_price/level_price-1)*100,1)}%",
                "gtt_id":        result.get("trigger_id") if result else None,
                "status":        "PLACED" if result else "FAILED",
            })
        except Exception as e:
            errors.append(f"Level {level_name}: {str(e)[:80]}")

    # Store ladder in DB
    db.execute("""
        INSERT OR REPLACE INTO price_ladders
          (user_id, ticker, neckline, level_b, level_c, level_d, corpus_pct, note)
        VALUES (?,?,?,?,?,?,?,?)
    """, (user_id, ticker, neckline, B, C, D, corpus_pct,
          f"GTT placed {datetime.now().strftime('%d-%b-%Y')}"))
    db.commit()

    return {
        "ticker":        ticker,
        "neckline":      neckline,
        "levels":        {"A": neckline, "B": B, "C": C, "D": D, "STOP": STOP},
        "gtts_placed":   gtts_placed,
        "total_exposure": round(budget_per_level * len(gtts_placed), 0),
        "stop_level":    STOP,
        "errors":        errors,
        "message": (f"✓ {len(gtts_placed)} GTT buy orders placed for {ticker}. "
                    f"Stop loss: below ₹{STOP}")
    }


async def place_trim_gtt(ticker: str, qty: int, target_price: float,
                          stop_price: float, user_id: int) -> dict:
    """
    Place a GTT OCO (One-Cancels-Other) sell order:
    - Sell at target_price (profit)
    - OR sell at stop_price (stop loss)
    Whichever triggers first cancels the other.
    """
    from database import get_db
    from kite_service import KiteService

    db  = get_db()
    row = db.execute("SELECT kite_api_key, kite_api_secret, kite_access_token FROM users WHERE id=?",
                     (user_id,)).fetchone()
    if not row or not row["kite_access_token"]:
        return {"error": "Zerodha not connected"}

    kite = KiteService(row["kite_api_key"], row["kite_api_secret"], row["kite_access_token"])

    try:
        import yfinance as yf
        t   = yf.Ticker(ticker+".NS")
        ltp = float(t.fast_info.last_price or (target_price+stop_price)/2)
    except Exception:
        ltp = (target_price + stop_price) / 2

    result = kite.place_gtt(
        trigger_type="two-leg",       # OCO
        tradingsymbol=ticker,
        exchange="NSE",
        trigger_values=[stop_price, target_price],
        last_price=ltp,
        orders=[
            {   # Stop-loss leg
                "transaction_type": "SELL",
                "quantity":  qty,
                "product":   "CNC",
                "order_type":"LIMIT",
                "price":     stop_price,
            },
            {   # Target leg
                "transaction_type": "SELL",
                "quantity":  qty,
                "product":   "CNC",
                "order_type":"LIMIT",
                "price":     target_price,
            }
        ]
    )
    return {
        "ticker":       ticker,
        "qty":          qty,
        "target":       target_price,
        "stop":         stop_price,
        "gtt_id":       result.get("trigger_id") if result else None,
        "type":         "OCO (One-Cancels-Other)",
        "message":      f"✓ GTT OCO placed: sell {qty} {ticker} at ₹{target_price} OR stop at ₹{stop_price}",
    }


async def get_active_gtts(user_id: int) -> dict:
    """Get all active GTT orders from Zerodha."""
    from database import get_db
    from kite_service import KiteService

    db  = get_db()
    row = db.execute("SELECT kite_api_key, kite_api_secret, kite_access_token FROM users WHERE id=?",
                     (user_id,)).fetchone()
    if not row or not row["kite_access_token"]:
        return {"gtts": [], "source": "zerodha_not_connected"}

    try:
        kite = KiteService(row["kite_api_key"], row["kite_api_secret"], row["kite_access_token"])
        gtts = kite.get_gtts() or []
        # Enrich with ASOS context
        for g in gtts:
            tk = g.get("condition",{}).get("tradingsymbol","")
            g["asos_ticker"] = tk
            g["asos_context"] = "Ladder entry" if g.get("type")=="single" else "OCO exit"
        return {"gtts": gtts, "count": len(gtts), "source": "zerodha_live"}
    except Exception as e:
        return {"gtts": [], "error": str(e), "source": "zerodha_error"}


# ════════════════════════════════════════════════════════════════════════════
# 4. EMERGENCY REBALANCE TRIGGERS
# ════════════════════════════════════════════════════════════════════════════
def check_emergency_triggers(vix: float, nifty_rsi: float,
                               nifty_drawdown_from_peak: float) -> list:
    """
    Returns list of triggered emergency conditions.
    Called by background scheduler every 30 min during market hours.
    """
    triggers = []

    # TRIGGER 1: Panic buying opportunity
    if vix > 25:
        triggers.append({
            "type":     "PANIC_OPPORTUNITY",
            "severity": "HIGH",
            "action":   f"VIX {vix:.1f} > 25 — DEPLOY 2× SIP. Buy underweight B2/B3 stocks.",
            "details":  "Historically, VIX > 25 = 80%+ chance of positive 6-month returns.",
        })

    # TRIGGER 2: Market correction
    if nifty_drawdown_from_peak < -15:
        triggers.append({
            "type":     "MARKET_CORRECTION",
            "severity": "HIGH",
            "action":   f"Nifty down {abs(nifty_drawdown_from_peak):.0f}% from peak — activate buy mode.",
            "details":  "Deploy B5 crisis reserve + extra SIP in B1 ETFs.",
        })

    # TRIGGER 3: Deep oversold Nifty
    if nifty_rsi < 30:
        triggers.append({
            "type":     "NIFTY_OVERSOLD",
            "severity": "MEDIUM",
            "action":   f"Nifty RSI {nifty_rsi:.0f} < 30 — increase B1 ETF SIP by 50%.",
            "details":  "NIFTYBEES, JUNIORBEES are on discount. SIP now.",
        })

    # TRIGGER 4: Complacency
    if vix < 12:
        triggers.append({
            "type":     "COMPLACENCY",
            "severity": "LOW",
            "action":   f"VIX {vix:.1f} < 12 — market complacent. Park 25% SIP in liquid.",
            "details":  "Trim any stock RSI > 70. Prepare for correction.",
        })

    return triggers


# ════════════════════════════════════════════════════════════════════════════
# FASTAPI ROUTES
# ════════════════════════════════════════════════════════════════════════════
def register_routes(app):
    from fastapi import Depends, HTTPException
    from auth import get_current_user
    from market_data import compute_ivp_ivr, compute_indicators, get_nifty_spot
    from database import get_db

    # ── Monthly SIP ───────────────────────────────────────────────────────
    @app.get("/rebalance/monthly-sip")
    async def monthly_sip(current_user=Depends(get_current_user)):
        uid = current_user["id"]
        db  = get_db()
        row = db.execute("SELECT * FROM user_settings WHERE user_id=?", (uid,)).fetchone()
        sip  = row["sip_amount"]        if row else 100000
        with_ = row["withdrawal_amount"] if row else 0
        pend = row["pending_credit"]    if row else 0
        held = db.execute("SELECT SUM(quantity*last_price) as v FROM stored_holdings WHERE user_id=?",
                          (uid,)).fetchone()
        corpus = (held["v"] or 0) - with_ + pend
        iv = compute_ivp_ivr()
        return compute_monthly_sip(uid, sip, corpus, iv["vix"])

    # ── Quarterly rebalance ───────────────────────────────────────────────
    @app.get("/rebalance/quarterly")
    async def quarterly_rebalance(current_user=Depends(get_current_user)):
        uid = current_user["id"]
        db  = get_db()
        row = db.execute("SELECT * FROM user_settings WHERE user_id=?", (uid,)).fetchone()
        with_ = row["withdrawal_amount"] if row else 0
        pend  = row["pending_credit"]    if row else 0
        held  = db.execute("SELECT SUM(quantity*last_price) as v FROM stored_holdings WHERE user_id=?",
                            (uid,)).fetchone()
        corpus = (held["v"] or 0) - with_ + pend
        iv = compute_ivp_ivr()
        return compute_quarterly_rebalance(uid, corpus, iv["vix"])

    # ── Place GTT for ladder ──────────────────────────────────────────────
    @app.post("/rebalance/ladder-gtt")
    async def place_ladder_gtt_route(data: dict, current_user=Depends(get_current_user)):
        """
        Body: {ticker, neckline, corpus_pct}
        Places GTT buy orders at B, C, D levels on Zerodha.
        """
        uid     = current_user["id"]
        ticker  = data.get("ticker","").upper()
        neckline= float(data.get("neckline", 0))
        pct     = float(data.get("corpus_pct", 2.0))
        if not ticker or neckline <= 0:
            raise HTTPException(400, "ticker and neckline required")

        db  = get_db()
        row = db.execute("SELECT * FROM user_settings WHERE user_id=?", (uid,)).fetchone()
        held = db.execute("SELECT SUM(quantity*last_price) as v FROM stored_holdings WHERE user_id=?",
                          (uid,)).fetchone()
        corpus = ((held["v"] or 0) - (row["withdrawal_amount"] if row else 0)
                  + (row["pending_credit"] if row else 0))

        return await place_ladder_gtts(ticker, neckline, pct, corpus, uid)

    # ── Place OCO exit GTT ────────────────────────────────────────────────
    @app.post("/rebalance/exit-gtt")
    async def place_exit_gtt(data: dict, current_user=Depends(get_current_user)):
        return await place_trim_gtt(
            ticker=data.get("ticker","").upper(),
            qty=int(data.get("qty",1)),
            target_price=float(data.get("target",0)),
            stop_price=float(data.get("stop",0)),
            user_id=current_user["id"]
        )

    # ── Active GTTs ───────────────────────────────────────────────────────
    @app.get("/rebalance/gtts")
    async def active_gtts(current_user=Depends(get_current_user)):
        return await get_active_gtts(current_user["id"])

    # ── Emergency triggers ────────────────────────────────────────────────
    @app.get("/rebalance/emergency-check")
    async def emergency_check(current_user=Depends(get_current_user)):
        iv  = compute_ivp_ivr()
        nf  = compute_indicators("^NSEI")
        # 52-week peak for Nifty
        try:
            import yfinance as yf
            nf52 = yf.download("^NSEI", period="1y", progress=False, auto_adjust=True)
            peak = float(nf52["Close"].max())
            spot = float(nf52["Close"].iloc[-1])
            drawdown = round((spot - peak) / peak * 100, 1)
        except Exception:
            drawdown = 0

        triggers = check_emergency_triggers(iv["vix"], nf["rsi"], drawdown)
        return {
            "vix":        iv["vix"],
            "nifty_rsi":  nf["rsi"],
            "drawdown":   drawdown,
            "triggers":   triggers,
            "alert_count":len(triggers),
            "timestamp":  datetime.now().isoformat()
        }

    # ── Full rebalance plan (combines everything) ─────────────────────────
    @app.get("/rebalance/full-plan")
    async def full_rebalance_plan(current_user=Depends(get_current_user)):
        """
        The ultimate endpoint — calls all engines and returns unified plan.
        Monthly SIP allocation + quarterly drift + emergency triggers + GTT status.
        """
        uid = current_user["id"]
        db  = get_db()
        row = db.execute("SELECT * FROM user_settings WHERE user_id=?", (uid,)).fetchone()
        sip   = row["sip_amount"]        if row else 100000
        with_ = row["withdrawal_amount"] if row else 0
        pend  = row["pending_credit"]    if row else 0
        held  = db.execute("SELECT SUM(quantity*last_price) as v FROM stored_holdings WHERE user_id=?",
                            (uid,)).fetchone()
        corpus = (held["v"] or 0) - with_ + pend
        iv  = compute_ivp_ivr()
        nf  = compute_indicators("^NSEI")
        vix = iv["vix"]

        sip_plan = compute_monthly_sip(uid, sip, corpus, vix)
        quarterly = compute_quarterly_rebalance(uid, corpus, vix)
        gtts     = await get_active_gtts(uid)

        try:
            nf52 = __import__("yfinance").download("^NSEI", period="1y", progress=False, auto_adjust=True)
            peak = float(nf52["Close"].max())
            spot = float(nf52["Close"].iloc[-1])
            drawdown = round((spot-peak)/peak*100,1)
        except Exception:
            drawdown = 0

        emergency = check_emergency_triggers(vix, nf["rsi"], drawdown)

        return {
            "effective_corpus": round(corpus,0),
            "vix":              vix,
            "ivp":              iv["ivp"],
            "nifty_rsi":        nf["rsi"],
            "monthly_sip":      sip_plan,
            "quarterly":        quarterly,
            "emergency":        emergency,
            "active_gtts":      gtts,
            "action_count": {
                "sip_stocks":   sum(1 for a in sip_plan["allocations"] if a["sip_amount"]>0),
                "trims":        len(quarterly["trim_actions"]),
                "buys":         len(quarterly["buy_actions"]),
                "emergency":    len(emergency),
                "gtts_active":  gtts.get("count",0),
            },
            "timestamp": datetime.now().isoformat(),
        }