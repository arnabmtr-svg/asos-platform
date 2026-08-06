"""
core22_framework.py — Dynamic Core 22 Allocation Framework + Swing Sleeve
The "mother" allocation engine. Core 22 = 22 dynamic target allocations.
Swing = separate temporary tactical sleeve with a graduation path to Core 22.

Fund allocation is the parent: target weights computed dynamically from quality,
with simple sector cap + per-stock ceiling. SIP/BuySell/Substitution all serve
keeping actual weight -> target weight.

Routes:
  GET  /core22/framework            -> dynamic target weights (the allocation plan)
  GET  /core22/allocation-status    -> actual vs target per slot, drift, actions
  POST /core22/recompute-weights    -> recompute dynamic weights from quality
  GET  /swing/positions             -> active swing trades
  POST /swing/open                  -> open a swing trade (tactical sleeve)
  POST /swing/close                 -> close a swing (with graduation check)
  GET  /swing/graduation-candidates -> swings that qualify for Core 22

main.py:
  try: import core22_framework
  except ImportError: core22_framework = None
  # lifespan: if core22_framework: core22_framework.init_schema()
  # after app: if core22_framework: core22_framework.register_routes(app)
"""
import json
from datetime import datetime, date

# ── Simple guardrails (kept simple per user's instruction) ────────────────
STOCK_CEILING_PCT = 12.0      # no single name over 12%
STOCK_FLOOR_PCT = 1.0         # meaningful position at least 1%
SECTOR_CAP_PCT = 30.0         # no sector over 30%
DRIFT_THRESHOLD = 2.0         # act when actual drifts >2% from target


def init_schema():
    from database import get_db
    db = get_db()
    db.executescript("""
    CREATE TABLE IF NOT EXISTS swing_positions (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER, ticker TEXT, entry_price REAL, quantity INTEGER,
        target_price REAL, stop_price REAL, thesis TEXT,
        opened_at TEXT DEFAULT (datetime('now')), status TEXT DEFAULT 'OPEN',
        closed_at TEXT, exit_price REAL, pnl REAL,
        quality_score REAL, long_term_potential INTEGER DEFAULT 0
    );
    CREATE TABLE IF NOT EXISTS core22_weight_history (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER, ticker TEXT, target_pct REAL,
        quality_score REAL, computed_at TEXT DEFAULT (datetime('now'))
    );
    """)
    db.commit()


# ── DYNAMIC WEIGHTING — the mother allocation ─────────────────────────────
def _sector_of(ticker, role=""):
    """Light sector inference from role text (kept simple)."""
    r = (role or "").lower()
    if any(w in r for w in ["index","nifty","global"]): return "Index/ETF"
    if any(w in r for w in ["gold","silver","reserve"]): return "Precious"
    if any(w in r for w in ["power","renewable","infra"]): return "Power"
    if any(w in r for w in ["defence","defense"]): return "Defence"
    if any(w in r for w in ["copper","aluminium","metal","mining"]): return "Metals"
    if any(w in r for w in ["bank","nbfc","wealth","finance"]): return "Financials"
    if any(w in r for w in ["pharma","api"]): return "Pharma"
    if any(w in r for w in ["chem"]): return "Chemicals"
    if any(w in r for w in ["ev","auto","drivetrain","battery"]): return "Auto/EV"
    return "Other"


