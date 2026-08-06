"""
athena_dashboard.py — Sprint 1: The Thinking Engine
The SHARED signal engine that powers BOTH Dashboard and Buy/Sell Radar,
so they never contradict each other.

Combines:
  - Live Kite prices/RSI (from kite_data_patch)
  - Cached fundamentals (from screener_fetch)  <- "best for long term" check
  - Core 22 DB targets (from athena_core)

Produces per-stock a LONG-TERM QUALITY SCORE + a live SIGNAL, and a
portfolio-level HEALTH SCORE + prioritised ACTIONS.

Add to main.py:
  import athena_dashboard
  athena_dashboard.register_routes(app)
"""
from datetime import datetime, date
import math

# ── Long-term quality scoring (the "best for long term" mandate) ───────────
def quality_score(fund: dict) -> dict:
    """
    Score a stock 0-100 on long-term quality from fundamentals.
    Valuation is the MOTHER of investment (PD-3) — it's weighted heavily.
    """
    if not fund or not fund.get("fetch_ok", 1):
        return {"score": None, "grade": "NO DATA", "breakdown": {}, "flags": ["No fundamental data"]}

    roce = fund.get("roce")
    roe  = fund.get("roe")
    de   = fund.get("de")
    pe   = fund.get("pe")
    sales_g = fund.get("sales_growth_3y")
    profit_g = fund.get("profit_growth_3y")
    promoter = fund.get("promoter_pct")
    pledge = fund.get("promoter_pledge") or 0

    b = {}   # breakdown
    flags = []

    # ROCE (25 pts) — capital efficiency, the #1 quality metric
    if roce is None: b["roce"] = 10
    elif roce >= 25: b["roce"] = 25
    elif roce >= 20: b["roce"] = 21
    elif roce >= 15: b["roce"] = 16
    elif roce >= 12: b["roce"] = 10
    else: b["roce"] = 4; flags.append(f"Low ROCE {roce}%")

    # Profit growth 3y (20 pts)
    if profit_g is None: b["profit"] = 8
    elif profit_g >= 25: b["profit"] = 20
    elif profit_g >= 15: b["profit"] = 16
    elif profit_g >= 8: b["profit"] = 11
    elif profit_g >= 0: b["profit"] = 6
    else: b["profit"] = 2; flags.append(f"Profit declining {profit_g}%")

    # Sales growth 3y (15 pts)
    if sales_g is None: b["sales"] = 6
    elif sales_g >= 20: b["sales"] = 15
    elif sales_g >= 12: b["sales"] = 12
    elif sales_g >= 6: b["sales"] = 8
    else: b["sales"] = 3

    # Valuation (20 pts) — PD-3, the mother. High PE = expensive = lower score
    if pe is None: b["valuation"] = 8
    elif pe <= 15: b["valuation"] = 20
    elif pe <= 25: b["valuation"] = 17
    elif pe <= 40: b["valuation"] = 12
    elif pe <= 60: b["valuation"] = 7
    elif pe <= 90: b["valuation"] = 3; flags.append(f"Expensive PE {pe}")
    else: b["valuation"] = 1; flags.append(f"Very expensive PE {pe}")

    # Promoter holding + pledge (10 pts)
    if promoter is None: b["promoter"] = 5
    elif promoter >= 50: b["promoter"] = 10
    elif promoter >= 40: b["promoter"] = 7
    elif promoter >= 30: b["promoter"] = 5
    else: b["promoter"] = 2; flags.append(f"Low promoter {promoter}%")
    if pledge and pledge > 10:
        b["promoter"] = max(0, b["promoter"] - 4); flags.append(f"Pledged {pledge}%")

    # Balance sheet / D-E (10 pts)
    if de is None: b["debt"] = 6   # neutral if unknown
    elif de <= 0.3: b["debt"] = 10
    elif de <= 0.7: b["debt"] = 8
    elif de <= 1.2: b["debt"] = 5
    elif de <= 2.0: b["debt"] = 2
    else: b["debt"] = 0; flags.append(f"High debt D/E {de}")

    score = sum(b.values())
    grade = ("EXCELLENT" if score >= 80 else "STRONG" if score >= 68 else
             "GOOD" if score >= 55 else "WATCH" if score >= 42 else "WEAK")
    return {"score": score, "grade": grade, "breakdown": b, "flags": flags}


