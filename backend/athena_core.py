"""
athena_core.py — ATHENA Sprint S-1/S-2 backend
1. Market Clock  — IST-aware, NSE holidays, single source of truth (fixes B-2)
2. Cash Ledger   — proceeds lifecycle: pending→credited→withdrawn/deployed (fixes B-1)
3. Core 22 DB    — targets in DB with full audit trail (PD-2)
4. Income Engine — options + equity earnings vs monthly target (Arnab's prime goal)
5. Daily Actions — generated tasks, not hardcoded (fixes B-5)

Add to main.py:
  import athena_core
  # in lifespan:  athena_core.init_schema()
  # after app:    athena_core.register_routes(app)
"""

from datetime import datetime, timedelta, date
import pytz

IST = pytz.timezone("Asia/Kolkata")

# ── NSE Holidays 2026 (trading holidays) ──────────────────────────────────
NSE_HOLIDAYS_2026 = {
    "2026-01-26","2026-02-17","2026-03-06","2026-03-25","2026-04-03",
    "2026-04-10","2026-04-14","2026-05-01","2026-05-27","2026-06-17",
    "2026-08-15","2026-08-28","2026-09-14","2026-10-02","2026-10-20",
    "2026-10-21","2026-11-09","2026-12-25",
}

# ════════════════════════════════════════════════════════════════════════
# 1. MARKET CLOCK — single source of truth
# ════════════════════════════════════════════════════════════════════════
def market_clock() -> dict:
    now = datetime.now(IST)
    today_str = now.strftime("%Y-%m-%d")
    is_weekend = now.weekday() >= 5
    is_holiday = today_str in NSE_HOLIDAYS_2026
    mins = now.hour * 60 + now.minute
    OPEN_M, CLOSE_M, PRE_M = 9*60+15, 15*60+30, 9*60

    if is_weekend or is_holiday:
        status, label = "closed", ("Weekend" if is_weekend else "NSE Holiday")
    elif PRE_M <= mins < OPEN_M:
        status, label = "pre-open", "Pre-open"
    elif OPEN_M <= mins <= CLOSE_M:
        status, label = "open", "Market open"
    else:
        status, label = "closed", "Market closed"

    # Next session
    d = now
    if status == "open":
        next_session = now.replace(hour=15, minute=30, second=0); next_label = "Closes"
    else:
        d = now if (mins < OPEN_M and not is_weekend and not is_holiday) else now + timedelta(days=1)
        for _ in range(10):
            ds = d.strftime("%Y-%m-%d")
            if d.weekday() < 5 and ds not in NSE_HOLIDAYS_2026:
                break
            d += timedelta(days=1)
        next_session = d.replace(hour=9, minute=15, second=0); next_label = "Opens"

    return {
        "ist": now.strftime("%H:%M:%S"),
        "date": now.strftime("%a, %d %b %Y"),
        "iso": now.isoformat(),
        "status": status,             # open | closed | pre-open
        "label": label,               # "Market open" / "Weekend" / "NSE Holiday" / ...
        "is_weekend": is_weekend,
        "is_holiday": is_holiday,
        "next_session_label": next_label,
        "next_session": next_session.strftime("%a %d %b, %H:%M"),
        "sip_week": 1 <= now.day <= 7,
    }


# ════════════════════════════════════════════════════════════════════════
# SCHEMA
# ════════════════════════════════════════════════════════════════════════
def init_schema():
    from database import get_db
    db = get_db()
    db.executescript("""
    CREATE TABLE IF NOT EXISTS cash_ledger (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER, entry_date TEXT, description TEXT,
        amount REAL NOT NULL,
        entry_type TEXT CHECK(entry_type IN ('sale_proceeds','dividend','deposit','withdrawal','deployment','option_income','interest')),
        status TEXT DEFAULT 'pending' CHECK(status IN ('pending','credited','withdrawn','deployed')),
        linked_ticker TEXT DEFAULT '',
        status_updated_at TEXT,
        created_at TEXT DEFAULT (datetime('now'))
    );
    CREATE TABLE IF NOT EXISTS core22_targets (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER, ticker TEXT NOT NULL,
        bucket INTEGER, target_pct REAL, monthly_sip REAL,
        role TEXT DEFAULT '', is_etf INTEGER DEFAULT 0,
        active INTEGER DEFAULT 1,
        added_at TEXT DEFAULT (datetime('now'))
    );
    CREATE TABLE IF NOT EXISTS core22_audit (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER, ticker TEXT, action TEXT,
        old_value TEXT, new_value TEXT, reason TEXT NOT NULL,
        valuation_snapshot TEXT, created_at TEXT DEFAULT (datetime('now'))
    );
    CREATE TABLE IF NOT EXISTS daily_actions (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER, action_date TEXT, title TEXT, detail TEXT,
        priority INTEGER DEFAULT 3, source TEXT DEFAULT 'engine',
        status TEXT DEFAULT 'open' CHECK(status IN ('open','done','dismissed')),
        created_at TEXT DEFAULT (datetime('now'))
    );
    CREATE TABLE IF NOT EXISTS income_targets (
        user_id INTEGER PRIMARY KEY,
        monthly_option_target REAL DEFAULT 25000,
        monthly_equity_target REAL DEFAULT 15000,
        updated_at TEXT DEFAULT (datetime('now'))
    );
    """)
    db.commit()