def compute_dynamic_weights(uid, db):
    """
    THE MOTHER: compute target weight for each Core 22 slot dynamically from
    quality score, then apply floor/ceiling + sector cap. Returns the plan.
    """
    from athena_dashboard import quality_score
    import screener_fetch
    rows = db.execute("""SELECT ticker, role, is_etf, bucket, target_pct FROM core22_targets
                         WHERE user_id=? AND active=1""", (uid,)).fetchall()
    if not rows:
        return {"error": "no core22 members", "members": []}

    members = []
    for r in rows:
        tk = r["ticker"]
        if r["is_etf"]:
            # ETFs get their configured weight (quality scoring doesn't apply)
            q = None; base = r["target_pct"] or 4.0
        else:
            fund = screener_fetch.get_cached(tk)
            qres = quality_score(fund)
            q = qres["score"]
            # dynamic base: quality 0-100 -> weight seed. Higher Q = more weight.
            base = max(1.0, (q or 40) / 12.0)   # e.g. Q90 -> 7.5, Q60 -> 5, Q36 -> 3
        members.append({"ticker": tk, "role": r["role"], "is_etf": bool(r["is_etf"]),
                        "quality": q, "raw_weight": base,
                        "sector": _sector_of(tk, r["role"])})

    # normalize to 100%
    total_raw = sum(m["raw_weight"] for m in members)
    for m in members:
        m["target_pct"] = round(m["raw_weight"] / total_raw * 100, 2)

    # apply per-stock ceiling/floor
    for m in members:
        m["target_pct"] = max(STOCK_FLOOR_PCT, min(STOCK_CEILING_PCT, m["target_pct"]))

    # apply sector cap: if a sector exceeds cap, scale its members down
    sector_tot = {}
    for m in members:
        sector_tot[m["sector"]] = sector_tot.get(m["sector"], 0) + m["target_pct"]
    for sec, tot in sector_tot.items():
        if tot > SECTOR_CAP_PCT:
            scale = SECTOR_CAP_PCT / tot
            for m in members:
                if m["sector"] == sec:
                    m["target_pct"] = round(m["target_pct"] * scale, 2)

    # renormalize to 100 after caps
    total_final = sum(m["target_pct"] for m in members)
    for m in members:
        m["target_pct"] = round(m["target_pct"] / total_final * 100, 2)

    members.sort(key=lambda x: x["target_pct"], reverse=True)
    return {"members": members, "count": len(members),
            "sector_breakdown": {s: round(sum(m["target_pct"] for m in members if m["sector"]==s),1)
                                 for s in set(m["sector"] for m in members)},
            "rules": {"stock_ceiling": STOCK_CEILING_PCT, "stock_floor": STOCK_FLOOR_PCT,
                      "sector_cap": SECTOR_CAP_PCT}}


def allocation_status(uid, db):
    """Actual weight vs dynamic target per slot -> drift + action."""
    plan = compute_dynamic_weights(uid, db)
    if plan.get("error"):
        return plan
    # live holdings value
    try:
        from athena_dashboard import _corpus
        cb = _corpus(uid, db, breakdown=True)
        corpus = cb["holdings_value"] or 0
    except Exception:
        corpus = 0
    held = {}
    for h in db.execute("SELECT ticker, quantity, last_price FROM stored_holdings WHERE user_id=?",
                        (uid,)).fetchall():
        held[h["ticker"]] = (h["quantity"] or 0) * (h["last_price"] or 0)

    out = []
    for m in plan["members"]:
        actual_val = held.get(m["ticker"], 0)
        actual_pct = round(actual_val / corpus * 100, 2) if corpus else 0
        drift = round(actual_pct - m["target_pct"], 2)
        if abs(drift) < DRIFT_THRESHOLD:
            action = "HOLD"
        elif drift < 0:
            action = "ADD"      # underweight
        else:
            action = "TRIM"     # overweight
        out.append({**m, "actual_pct": actual_pct, "actual_value": round(actual_val,0),
                    "drift": drift, "action": action})
    return {"corpus": corpus, "slots": out,
            "underweight": [s["ticker"] for s in out if s["action"]=="ADD"],
            "overweight": [s["ticker"] for s in out if s["action"]=="TRIM"],
            "sector_breakdown": plan["sector_breakdown"]}