# ── The shared per-stock engine (Dashboard AND Buy/Sell Radar use this) ────
def evaluate_stock(ticker, current_pct, target_pct, vix, user_id=None):
    """
    THE single source of truth for a stock's state.
    Merges live signal (Kite) + long-term quality (fundamentals).
    """
    from kite_data_patch import stock_signal
    from screener_fetch import get_cached

    sig  = stock_signal(ticker, current_pct, target_pct, vix, user_id)
    fund = get_cached(ticker)
    qual = quality_score(fund)

    # Combined verdict: the signal says WHEN, quality says WHETHER-TO-OWN
    q = qual["score"]
    long_term_ok = (q is not None and q >= 55)

    # If quality is weak, override any ADD signal with a REVIEW
    combined = sig.get("signal", "HOLD")
    note = ""
    if q is not None and q < 42 and "ADD" in combined:
        combined = "REVIEW QUALITY"
        note = f"Signal says add, but long-term score {q} is WEAK — review before adding"
    elif q is not None and q < 42:
        note = f"Long-term quality WEAK ({q}/100) — replacement candidate"

    return {
        **sig,
        "quality_score": q,
        "quality_grade": qual["grade"],
        "quality_flags": qual["flags"],
        "long_term_ok": long_term_ok,
        "combined_signal": combined,
        "quality_note": note,
        "fundamentals": {k: fund.get(k) for k in
                         ("roce","roe","de","pe","promoter_pct","sales_growth_3y","profit_growth_3y")}
                         if fund else {},
    }