DEFAULT_C22 = [
    ("NIFTYBEES",1,12,12000,"India index core",1),("MON100",1,10,10000,"Global tech hedge",1),
    ("JUNIORBEES",1,8,8000,"Nifty Next 50",1),("CGPOWER",2,9,8000,"Power infra",0),
    ("TATAPOWER",2,7,4000,"Renewables",0),("BDL",2,5,5000,"Defence PSU",0),
    ("HBLENGINE",2,4,7000,"Battery EV",0),("HINDCOPPER",3,5,8000,"Copper",0),
    ("HINDALCO",3,5,7000,"Aluminium",0),("ANGELONE",3,4,5000,"Wealth mgmt",0),
    ("FINCABLES",3,4,6000,"Cables",0),("GRANULES",3,4,6000,"API pharma",0),
    ("SONACOMS",3,3,3000,"EV drivetrain",0),("PRICOLLTD",3,2,2000,"Precision auto",0),
    ("INDUSINDBK",3,2,7000,"Private bank",0),("RELIANCE",3,2,3000,"Conglomerate",0),
    ("PIRAMALFIN",4,3.5,6000,"NBFC rebuild",0),("HSCL",4,3,2000,"Spec chem",0),
    ("SHILCHAR",4,2,2000,"Transformers",0),("GMDCLTD",4,1.5,4000,"Mining",0),
    ("GOLDBEES",5,3,1000,"Gold reserve",1),("SILVERETF",5,2,1000,"Silver reserve",1),
]

def seed_core22(user_id: int):
    """Seed default Core 22 for a user if empty."""
    from database import get_db
    db = get_db()
    n = db.execute("SELECT COUNT(*) c FROM core22_targets WHERE user_id=? AND active=1",
                   (user_id,)).fetchone()["c"]
    if n == 0:
        for t, b, pct, sip, role, etf in DEFAULT_C22:
            db.execute("""INSERT INTO core22_targets
                (user_id,ticker,bucket,target_pct,monthly_sip,role,is_etf)
                VALUES (?,?,?,?,?,?,?)""", (user_id, t, b, pct, sip, role, etf))
        db.execute("""INSERT INTO core22_audit (user_id,ticker,action,old_value,new_value,reason)
                      VALUES (?,?,?,?,?,?)""",
                   (user_id, "ALL", "SEED", "", "22 stocks", "Initial seed from ASOS framework"))
        db.commit()