# ══════════════════════════════════════════════════════════════════════════
# SIP ALLOCATION BRAIN — valuation-gated, weight-gap driven, flexible amount
# "Allocation is the mother": fund underweight + fairly-valued quality names,
# skip the expensive/peaked ones until they're fair.
# ══════════════════════════════════════════════════════════════════════════
def sip_allocate(uid, db, amount):
    """
    Distribute `amount` (whatever the user gives) across Core 22 by:
    weight-gap (underweight only) x quality x valuation gate.
    Skips expensive / technically-extended names.
    """
    status = allocation_status(uid, db)
    if status.get("error"):
        return status
    from kite_data_patch import _kite, _hist_closes, _rsi
    import screener_fetch
    k = _kite(uid)

    funded, skipped = [], []
    eligible = []
    for slot in status["slots"]:
        tk = slot["ticker"]
        # only underweight names get fresh money
        if slot["action"] != "ADD":
            skipped.append({"ticker": tk, "reason": f"{slot['action'].lower()} (drift {slot['drift']}%)"})
            continue

        # valuation + technical gate
        reason_skip = None
        rsi = None; pct_from_high = None
        if not slot.get("is_etf"):
            fund = screener_fetch.get_cached(tk)
            pe = fund.get("pe")
            # expensive vs a rough sanity band
            if pe and pe > 80:
                reason_skip = f"expensive (PE {pe})"
        if k and not reason_skip:
            try:
                closes = _hist_closes(k, tk, 250)
                if closes and len(closes) > 50:
                    rsi = _rsi(closes)
                    hi = max(closes[-250:]); price = closes[-1]
                    pct_from_high = round((price - hi)/hi*100, 1)
                    # technically extended: near 52wk high or RSI hot
                    if rsi and rsi > 70:
                        reason_skip = f"overbought (RSI {rsi})"
                    elif pct_from_high is not None and pct_from_high > -3:
                        reason_skip = f"at peak ({pct_from_high}% from 52wk high)"
            except Exception:
                pass

        if reason_skip:
            skipped.append({"ticker": tk, "reason": reason_skip,
                            "note": "will fund when fairly valued"})
            continue

        # eligible — score by gap size x quality
        gap = abs(slot["drift"])
        q = slot.get("quality") or 50
        weight = gap * (q/50)   # bigger gap + higher quality = more money
        eligible.append({"ticker": tk, "gap": gap, "quality": q, "rsi": rsi,
                         "pct_from_high": pct_from_high, "target_pct": slot["target_pct"],
                         "actual_pct": slot["actual_pct"], "_w": weight})

    if not eligible:
        return {"amount": amount, "funded": [], "skipped": skipped,
                "message": "No underweight fairly-valued names this month. Hold cash or wait for pullbacks.",
                "unallocated": amount}

    total_w = sum(e["_w"] for e in eligible)
    for e in eligible:
        e["allocation"] = round(amount * e["_w"]/total_w, 0)
        del e["_w"]
        funded.append(e)
    funded.sort(key=lambda x: x["allocation"], reverse=True)
    return {"amount": amount, "funded": funded, "skipped": skipped,
            "funded_total": sum(f["allocation"] for f in funded),
            "message": f"₹{amount:,.0f} directed to {len(funded)} underweight, fairly-valued names. "
                       f"{len(skipped)} skipped (overweight/expensive/peaked)."}