def register_routes(app):
    from fastapi import Depends, HTTPException
    from auth import get_current_user
    from database import get_db

    def _corpus(uid, db, breakdown=False):
        """
        Effective corpus with LIVE prices (same source as My Holdings, so pages agree).
        Refreshes stored_holdings.last_price from Kite, then sums.
        """
        row = db.execute("SELECT * FROM user_settings WHERE user_id=?", (uid,)).fetchone()
        w = (row["withdrawal_amount"] if row else 0) or 0
        p = (row["pending_credit"] if row else 0) or 0

        rows = db.execute("""SELECT ticker, quantity, last_price FROM stored_holdings
                             WHERE user_id=? AND quantity>0""", (uid,)).fetchall()
        if not rows:
            return {"corpus": 0, "holdings_value": 0, "withdrawal": w,
                    "pending": p, "count": 0, "priced": "none"} if breakdown else 0

        # Refresh with live Kite prices in ONE batch call
        live, priced = {}, "stored"
        try:
            from kite_data_patch import _kite
            k = _kite(uid)
            if k:
                syms = [f"NSE:{r['ticker']}" for r in rows]
                q = k.quote(syms) or {}
                for r in rows:
                    key = f"NSE:{r['ticker']}"
                    if key in q and q[key].get("last_price"):
                        live[r["ticker"]] = q[key]["last_price"]
                if live:
                    priced = "live_kite"
                    for tk, px in live.items():
                        db.execute("UPDATE stored_holdings SET last_price=? WHERE user_id=? AND ticker=?",
                                   (px, uid, tk))
                    db.commit()
        except Exception as e:
            print(f"corpus live-price refresh failed: {e}")

        value = 0.0
        for r in rows:
            px = live.get(r["ticker"], r["last_price"] or 0)
            value += (r["quantity"] or 0) * px

        eff = value - w + p
        if breakdown:
            return {"corpus": round(eff, 0), "holdings_value": round(value, 0),
                    "withdrawal": w, "pending": p, "count": len(rows),
                    "priced": priced, "live_priced_count": len(live)}
        return eff

    def _targets(uid, db):
        try:
            import athena_core; athena_core.seed_core22(uid)
        except Exception: pass
        rows = db.execute("""SELECT * FROM core22_targets WHERE user_id=? AND active=1""",
                          (uid,)).fetchall()
        return [dict(r) for r in rows]

    # ── SHARED ENGINE ENDPOINT (Dashboard + Radar both call this) ─────────
    @app.get("/engine/core22-signals")
    async def core22_signals(current_user=Depends(get_current_user)):
        uid = current_user["id"]; db = get_db()
        try:
            from kite_data_patch import compute_ivp_ivr
            vix = compute_ivp_ivr(uid)["vix"]
        except Exception:
            vix = 14.0
        corpus = _corpus(uid, db)
        targets = _targets(uid, db)
        held = {r["ticker"]: r for r in db.execute(
            "SELECT ticker, quantity, last_price FROM stored_holdings WHERE user_id=?", (uid,)).fetchall()}

        results = []
        for t in targets:
            tk = t["ticker"]
            h = held.get(tk)
            val = (h["quantity"]*h["last_price"]) if h else 0
            curr_pct = (val/corpus*100) if corpus else 0
            results.append(evaluate_stock(tk, curr_pct, t["target_pct"], vix, uid))
        results.sort(key=lambda x: x.get("priority", 5))
        return {"vix": vix, "corpus": round(corpus,0), "signals": results,
                "count": len(results), "generated_at": datetime.now().isoformat()}

    # ── PORTFOLIO HEALTH SCORE (Dashboard) ────────────────────────────────
    @app.get("/engine/health")
    async def portfolio_health(current_user=Depends(get_current_user)):
        uid = current_user["id"]; db = get_db()
        try:
            from kite_data_patch import compute_ivp_ivr
            iv = compute_ivp_ivr(uid); vix = iv["vix"]
        except Exception:
            vix = 14.0; iv = {"vix":vix,"ivp":50}
        corpus = _corpus(uid, db)
        targets = _targets(uid, db)
        held = {r["ticker"]: r for r in db.execute(
            "SELECT ticker, quantity, last_price FROM stored_holdings WHERE user_id=?", (uid,)).fetchall()}

        # Component scores
        from screener_fetch import get_cached
        qualities, weak, expensive = [], [], []
        completion = 0
        bucket_actual = {}
        for t in targets:
            tk = t["ticker"]; h = held.get(tk)
            if h: completion += 1
            val = (h["quantity"]*h["last_price"]) if h else 0
            cp = (val/corpus*100) if corpus else 0
            bucket_actual[t["bucket"]] = bucket_actual.get(t["bucket"],0) + cp
            q = quality_score(get_cached(tk))
            if q["score"] is not None:
                qualities.append(q["score"])
                if q["score"] < 42: weak.append(tk)
                if any("Expensive" in f or "expensive" in f for f in q["flags"]): expensive.append(tk)

        avg_quality = round(sum(qualities)/len(qualities),1) if qualities else None
        completion_pct = round(completion/len(targets)*100,1) if targets else 0

        # Health components (0-100 each)
        c_quality = avg_quality or 50
        c_completion = completion_pct
        c_vix = 100 if 12 <= vix <= 16 else 75 if vix <= 20 else 50 if vix <= 25 else 30
        c_weak = max(0, 100 - len(weak)*20)   # each weak stock hurts
        c_valn = max(0, 100 - len(expensive)*12)

        health = round((c_quality*0.30 + c_completion*0.20 + c_vix*0.15 +
                        c_weak*0.20 + c_valn*0.15), 1)
        health_grade = ("EXCELLENT" if health>=80 else "HEALTHY" if health>=65 else
                        "FAIR" if health>=50 else "NEEDS ATTENTION")

        cb = _corpus(uid, db, breakdown=True)
        return {
            "health_score": health, "health_grade": health_grade,
            "corpus_breakdown": cb,
            "components": {
                "avg_quality": c_quality, "completion": c_completion,
                "vix_regime": c_vix, "quality_risk": c_weak, "valuation": c_valn,
            },
            "avg_quality_score": avg_quality,
            "completion_pct": completion_pct,
            "weak_stocks": weak,
            "expensive_stocks": expensive,
            "vix": vix, "corpus": round(corpus,0),
            "generated_at": datetime.now().isoformat(),
        }

    # ── TODAY'S DECISIONS (the thinking engine output) ────────────────────
    @app.get("/engine/decisions")
    async def todays_decisions(current_user=Depends(get_current_user)):
        """The Dashboard's core: prioritised decisions, not raw data."""
        uid = current_user["id"]; db = get_db()
        sig_data = await core22_signals(current_user)
        signals = sig_data["signals"]
        decisions = []

        for s in signals:
            tk = s["ticker"]; sg = s.get("combined_signal", s.get("signal"))
            q = s.get("quality_score")
            if sg in ("STRONG ADD","STRONG TRIM","REVIEW QUALITY"):
                pri = 1
            elif sg in ("ADD","TRIM"):
                pri = 2
            elif sg == "AVOID ADDING":
                pri = 3
            else:
                continue  # HOLD = no decision needed
            decisions.append({
                "ticker": tk, "signal": sg, "action": s.get("action",""),
                "quality_score": q, "quality_grade": s.get("quality_grade"),
                "rsi": s.get("rsi"), "priority": pri,
                "note": s.get("quality_note",""),
                "confidence": ("HIGH" if q and (q>=68 or q<42) else "MEDIUM"),
            })
        decisions.sort(key=lambda x: x["priority"])

        # Long-term watch: weak-quality holdings = replacement candidates
        replace_watch = [{"ticker": s["ticker"], "quality_score": s["quality_score"],
                          "flags": s["quality_flags"]}
                         for s in signals
                         if s.get("quality_score") is not None and s["quality_score"] < 42]

        return {
            "decisions": decisions[:8],
            "decision_count": len(decisions),
            "replacement_watch": replace_watch,
            "vix": sig_data["vix"],
            "generated_at": datetime.now().isoformat(),
        }

    # ── SCREENER SYNC TRIGGER ──────────────────────────────────────────────
    @app.post("/engine/sync-fundamentals")
    async def sync_fundamentals(current_user=Depends(get_current_user)):
        uid = current_user["id"]; db = get_db()
        import screener_fetch
        screener_fetch.init_schema()
        targets = _targets(uid, db)
        tickers = [t["ticker"] for t in targets if not t.get("is_etf")]
        result = screener_fetch.sync_all(tickers, delay=2.0)
        return result

    @app.get("/engine/fundamentals")
    async def get_fundamentals(current_user=Depends(get_current_user)):
        import screener_fetch
        return {"fundamentals": screener_fetch.get_all_cached()}