# ════════════════════════════════════════════════════════════════════════
# ROUTES
# ════════════════════════════════════════════════════════════════════════
def register_routes(app):
    from fastapi import Depends, HTTPException
    from auth import get_current_user
    from database import get_db

    # ── MARKET CLOCK ──────────────────────────────────────────────────
    @app.get("/clock")
    async def clock():
        return market_clock()

    # ── CASH LEDGER ───────────────────────────────────────────────────
    @app.get("/cash/summary")
    async def cash_summary(current_user=Depends(get_current_user)):
        db = get_db(); uid = current_user["id"]
        rows = db.execute("SELECT * FROM cash_ledger WHERE user_id=? ORDER BY entry_date DESC, id DESC",
                          (uid,)).fetchall()
        entries = [dict(r) for r in rows]
        pending  = sum(e["amount"] for e in entries if e["status"]=="pending"  and e["entry_type"]!="withdrawal")
        credited = sum(e["amount"] for e in entries if e["status"]=="credited" and e["entry_type"]!="withdrawal")
        return {
            "entries": entries[:100],
            "pending_total": round(pending,0),
            "idle_cash": round(credited,0),
            "pill_text": f"₹{pending/1e5:.2f}L pending T+1" if pending > 0 else "",
            "show_pill": pending > 0,
        }

    @app.post("/cash/add")
    async def cash_add(data: dict, current_user=Depends(get_current_user)):
        db = get_db()
        db.execute("""INSERT INTO cash_ledger
            (user_id,entry_date,description,amount,entry_type,status,linked_ticker)
            VALUES (?,?,?,?,?,?,?)""",
            (current_user["id"], data.get("date", date.today().isoformat()),
             data.get("description",""), float(data["amount"]),
             data.get("type","sale_proceeds"), data.get("status","pending"),
             data.get("ticker","")))
        db.commit()
        return {"message": "Entry added"}

    @app.post("/cash/{entry_id}/status")
    async def cash_status(entry_id: int, data: dict, current_user=Depends(get_current_user)):
        """Q-1 answer: user chooses — credited / withdrawn / deployed. System obeys."""
        new_status = data.get("status")
        if new_status not in ("pending","credited","withdrawn","deployed"):
            raise HTTPException(400, "invalid status")
        db = get_db()
        db.execute("""UPDATE cash_ledger SET status=?, status_updated_at=datetime('now')
                      WHERE id=? AND user_id=?""",
                   (new_status, entry_id, current_user["id"]))
        db.commit()
        return {"message": f"Marked {new_status}"}

    # ── CORE 22 MANAGER (PD-2: DB-driven, full audit Q-3) ─────────────
    @app.get("/core22/targets")
    async def get_targets(current_user=Depends(get_current_user)):
        seed_core22(current_user["id"])
        db = get_db()
        rows = db.execute("""SELECT * FROM core22_targets WHERE user_id=? AND active=1
                             ORDER BY bucket, target_pct DESC""",
                          (current_user["id"],)).fetchall()
        return {"targets": [dict(r) for r in rows],
                "total_pct": round(sum(r["target_pct"] for r in rows),1)}

    @app.post("/core22/change")
    async def change_target(data: dict, current_user=Depends(get_current_user)):
        """
        Full-audit change (Q-3). Body: {action: add|remove|reweight, ticker,
        target_pct?, bucket?, monthly_sip?, reason (REQUIRED)}
        """
        reason = data.get("reason","").strip()
        if len(reason) < 10:
            raise HTTPException(400, "Audit reason required (min 10 chars) — PD-2 full audit")
        uid = current_user["id"]; tk = data.get("ticker","").upper(); action = data.get("action")
        db = get_db()
        old = db.execute("SELECT * FROM core22_targets WHERE user_id=? AND ticker=? AND active=1",
                         (uid,tk)).fetchone()

        # Valuation snapshot (PD-3: valuation is the mother of investment)
        val_snap = ""
        try:
            import yfinance as yf
            t = yf.Ticker(tk+".NS"); inf = t.info
            val_snap = f"PE:{inf.get('trailingPE','?')} | 52wH:{inf.get('fiftyTwoWeekHigh','?')} | Px:{inf.get('currentPrice','?')}"
        except Exception:
            val_snap = "unavailable"

        if action == "add":
            if old: raise HTTPException(400, f"{tk} already in Core 22")
            db.execute("""INSERT INTO core22_targets (user_id,ticker,bucket,target_pct,monthly_sip,role,is_etf)
                          VALUES (?,?,?,?,?,?,?)""",
                       (uid, tk, int(data.get("bucket",3)), float(data.get("target_pct",2)),
                        float(data.get("monthly_sip",2000)), data.get("role",""), int(data.get("is_etf",0))))
            old_v, new_v = "", f"{data.get('target_pct')}% B{data.get('bucket')}"
        elif action == "remove":
            if not old: raise HTTPException(404, f"{tk} not in Core 22")
            db.execute("UPDATE core22_targets SET active=0 WHERE id=?", (old["id"],))
            old_v, new_v = f"{old['target_pct']}%", "REMOVED"
        elif action == "reweight":
            if not old: raise HTTPException(404, f"{tk} not in Core 22")
            new_pct = float(data.get("target_pct", old["target_pct"]))
            new_sip = float(data.get("monthly_sip", old["monthly_sip"]))
            db.execute("UPDATE core22_targets SET target_pct=?, monthly_sip=? WHERE id=?",
                       (new_pct, new_sip, old["id"]))
            old_v, new_v = f"{old['target_pct']}% ₹{old['monthly_sip']}", f"{new_pct}% ₹{new_sip}"
        else:
            raise HTTPException(400, "action must be add|remove|reweight")

        db.execute("""INSERT INTO core22_audit (user_id,ticker,action,old_value,new_value,reason,valuation_snapshot)
                      VALUES (?,?,?,?,?,?,?)""",
                   (uid, tk, action.upper(), old_v, new_v, reason, val_snap))
        db.commit()
        return {"message": f"{action} {tk} recorded with audit", "valuation_snapshot": val_snap}

    @app.get("/core22/audit")
    async def get_audit(current_user=Depends(get_current_user)):
        db = get_db()
        rows = db.execute("""SELECT * FROM core22_audit WHERE user_id=?
                             ORDER BY created_at DESC LIMIT 100""",
                          (current_user["id"],)).fetchall()
        return {"audit": [dict(r) for r in rows]}

    # ── INCOME ENGINE (Arnab's prime goal: earning from Options + Equity) ──
    @app.get("/income/summary")
    async def income_summary(current_user=Depends(get_current_user)):
        db = get_db(); uid = current_user["id"]
        row = db.execute("SELECT * FROM income_targets WHERE user_id=?",(uid,)).fetchone()
        if not row:
            db.execute("INSERT INTO income_targets (user_id) VALUES (?)",(uid,)); db.commit()
            opt_t, eq_t = 25000, 15000
        else:
            opt_t, eq_t = row["monthly_option_target"], row["monthly_equity_target"]

        month_start = date.today().replace(day=1).isoformat()
        # Option income: cash_ledger entries of type option_income this month
        opt = db.execute("""SELECT COALESCE(SUM(amount),0) s FROM cash_ledger
                            WHERE user_id=? AND entry_type='option_income' AND entry_date>=?""",
                         (uid, month_start)).fetchone()["s"]
        # Equity realized: SELL transactions this month minus their cost (approx: use ledger sale_proceeds vs deployments)
        eq = db.execute("""SELECT COALESCE(SUM(CASE WHEN txn_type='SELL' THEN quantity*price ELSE 0 END),0) s
                           FROM transactions WHERE user_id=? AND txn_date>=?""",
                        (uid, month_start)).fetchone()["s"] if _table_exists(db,"transactions") else 0

        return {
            "month": date.today().strftime("%B %Y"),
            "option_income": round(opt,0), "option_target": opt_t,
            "option_pct": round(opt/opt_t*100,1) if opt_t else 0,
            "equity_realized": round(eq,0), "equity_target": eq_t,
            "combined": round(opt+eq,0), "combined_target": opt_t+eq_t,
            "on_track": (opt+eq) >= (opt_t+eq_t) * (date.today().day/30),
        }

    @app.post("/income/targets")
    async def set_income_targets(data: dict, current_user=Depends(get_current_user)):
        db = get_db()
        db.execute("""INSERT INTO income_targets (user_id,monthly_option_target,monthly_equity_target)
                      VALUES (?,?,?) ON CONFLICT(user_id) DO UPDATE SET
                      monthly_option_target=excluded.monthly_option_target,
                      monthly_equity_target=excluded.monthly_equity_target,
                      updated_at=datetime('now')""",
                   (current_user["id"], float(data.get("option_target",25000)),
                    float(data.get("equity_target",15000))))
        db.commit()
        return {"message":"Income targets saved"}

    # ── DAILY ACTIONS (fixes B-5: no hardcoded checklists) ────────────
    @app.get("/actions/today")
    async def actions_today(current_user=Depends(get_current_user)):
        db = get_db(); uid = current_user["id"]; today = date.today().isoformat()
        rows = db.execute("""SELECT * FROM daily_actions WHERE user_id=? AND action_date=?
                             AND status='open' ORDER BY priority""",(uid,today)).fetchall()
        actions = [dict(r) for r in rows]
        if not actions:
            # Generate from live signals
            clk = market_clock()
            gen = []
            if clk["sip_week"]:
                gen.append(("Run SIP Optimizer — SIP week is active",
                            "Deploy this month's ₹1L per live RSI + weight gaps", 1))
            try:
                from market_data import compute_ivp_ivr
                vix = compute_ivp_ivr()["vix"]
                if vix > 20:
                    gen.append((f"VIX {vix:.1f} elevated — review SIP mode",
                                "Consider 50% SIP or pause per ASOS timing rules", 1))
                elif vix < 20 and clk["status"]=="open":
                    gen.append(("Check Options Desk for income setup",
                                f"VIX {vix:.1f} — Iron Condor conditions may be favourable", 2))
            except Exception:
                pass
            gen.append(("Review Buy/Sell Radar signals",
                        "Check for new TRIM / STRONG ADD signals on Core 22", 3))
            for title, detail, pri in gen:
                db.execute("""INSERT INTO daily_actions (user_id,action_date,title,detail,priority)
                              VALUES (?,?,?,?,?)""",(uid,today,title,detail,pri))
            db.commit()
            rows = db.execute("""SELECT * FROM daily_actions WHERE user_id=? AND action_date=?
                                 AND status='open' ORDER BY priority""",(uid,today)).fetchall()
            actions = [dict(r) for r in rows]
        return {"actions": actions, "date": today}

    @app.post("/actions/{action_id}/{new_status}")
    async def action_update(action_id: int, new_status: str, current_user=Depends(get_current_user)):
        if new_status not in ("done","dismissed","open"):
            raise HTTPException(400,"bad status")
        db = get_db()
        db.execute("UPDATE daily_actions SET status=? WHERE id=? AND user_id=?",
                   (new_status, action_id, current_user["id"]))
        db.commit()
        return {"message": new_status}


def _table_exists(db, name: str) -> bool:
    r = db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name=?",(name,)).fetchone()
    return r is not None