def register_routes(app):
    from fastapi import Depends, HTTPException
    from auth import get_current_user
    from database import get_db

    @app.get("/core22/framework")
    async def framework(current_user=Depends(get_current_user)):
        db = get_db(); init_schema()
        return compute_dynamic_weights(current_user["id"], db)

    @app.get("/core22/allocation-status")
    async def alloc_status(current_user=Depends(get_current_user)):
        db = get_db(); init_schema()
        return allocation_status(current_user["id"], db)

    @app.post("/core22/recompute-weights")
    async def recompute(current_user=Depends(get_current_user)):
        db = get_db(); init_schema(); uid = current_user["id"]
        plan = compute_dynamic_weights(uid, db)
        if plan.get("error"): raise HTTPException(400, plan["error"])
        # persist the new target_pct into core22_targets
        for m in plan["members"]:
            db.execute("UPDATE core22_targets SET target_pct=? WHERE user_id=? AND ticker=?",
                       (m["target_pct"], uid, m["ticker"]))
            db.execute("""INSERT INTO core22_weight_history (user_id,ticker,target_pct,quality_score)
                          VALUES (?,?,?,?)""", (uid, m["ticker"], m["target_pct"], m["quality"]))
        db.commit()
        return {"message": "dynamic weights recomputed & saved", "members": plan["members"]}

    @app.post("/core22/sip-allocate")
    async def sip_allocate_route(data: dict, current_user=Depends(get_current_user)):
        """
        Valuation-gated SIP allocation. Body: {amount: 100000} (or any amount).
        Returns funded (underweight+fair) and skipped (overweight/expensive/peaked).
        """
        db = get_db(); init_schema()
        amount = float(data.get("amount", 100000))
        if amount <= 0: raise HTTPException(400, "amount must be positive")
        return sip_allocate(current_user["id"], db, amount)

    # ── SWING SLEEVE (separate tactical engine) ──
    @app.get("/swing/positions")
    async def swing_positions(current_user=Depends(get_current_user)):
        db = get_db(); init_schema()
        rows = db.execute("""SELECT * FROM swing_positions WHERE user_id=? AND status='OPEN'
                             ORDER BY opened_at DESC""", (current_user["id"],)).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            # live price + P&L
            try:
                from kite_data_patch import _kite
                k = _kite(current_user["id"])
                q = k.quote([f"NSE:{d['ticker']}"]) if k else {}
                ltp = q.get(f"NSE:{d['ticker']}", {}).get("last_price", d["entry_price"])
                d["ltp"] = ltp
                d["pnl_live"] = round((ltp - d["entry_price"]) * d["quantity"], 0)
                d["pnl_pct"] = round((ltp - d["entry_price"]) / d["entry_price"] * 100, 1)
                d["to_target"] = round((d["target_price"] - ltp) / ltp * 100, 1) if d["target_price"] else None
                d["to_stop"] = round((ltp - d["stop_price"]) / ltp * 100, 1) if d["stop_price"] else None
            except Exception:
                d["ltp"] = d["entry_price"]; d["pnl_live"] = 0; d["pnl_pct"] = 0
            out.append(d)
        return {"positions": out, "count": len(out)}

    @app.post("/swing/open")
    async def swing_open(data: dict, current_user=Depends(get_current_user)):
        db = get_db(); init_schema(); uid = current_user["id"]
        tk = (data.get("ticker") or "").upper()
        if not tk: raise HTTPException(400, "ticker required")
        db.execute("""INSERT INTO swing_positions (user_id,ticker,entry_price,quantity,
                      target_price,stop_price,thesis,quality_score)
                      VALUES (?,?,?,?,?,?,?,?)""",
                   (uid, tk, float(data.get("entry_price",0)), int(data.get("quantity",0)),
                    float(data.get("target_price",0)), float(data.get("stop_price",0)),
                    data.get("thesis",""), float(data.get("quality_score",0) or 0)))
        db.commit()
        return {"message": f"swing trade opened: {tk}", "ticker": tk}

    @app.post("/swing/close")
    async def swing_close(data: dict, current_user=Depends(get_current_user)):
        db = get_db(); uid = current_user["id"]
        sid = data.get("id"); exit_price = float(data.get("exit_price", 0))
        r = db.execute("SELECT * FROM swing_positions WHERE id=? AND user_id=?", (sid, uid)).fetchone()
        if not r: raise HTTPException(404, "swing not found")
        pnl = round((exit_price - r["entry_price"]) * r["quantity"], 0)
        db.execute("""UPDATE swing_positions SET status='CLOSED', closed_at=datetime('now'),
                      exit_price=?, pnl=? WHERE id=?""", (exit_price, pnl, sid))
        db.commit()
        # graduation check: profitable AND flagged long-term potential
        graduate = pnl > 0 and r["long_term_potential"] == 1
        return {"message": f"swing closed, P&L ₹{pnl}", "pnl": pnl,
                "graduation_eligible": graduate,
                "note": ("This swing was profitable and flagged long-term — consider "
                         "substituting it into Core 22 via Watchlist Scout." if graduate else "")}

    @app.get("/swing/graduation-candidates")
    async def graduation(current_user=Depends(get_current_user)):
        db = get_db(); uid = current_user["id"]
        # swings marked long-term potential that are performing
        rows = db.execute("""SELECT * FROM swing_positions WHERE user_id=?
                             AND long_term_potential=1 AND status='OPEN'""", (uid,)).fetchall()
        return {"candidates": [dict(r) for r in rows],
                "note": "Profitable swings with long-term potential — eligible to enter Core 22 via substitution."}
