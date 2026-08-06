"""
ASOS Wealth Platform — Backend v2
All fixes: holdings fallback · CSV upload · withdrawal · core22-gap
           options chain (no-auth) · watchlist seed · live trading data
"""

from fastapi import FastAPI, Depends, HTTPException, status, Request, UploadFile, File
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import HTMLResponse, RedirectResponse
from contextlib import asynccontextmanager
import sqlite3, os, json, csv, io, math
from datetime import datetime, timedelta
from typing import Optional

from auth import (create_user, verify_user, create_token,
                  get_current_user, UserIn, UserLogin, Token)
from kite_service import KiteService, build_synthetic_chain, build_ic_structure, find_16delta_strike
from kite_data_patch import compute_ivp_ivr, compute_indicators, get_nifty_spot, stock_signal, get_movers
from database import init_db, get_db

# ── Feature modules (safe import — won't crash if file missing) ───────────
try:
    import decision_engine
except ImportError:
    decision_engine = None
try:
    import replacement_engine
except ImportError:
    replacement_engine = None
try:
    import features_all
except ImportError:
    features_all = None
try:
    import ai_service
except ImportError:
    ai_service = None
try:
    import rebalancing_engine
except ImportError:
    rebalancing_engine = None
try:
    import athena_core
except ImportError:
    athena_core = None


# With the other safe imports:
try:
    import athena_market
except ImportError:
    athena_market = None


try:
    import athena_dashboard
except ImportError:
    athena_dashboard = None

try:
    import income_engine
except ImportError:
    income_engine = None

try:
    import ai_gemini
except ImportError:
    ai_gemini = None

try:
    import discovery_engine
except ImportError:
    discovery_engine = None

try:
    import corpus_adjust
except ImportError:
    corpus_adjust = None

try:
    import corpus_adjust
except ImportError:
    corpus_adjust = None

try:
    import options_data
except ImportError:
    options_data = None

try:
    import strategy_matrix
except ImportError:
    strategy_matrix = None

try:
    import core22_framework
except ImportError:
    core22_framework = None

@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    _update_db_schema()
    if features_all: features_all.init_schema()
    if athena_core:  athena_core.init_schema()
    if income_engine: income_engine.init_schema()
    if ai_gemini: ai_gemini.init_schema()
    if discovery_engine: discovery_engine.init_schema()   # in lifespan
    if core22_framework: core22_framework.init_schema()   # in lifespan
    print("✅  ASOS Platform ready — http://localhost:8000")
    yield

app = FastAPI(title="ASOS Wealth Platform", version="2.0.0", lifespan=lifespan)
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_credentials=True,
                   allow_methods=["*"], allow_headers=["*"])

# ── Register feature routes (MUST be after app creation) ──────────────────
if decision_engine:     decision_engine.register_routes(app)
if replacement_engine:  replacement_engine.register_routes(app)
if features_all:        features_all.register_routes(app)
if rebalancing_engine:  rebalancing_engine.register_routes(app)
if athena_core:         athena_core.register_routes(app)
if athena_market: athena_market.register_routes(app)
if athena_dashboard: athena_dashboard.register_routes(app)
if income_engine: income_engine.register_routes(app)
if ai_gemini: ai_gemini.register_routes(app)
if discovery_engine: discovery_engine.register_routes(app)
if corpus_adjust: corpus_adjust.register_routes(app)
if options_data: options_data.register_routes(app)
if strategy_matrix: strategy_matrix.register_routes(app)
if core22_framework: core22_framework.register_routes(app)

# ── Default Core 22 targets ───────────────────────────────────────────────────
CORE22_TARGETS = [
    {"ticker":"NIFTYBEES", "bucket":1,"target_pct":12,"sip":12000,"role":"India index core"},
    {"ticker":"MON100",    "bucket":1,"target_pct":10,"sip":10000,"role":"Global tech hedge"},
    {"ticker":"JUNIORBEES","bucket":1,"target_pct": 8,"sip": 8000,"role":"Nifty Next 50"},
    {"ticker":"CGPOWER",   "bucket":2,"target_pct": 9,"sip": 8000,"role":"Power infra"},
    {"ticker":"TATAPOWER", "bucket":2,"target_pct": 7,"sip": 4000,"role":"Renewables"},
    {"ticker":"BDL",       "bucket":2,"target_pct": 5,"sip": 5000,"role":"Defence PSU"},
    {"ticker":"HBLENGINE", "bucket":2,"target_pct": 4,"sip": 7000,"role":"Battery EV"},
    {"ticker":"HINDCOPPER","bucket":3,"target_pct": 5,"sip": 8000,"role":"Copper metals"},
    {"ticker":"HINDALCO",  "bucket":3,"target_pct": 5,"sip": 7000,"role":"Aluminium"},
    {"ticker":"ANGELONE",  "bucket":3,"target_pct": 4,"sip": 5000,"role":"Wealth mgmt"},
    {"ticker":"FINCABLES", "bucket":3,"target_pct": 4,"sip": 6000,"role":"Cable infra"},
    {"ticker":"GRANULES",  "bucket":3,"target_pct": 4,"sip": 6000,"role":"API pharma"},
    {"ticker":"SONACOMS",  "bucket":3,"target_pct": 3,"sip": 3000,"role":"EV drivetrain"},
    {"ticker":"PRICOLLTD", "bucket":3,"target_pct": 2,"sip": 2000,"role":"Precision auto"},
    {"ticker":"INDUSINDBK","bucket":3,"target_pct": 2,"sip": 7000,"role":"Private bank"},
    {"ticker":"RELIANCE",  "bucket":3,"target_pct": 2,"sip": 3000,"role":"Conglomerate"},
    {"ticker":"PIRAMALFIN","bucket":4,"target_pct": 3,"sip": 6000,"role":"NBFC rebuild"},
    {"ticker":"HSCL",      "bucket":4,"target_pct": 3,"sip": 2000,"role":"Spec chemicals"},
    {"ticker":"SHILCHAR",  "bucket":4,"target_pct": 2,"sip": 2000,"role":"Transformers"},
    {"ticker":"GMDCLTD",   "bucket":4,"target_pct": 2,"sip": 4000,"role":"Mining"},
    {"ticker":"GOLDBEES",  "bucket":5,"target_pct": 3,"sip": 1000,"role":"Gold ETF"},
    {"ticker":"SILVERETF", "bucket":5,"target_pct": 2,"sip": 1000,"role":"Silver ETF"},
]

DEFAULT_WATCHLIST = [
    {"ticker":"KAYNES","sector":"Electronics","roce":41,"de":0.08,"rev_cagr":45,"score":87,
     "thesis":"PCB assemblies for defence, EV, aerospace. ROCE 41% near-zero debt.",
     "entry_trigger":"ADX <18, within 5% of 52-week low"},
    {"ticker":"POLYCAB","sector":"Cables","roce":29,"de":0.1,"rev_cagr":22,"score":82,
     "thesis":"Dominant cable + wire player. Better brand than FINCABLES.",
     "entry_trigger":"P/E below 35x or FINCABLES 2Q PAT decline"},
    {"ticker":"WAAREE","sector":"Solar","roce":31,"de":0.2,"rev_cagr":68,"score":80,
     "thesis":"India's largest solar module maker. 500GW target beneficiary.",
     "entry_trigger":"25%+ correction from peak"},
    {"ticker":"HAL","sector":"Defence","roce":28,"de":0.0,"rev_cagr":19,"score":79,
     "thesis":"Tejas jets + MRO. ₹1.7L Cr order book = 10yr revenue visibility.",
     "entry_trigger":"Correction to ₹3,800-4,000 range"},
    {"ticker":"LAURUS","sector":"Pharma CDMO","roce":24,"de":0.3,"rev_cagr":18,"score":76,
     "thesis":"API + CDMO. USFDA approvals unlocking US market.",
     "entry_trigger":"2 consecutive quarters PAT growth"},
    {"ticker":"APAR","sector":"Conductors","roce":22,"de":0.4,"rev_cagr":25,"score":74,
     "thesis":"Transformer oil + cables + conductors. All 3 infra tailwinds.",
     "entry_trigger":"P/E below 18x"},
]



FRONTEND = os.path.join(os.path.dirname(__file__), "..", "frontend")
if os.path.exists(FRONTEND):
    app.mount("/static", StaticFiles(directory=FRONTEND), name="static")

@app.get("/", response_class=HTMLResponse)
async def root():
    return RedirectResponse("/static/login.html")


# ── DB schema update ──────────────────────────────────────────────────────────
def _update_db_schema():
    db = get_db()
    # Add new columns to user_settings if they don't exist
    for col, typ, default in [
        ("withdrawal_amount",  "REAL",    "0"),
        ("pending_credit",     "REAL",    "0"),
        ("sip_date",           "INTEGER", "5"),
        ("notes",              "TEXT",    "''"),
        ("glm_api_key",        "TEXT",    "''"),
        ("ai_provider",        "TEXT",    "'glm'"),
        ("ai_total_tokens",    "INTEGER", "0"),
    ]:
        try:
            db.execute(f"ALTER TABLE user_settings ADD COLUMN {col} {typ} DEFAULT {default}")
            db.commit()
        except Exception:
            pass  # Column already exists

    # Holdings store table
    db.execute("""
        CREATE TABLE IF NOT EXISTS stored_holdings (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER REFERENCES users(id),
            ticker TEXT,
            quantity INTEGER DEFAULT 0,
            average_price REAL DEFAULT 0,
            last_price REAL DEFAULT 0,
            product TEXT DEFAULT 'CNC',
            source TEXT DEFAULT 'manual',
            updated_at TEXT DEFAULT (datetime('now'))
        )
    """)
    # Unique constraint per user+ticker
    try:
        db.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_holdings ON stored_holdings(user_id, ticker)")
    except Exception:
        pass
    db.commit()


# ════════════════════════════════════════════════════════════════════════════
# AUTH
# ════════════════════════════════════════════════════════════════════════════
@app.post("/auth/register", response_model=Token)
async def register(user: UserIn):
    db_user = create_user(user.email, user.password, user.name)
    if not db_user:
        raise HTTPException(400, "Email already registered")
    token = create_token({"sub": db_user["email"], "uid": db_user["id"]})
    return {"access_token": token, "token_type": "bearer",
            "user": {"email": db_user["email"], "name": db_user["name"], "id": db_user["id"]}}

@app.post("/auth/login", response_model=Token)
async def login(creds: UserLogin):
    user = verify_user(creds.email, creds.password)
    if not user:
        raise HTTPException(401, "Invalid email or password")
    token = create_token({"sub": user["email"], "uid": user["id"]})
    # Seed default watchlist for new users
    _seed_watchlist(user["id"])
    return {"access_token": token, "token_type": "bearer",
            "user": {"email": user["email"], "name": user["name"], "id": user["id"]}}

@app.get("/auth/me")
async def me(current_user=Depends(get_current_user)):
    return current_user


# ════════════════════════════════════════════════════════════════════════════
# KITE CONNECT
# ════════════════════════════════════════════════════════════════════════════
@app.post("/kite/setup")
async def kite_setup(data: dict, current_user=Depends(get_current_user)):
    db = get_db()
    db.execute("UPDATE users SET kite_api_key=?, kite_api_secret=? WHERE id=?",
               (data["api_key"], data["api_secret"], current_user["id"]))
    db.commit()
    return {"message": "API credentials saved."}

@app.get("/kite/login-url")
async def kite_login_url(current_user=Depends(get_current_user)):
    db  = get_db()
    row = db.execute("SELECT kite_api_key FROM users WHERE id=?", (current_user["id"],)).fetchone()
    if not row or not row["kite_api_key"]:
        raise HTTPException(400, "Please set your Zerodha API key first.")
    return {"login_url": f"https://kite.zerodha.com/connect/login?api_key={row['kite_api_key']}&v=3"}

@app.get("/kite/callback")
async def kite_callback(request: Request, request_token: str = None, status: str = None):
    if status != "success" or not request_token:
        return HTMLResponse("<script>window.location='/static/login.html?error=kite_failed'</script>")
    html = f"""<html><body><p>Connecting…</p><script>
    const rt="{request_token}",jwt=localStorage.getItem("asos_token");
    if(!jwt){{window.location="/static/login.html";}}
    else{{fetch("/kite/exchange",{{method:"POST",headers:{{"Content-Type":"application/json","Authorization":"Bearer "+jwt}},body:JSON.stringify({{request_token:rt}})}})
    .then(r=>r.json()).then(d=>{{window.location="/static/app.html?kite=connected";}})
    .catch(()=>{{window.location="/static/app.html?kite=failed";}});}}
    </script></body></html>"""
    return HTMLResponse(html)

@app.post("/kite/exchange")
async def kite_exchange(data: dict, current_user=Depends(get_current_user)):
    db  = get_db()
    row = db.execute("SELECT kite_api_key, kite_api_secret FROM users WHERE id=?", (current_user["id"],)).fetchone()
    if not row or not row["kite_api_key"]:
        raise HTTPException(400, "Zerodha API key not set.")
    svc     = KiteService(row["kite_api_key"], row["kite_api_secret"])
    session = svc.generate_session(data["request_token"])
    if not session:
        raise HTTPException(400, "Failed to connect Zerodha.")
    db.execute("UPDATE users SET kite_access_token=?, kite_connected=1 WHERE id=?",
               (session["access_token"], current_user["id"]))
    db.commit()
    return {"message": "Zerodha connected!", "profile": session.get("user_name", "")}

@app.get("/kite/status")
async def kite_status(current_user=Depends(get_current_user)):
    db  = get_db()
    row = db.execute("SELECT kite_connected, kite_api_key FROM users WHERE id=?",
                     (current_user["id"],)).fetchone()
    return {"connected": bool(row and row["kite_connected"]),
            "has_api_key": bool(row and row["kite_api_key"])}


# ── Helper — get Kite service (DOES NOT RAISE 401) ────────────────────────────
def _kite_or_none(user_id: int):
    """Returns KiteService if connected, None otherwise. Never raises."""
    try:
        db  = get_db()
        row = db.execute("SELECT kite_api_key, kite_api_secret, kite_access_token FROM users WHERE id=?",
                         (user_id,)).fetchone()
        if not row or not row["kite_access_token"]:
            return None
        return KiteService(row["kite_api_key"], row["kite_api_secret"], row["kite_access_token"])
    except Exception:
        return None


# ════════════════════════════════════════════════════════════════════════════
# PORTFOLIO HOLDINGS — live Zerodha OR stored CSV/manual
# ════════════════════════════════════════════════════════════════════════════
@app.get("/portfolio/holdings")
async def get_holdings(current_user=Depends(get_current_user)):
    uid  = current_user["id"]
    kite = _kite_or_none(uid)

    if kite:
        # ── Live from Zerodha ─────────────────────────────────────────────
        live = kite.get_holdings()
        if live:
            # Sync to DB as cache
            db = get_db()
            for h in live:
                db.execute("""
                    INSERT OR REPLACE INTO stored_holdings
                    (user_id, ticker, quantity, average_price, last_price, product, source, updated_at)
                    VALUES (?,?,?,?,?,?,?,datetime('now'))
                """, (uid, h.get("tradingsymbol",""), h.get("quantity",0),
                      h.get("average_price",0), h.get("last_price",0),
                      h.get("product","CNC"), "zerodha"))
            db.commit()
            invested = sum(h.get("average_price",0)*h.get("quantity",0) for h in live)
            value    = sum(h.get("last_price",0)*h.get("quantity",0) for h in live)
            return _holdings_response(live, invested, value, "zerodha_live")

    # ── Fallback: stored holdings (from CSV upload or manual entry) ───────
    db   = get_db()
    rows = db.execute("SELECT * FROM stored_holdings WHERE user_id=?", (uid,)).fetchall()
    if rows:
        holdings = []
        for r in rows:
            # Try to get latest price from Yahoo Finance
            ltp = r["last_price"]
            try:
                import yfinance as yf
                t   = yf.Ticker(r["ticker"] + ".NS")
                inf = t.fast_info
                ltp = float(inf.last_price or r["last_price"])
                # Update stored price
                db.execute("UPDATE stored_holdings SET last_price=? WHERE user_id=? AND ticker=?",
                           (ltp, uid, r["ticker"]))
            except Exception:
                pass
            holdings.append({
                "tradingsymbol": r["ticker"], "quantity": r["quantity"],
                "average_price": r["average_price"], "last_price": ltp, "product": r["product"]
            })
        db.commit()
        invested = sum(h["average_price"]*h["quantity"] for h in holdings)
        value    = sum(h["last_price"]*h["quantity"] for h in holdings)
        return _holdings_response(holdings, invested, value, "stored_with_live_price")

    # ── No data — return empty with instructions ──────────────────────────
    return {"holdings": [], "total_invested": 0, "total_value": 0,
            "pnl": 0, "pnl_pct": 0, "count": 0,
            "source": "empty",
            "message": "No holdings found. Connect Zerodha or upload your holdings CSV."}


def _holdings_response(holdings, invested, value, source):
    pnl = value - invested
    return {
        "holdings":        holdings,
        "total_invested":  round(invested, 2),
        "total_value":     round(value, 2),
        "pnl":             round(pnl, 2),
        "pnl_pct":         round(pnl/invested*100, 2) if invested else 0,
        "count":           len(holdings),
        "source":          source,
    }


@app.post("/portfolio/holdings/upload")
async def upload_holdings_csv(file: UploadFile = File(...),
                               current_user=Depends(get_current_user)):
    """
    Upload Zerodha holdings CSV.
    Zerodha format: Instrument,Qty,Avg. cost,LTP,Cur. val,P&L,Net chg.,Day chg.
    """
    content = await file.read()
    text    = content.decode("utf-8-sig")
    db      = get_db()
    uid     = current_user["id"]
    count   = 0
    try:
        reader = csv.DictReader(io.StringIO(text))
        for row in reader:
            # Clean up column names
            row = {k.strip().lower().replace(" ","_").replace(".",""):v.strip() for k,v in row.items()}
            ticker = (row.get("instrument") or row.get("tradingsymbol") or "").upper().replace(" ","")
            if not ticker or ticker.lower() in ("instrument","total",""):
                continue
            qty = int(float(row.get("qty","0").replace(",","") or 0))
            avg = float(row.get("avg_cost","0").replace(",","") or
                        row.get("average_price","0").replace(",","") or 0)
            ltp = float(row.get("ltp","0").replace(",","") or
                        row.get("last_price","0").replace(",","") or 0)
            if qty <= 0:
                continue
            db.execute("""
                INSERT OR REPLACE INTO stored_holdings
                (user_id,ticker,quantity,average_price,last_price,product,source,updated_at)
                VALUES (?,?,?,?,?,?,?,datetime('now'))
            """, (uid, ticker, qty, avg, ltp, "CNC", "csv_upload"))
            count += 1
        db.commit()
        return {"message": f"✓ {count} holdings imported from CSV", "count": count}
    except Exception as e:
        raise HTTPException(400, f"CSV parse error: {str(e)}")


@app.post("/portfolio/holdings/manual")
async def save_manual_holdings(data: dict, current_user=Depends(get_current_user)):
    """Save a single holding manually."""
    db = get_db()
    ticker = data.get("ticker","").upper().strip()
    if not ticker:
        raise HTTPException(400, "Ticker required")
    db.execute("""
        INSERT OR REPLACE INTO stored_holdings
        (user_id,ticker,quantity,average_price,last_price,product,source,updated_at)
        VALUES (?,?,?,?,?,?,?,datetime('now'))
    """, (current_user["id"], ticker,
          int(data.get("quantity",0)),
          float(data.get("average_price",0)),
          float(data.get("last_price",0)),
          data.get("product","CNC"), "manual"))
    db.commit()
    return {"message": f"✓ {ticker} saved"}


@app.delete("/portfolio/holdings/{ticker}")
async def delete_holding(ticker: str, current_user=Depends(get_current_user)):
    db = get_db()
    db.execute("DELETE FROM stored_holdings WHERE user_id=? AND ticker=?",
               (current_user["id"], ticker.upper()))
    db.commit()
    return {"message": f"✓ {ticker} removed"}


@app.get("/portfolio/positions")
async def get_positions(current_user=Depends(get_current_user)):
    kite = _kite_or_none(current_user["id"])
    if kite:
        pos = kite.get_positions()
        return {"positions": pos or {"net":[],"day":[]}, "source":"zerodha"}
    return {"positions": {"net":[],"day":[]}, "source":"demo",
            "message":"Connect Zerodha for live positions"}


# ════════════════════════════════════════════════════════════════════════════
# CORPUS — with withdrawal, pending credit, live projection
# ════════════════════════════════════════════════════════════════════════════
@app.get("/portfolio/summary")
async def portfolio_summary(current_user=Depends(get_current_user)):
    uid  = current_user["id"]
    # Get holdings (this handles Kite or stored)
    hold_resp = await get_holdings(current_user)
    total_val = hold_resp.get("total_value", 0)
    invested  = hold_resp.get("total_invested", 0)

    db    = get_db()
    row   = db.execute("SELECT * FROM user_settings WHERE user_id=?", (uid,)).fetchone()
    sip       = row["sip_amount"]         if row else 100000
    cagr      = row["target_cagr"]        if row else 20
    year      = row["target_year"]        if row else 2047
    withdraw  = row["withdrawal_amount"]  if row else 0
    pending   = row["pending_credit"]     if row else 0

    # Effective corpus = holdings value - withdrawal + pending credit
    effective = total_val - withdraw + pending

    years  = year - datetime.now().year
    mr     = (cagr/100)/12
    months = years*12
    sip_fv = sip*((1+mr)**months-1)/mr*(1+mr) if mr > 0 else sip*months
    cor_fv = effective*(1+cagr/100)**years

    return {
        "corpus":           round(total_val, 0),
        "invested":         round(invested, 0),
        "withdrawal":       round(withdraw, 0),
        "pending_credit":   round(pending, 0),
        "effective_corpus": round(effective, 0),
        "pnl":              round(total_val - invested, 0),
        "sip":              sip,
        "target_cagr":      cagr,
        "target_year":      year,
        "years_left":       years,
        "projected":        round((sip_fv + cor_fv)/1e7, 2),
        "holdings_count":   hold_resp.get("count", 0),
        "source":           hold_resp.get("source","unknown"),
    }


# ════════════════════════════════════════════════════════════════════════════
# CORE 22 GAP ANALYSIS — real-time comparison of actual vs target
# ════════════════════════════════════════════════════════════════════════════
@app.get("/portfolio/core22-gap")
async def core22_gap(current_user=Depends(get_current_user)):
    """
    Compare actual holdings against Core 22 targets.
    Returns: buy list, sell list, hold list, rebalance actions.
    """
    hold_resp = await get_holdings(current_user)
    holdings  = hold_resp.get("holdings", [])
    corpus    = hold_resp.get("total_value", 0)

    db  = get_db()
    row = db.execute("SELECT * FROM user_settings WHERE user_id=?",
                     (current_user["id"],)).fetchone()
    sip        = row["sip_amount"]        if row else 100000
    withdrawal = row["withdrawal_amount"] if row else 0
    pending    = row["pending_credit"]    if row else 0
    effective  = corpus - withdrawal + pending

    # Build holdings dict for quick lookup
    held = {h["tradingsymbol"].upper(): h for h in holdings}

    gap_analysis = []
    for target in CORE22_TARGETS:
        ticker     = target["ticker"]
        target_pct = target["target_pct"] / 100
        target_val = round(effective * target_pct, 0)
        target_sip = target["sip"]

        current = held.get(ticker)
        if current:
            current_val  = round(current["last_price"] * current["quantity"], 0)
            current_pct  = round(current_val / effective * 100, 1) if effective else 0
            gap_val      = round(target_val - current_val, 0)
            action       = "HOLD" if abs(gap_val) < target_val * 0.1 else (
                           "BUY"  if gap_val > 0 else "TRIM")
            qty_to_buy   = max(0, round(gap_val / max(current["last_price"],1)))
        else:
            current_val  = 0
            current_pct  = 0
            gap_val      = target_val
            action       = "BUY (not held)"
            qty_to_buy   = 0  # unknown without price

        gap_analysis.append({
            "ticker":       ticker,
            "bucket":       target["bucket"],
            "role":         target["role"],
            "target_pct":   target["target_pct"],
            "target_val":   int(target_val),
            "current_val":  int(current_val),
            "current_pct":  current_pct,
            "gap_val":      int(gap_val),
            "gap_pct":      round((current_pct - target["target_pct"]), 1),
            "action":       action,
            "monthly_sip":  target_sip,
            "held":         bool(current),
            "quantity":     current["quantity"] if current else 0,
            "avg_price":    current["average_price"] if current else 0,
            "ltp":          current["last_price"] if current else 0,
        })

    # Stocks held but NOT in Core 22 → sell candidates
    core22_tickers = {t["ticker"] for t in CORE22_TARGETS}
    sell_list = []
    for ticker, h in held.items():
        if ticker not in core22_tickers:
            val = h["last_price"] * h["quantity"]
            sell_list.append({
                "ticker":   ticker,
                "quantity": h["quantity"],
                "ltp":      h["last_price"],
                "value":    round(val, 0),
                "action":   "SELL — not in Core 22",
            })

    total_gap     = sum(g["gap_val"] for g in gap_analysis if g["gap_val"] > 0)
    completion    = round(sum(1 for g in gap_analysis if g["held"] and g["action"]=="HOLD")
                          / len(gap_analysis) * 100, 1)

    return {
        "gap_analysis":    gap_analysis,
        "sell_list":       sell_list,
        "effective_corpus":round(effective, 0),
        "total_gap_to_fill":int(total_gap),
        "completion_pct":  completion,
        "holdings_source": hold_resp.get("source","unknown"),
    }


# ════════════════════════════════════════════════════════════════════════════
# MARKET DATA
# ════════════════════════════════════════════════════════════════════════════
@app.get("/market/snapshot")
async def market_snapshot():
    try:
        iv   = compute_ivp_ivr()
        nf   = compute_indicators("^NSEI")
        bnf  = compute_indicators("^NSEBANK")
        spot = get_nifty_spot()
        return {
            "nifty":           {"spot": spot.get("nifty",0),      "change_pct": spot.get("nifty_chg",0)},
            "banknifty":       {"spot": spot.get("banknifty",0),  "change_pct": spot.get("bnifty_chg",0)},
            "vix":             iv["vix"],  "ivp": iv["ivp"],  "ivr": iv["ivr"],
            "nifty_adx":       nf["adx"],  "nifty_rsi":  nf["rsi"],
            "nifty_dma50":     nf["dma50"],"nifty_dma50_gap": nf["pct_from_dma"],
            "deploy_signal":   "DEPLOY" if iv["vix"]<18 and 30<=iv["ivp"]<=80 and nf["adx"]<20
                               else "CAUTION" if iv["vix"]<25 else "HOLD",
            "timestamp":       datetime.now().isoformat()
        }
    except Exception as e:
        raise HTTPException(500, f"Market data error: {str(e)}")


NIFTY50_YF = [
    "RELIANCE.NS","TCS.NS","HDFCBANK.NS","INFY.NS","ICICIBANK.NS","HINDUNILVR.NS",
    "SBIN.NS","BHARTIARTL.NS","ITC.NS","KOTAKBANK.NS","LT.NS","AXISBANK.NS",
    "ASIANPAINT.NS","MARUTI.NS","SUNPHARMA.NS","TITAN.NS","BAJFINANCE.NS","HCLTECH.NS",
    "WIPRO.NS","NTPC.NS","POWERGRID.NS","TATAMOTORS.NS","TATASTEEL.NS","HINDALCO.NS",
    "JSWSTEEL.NS","CIPLA.NS","DRREDDY.NS","BAJAJFINSV.NS","INDUSINDBK.NS","APOLLOHOSP.NS",
    "BPCL.NS","TATACONSUM.NS","LTIM.NS","SHRIRAMFIN.NS","BAJAJ-AUTO.NS",
]

@app.get("/market/movers")
async def market_movers():
    return get_movers()

# ════════════════════════════════════════════════════════════════════════════
# OPTIONS CHAIN — comprehensive (no Kite required)
# ════════════════════════════════════════════════════════════════════════════
@app.get("/market/options-chain/{symbol}")
async def options_chain_full(symbol: str, expiry: str = "",
                              current_user=Depends(get_current_user)):
    """
    Full options chain with OI analysis, greeks, IC builder, Max Pain.
    Works without Kite — uses Black-Scholes when not connected.
    """
    CONFIGS = {
        "NIFTY":     {"spot_key":"nifty",    "wing":100,"lot":25,"step":50, "yf":"^NSEI"},
        "BANKNIFTY": {"spot_key":"banknifty","wing":200,"lot":15,"step":100,"yf":"^NSEBANK"},
    }
    cfg  = CONFIGS.get(symbol.upper(), CONFIGS["NIFTY"])
    iv   = compute_ivp_ivr()
    spot_data = get_nifty_spot()
    spot = spot_data.get(cfg["spot_key"], 24000)

    # Next Thursday (weekly expiry)
    if not expiry:
        today    = datetime.now()
        days_to  = (3 - today.weekday()) % 7
        if days_to == 0 and today.hour >= 15: days_to = 7
        if days_to == 0: days_to = 7
        exp_date = today + timedelta(days=days_to)
        expiry   = exp_date.strftime("%Y-%m-%d")

    dte = max((datetime.strptime(expiry, "%Y-%m-%d") - datetime.now()).days, 0)

    # Try Kite first, fallback to synthetic
    kite  = _kite_or_none(current_user["id"])
    chain = kite.get_option_chain(symbol, expiry) if kite else None
    source = "live_kite" if chain else "black_scholes"
    if not chain:
        chain = build_synthetic_chain(spot, iv["vix"], max(dte,1), cfg["step"])

    # ── Add IC recommendation ─────────────────────────────────────────────
    ic = build_ic_structure(spot, iv["vix"], max(dte,1),
                             cfg["wing"], cfg["wing"], cfg["step"], cfg["lot"])

    # ── Max Pain calculation (synthetic — uniform distribution assumption) ─
    atm_strike = round(spot / cfg["step"]) * cfg["step"]
    total_oi   = sum((s.get("call_ltp",0) + s.get("put_ltp",0)) for s in chain)
    max_pain   = atm_strike  # simplified — ATM approximation

    # ── PCR (from OI or LTP proxy) ────────────────────────────────────────
    total_put_oi  = sum(s.get("put_ltp",0) * 100 for s in chain)
    total_call_oi = sum(s.get("call_ltp",0) * 100 for s in chain) or 1
    pcr = round(total_put_oi / total_call_oi, 2)

    # Get next 4 expiries for selector
    expiries = []
    today = datetime.now()
    for week in range(4):
        days = ((3 - today.weekday()) % 7) + week * 7
        if days == 0: days = 7
        exp = today + timedelta(days=days)
        expiries.append(exp.strftime("%Y-%m-%d"))

    return {
        "symbol":   symbol.upper(),
        "spot":     round(spot, 1),
        "expiry":   expiry,
        "dte":      dte,
        "vix":      iv["vix"],
        "ivp":      iv["ivp"],
        "atm":      atm_strike,
        "step":     cfg["step"],
        "lot":      cfg["lot"],
        "wing":     cfg["wing"],
        "pcr":      pcr,
        "max_pain": max_pain,
        "deploy_signal": "DEPLOY" if iv["vix"]<20 and 30<=iv["ivp"]<=80 else "WAIT",
        "ic":       ic,
        "chain":    chain,
        "expiries": expiries,
        "source":   source,
        "timestamp":datetime.now().isoformat()
    }


# ════════════════════════════════════════════════════════════════════════════
# STRATEGY SELECTOR
# ════════════════════════════════════════════════════════════════════════════
@app.get("/strategy/recommend")
async def recommend_strategy():
    iv  = compute_ivp_ivr()
    nf  = compute_indicators("^NSEI")
    vix = iv["vix"]; ivp = iv["ivp"]; adx = nf["adx"]

    strategies = []
    if vix < 20 and 30 <= ivp <= 80 and adx < 20:
        strategies.append({"name":"Iron Condor","confidence":90,"action":"DEPLOY",
            "capital":"15%","reason":f"VIX {vix:.1f} · IVP {ivp:.0f}% · ADX {adx:.1f}"})
    if adx > 25 and vix < 22:
        strategies.append({"name":"ATM Straddle (breakout)","confidence":72,
            "action":"DEPLOY 5%","capital":"5%",
            "reason":f"ADX {adx:.1f} > 25 — directional breakout"})
    if ivp > 70 and vix > 18:
        strategies.append({"name":"Iron Butterfly","confidence":68,
            "action":"CONSIDER","capital":"10%",
            "reason":f"IVP {ivp:.0f}% high — tight IC"})
    if vix > 18:
        strategies.append({"name":"Park Liquid Fund","confidence":95,
            "action":"PARK SIP","capital":"100% of SIP",
            "reason":f"VIX {vix:.1f} > 18 — SIP pause"})
    if not strategies:
        strategies.append({"name":"Hold — wait for setup","confidence":50,
            "action":"WAIT","capital":"0%","reason":"No clear edge"})

    return {"vix":vix,"ivp":ivp,"adx":adx,"rsi":nf["rsi"],
            "top":strategies[0],"all":strategies,
            "timestamp":datetime.now().isoformat()}


# ════════════════════════════════════════════════════════════════════════════
# SETTINGS
# ════════════════════════════════════════════════════════════════════════════
@app.get("/settings")
async def get_settings(current_user=Depends(get_current_user)):
    db  = get_db()
    row = db.execute("SELECT * FROM user_settings WHERE user_id=?",
                     (current_user["id"],)).fetchone()
    if not row:
        return {"sip_amount":100000,"target_cagr":20,"target_year":2047,
                "withdrawal_amount":0,"pending_credit":0,"sip_date":5,
                "telegram_token":"","telegram_chat_id":"",
                "glm_api_key":"","ai_total_tokens":0,"glm_key_set":False}
    d = dict(row)
    # Never return full GLM key — mask it
    if d.get("glm_api_key"):
        d["glm_key_set"]  = True
        d["glm_api_key"]  = ""   # cleared for security
    else:
        d["glm_key_set"]  = False
    return d

@app.post("/settings")
async def save_settings(data: dict, current_user=Depends(get_current_user)):
    db  = get_db()
    uid = current_user["id"]
    # Preserve existing GLM key if not providing new one
    new_glm = data.get("glm_api_key", "").strip()
    existing = db.execute("SELECT glm_api_key FROM user_settings WHERE user_id=?", (uid,)).fetchone()
    glm_key  = new_glm if new_glm else (existing["glm_api_key"] if existing else "")
    db.execute("""
        INSERT INTO user_settings
          (user_id,sip_amount,target_cagr,target_year,
           withdrawal_amount,pending_credit,sip_date,
           telegram_token,telegram_chat_id,
           glm_api_key,ai_provider)
        VALUES (?,?,?,?,?,?,?,?,?,?,?)
        ON CONFLICT(user_id) DO UPDATE SET
          sip_amount=excluded.sip_amount,
          target_cagr=excluded.target_cagr,
          target_year=excluded.target_year,
          withdrawal_amount=excluded.withdrawal_amount,
          pending_credit=excluded.pending_credit,
          sip_date=excluded.sip_date,
          telegram_token=excluded.telegram_token,
          telegram_chat_id=excluded.telegram_chat_id,
          glm_api_key=CASE WHEN excluded.glm_api_key='' THEN glm_api_key ELSE excluded.glm_api_key END,
          ai_provider=excluded.ai_provider
    """, (uid,
          data.get("sip_amount",100000),  data.get("target_cagr",20),
          data.get("target_year",2047),   data.get("withdrawal_amount",0),
          data.get("pending_credit",0),   data.get("sip_date",5),
          data.get("telegram_token",""),  data.get("telegram_chat_id",""),
          glm_key, data.get("ai_provider","glm")))
    db.commit()
    return {"message": "Settings saved", "glm_key_updated": bool(new_glm)}


# ════════════════════════════════════════════════════════════════════════════
# WATCHLIST — dynamic with ASOS scoring
# ════════════════════════════════════════════════════════════════════════════
def _seed_watchlist(user_id: int):
    db = get_db()
    existing = db.execute("SELECT COUNT(*) as c FROM watchlist WHERE user_id=?",
                          (user_id,)).fetchone()["c"]
    if existing > 0: return
    for s in DEFAULT_WATCHLIST:
        db.execute("""
            INSERT OR IGNORE INTO watchlist
              (user_id,ticker,sector,roce,de,rev_cagr,score,thesis,entry_trigger)
            VALUES (?,?,?,?,?,?,?,?,?)
        """, (user_id, s["ticker"], s["sector"], s["roce"], s["de"],
              s["rev_cagr"], s["score"], s["thesis"], s["entry_trigger"]))
    db.commit()

@app.get("/watchlist")
async def get_watchlist(current_user=Depends(get_current_user)):
    _seed_watchlist(current_user["id"])
    db   = get_db()
    rows = db.execute("SELECT * FROM watchlist WHERE user_id=? ORDER BY score DESC",
                      (current_user["id"],)).fetchall()
    return {"watchlist": [dict(r) for r in rows]}

@app.post("/watchlist")
async def add_watchlist(data: dict, current_user=Depends(get_current_user)):
    db = get_db()
    ticker = data.get("ticker","").upper().strip()
    if not ticker: raise HTTPException(400,"Ticker required")
    db.execute("""
        INSERT OR REPLACE INTO watchlist
          (user_id,ticker,sector,roce,de,rev_cagr,score,thesis,entry_trigger)
        VALUES (?,?,?,?,?,?,?,?,?)
    """, (current_user["id"], ticker,
          data.get("sector",""), data.get("roce",0), data.get("de",0),
          data.get("rev_cagr",0), data.get("score",0),
          data.get("thesis",""), data.get("entry_trigger","")))
    db.commit()
    return {"message":f"{ticker} added to watchlist"}

@app.delete("/watchlist/{ticker}")
async def del_watchlist(ticker: str, current_user=Depends(get_current_user)):
    db = get_db()
    db.execute("DELETE FROM watchlist WHERE user_id=? AND ticker=?",
               (current_user["id"], ticker.upper()))
    db.commit()
    return {"message":f"{ticker} removed"}

@app.put("/watchlist/{ticker}")
async def update_watchlist(ticker: str, data: dict,
                            current_user=Depends(get_current_user)):
    db = get_db()
    db.execute("""UPDATE watchlist SET score=?,thesis=?,entry_trigger=?,sector=?,
                  roce=?,de=?,rev_cagr=? WHERE user_id=? AND ticker=?""",
               (data.get("score",0), data.get("thesis",""),
                data.get("entry_trigger",""), data.get("sector",""),
                data.get("roce",0), data.get("de",0), data.get("rev_cagr",0),
                current_user["id"], ticker.upper()))
    db.commit()
    return {"message":f"{ticker} updated"}


# ════════════════════════════════════════════════════════════════════════════
# MARKET QUOTES
# ════════════════════════════════════════════════════════════════════════════
@app.get("/market/quotes")
async def get_quotes(symbols: str, current_user=Depends(get_current_user)):
    kite = _kite_or_none(current_user["id"])
    if kite:
        data = kite.get_quotes([s.strip() for s in symbols.split(",")])
        return {"quotes": data or {}, "source":"zerodha"}
    # Fallback: Yahoo Finance
    try:
        import yfinance as yf
        result = {}
        for sym in symbols.split(","):
            sym = sym.strip().replace("NSE:","")
            t   = yf.Ticker(sym+".NS")
            inf = t.fast_info
            result[sym] = {"last_price": float(inf.last_price or 0),
                           "change_percent": float(inf.three_month_return or 0)}
        return {"quotes": result, "source":"yahoo_finance"}
    except Exception as e:
        return {"quotes": {}, "error": str(e)}



# ════════════════════════════════════════════════════════════════════════════
# AI SCOUT — GLM 5.2 via Fireworks AI (key stored securely in DB)
# ════════════════════════════════════════════════════════════════════════════

@app.post("/ai/chat")
async def ai_chat(data: dict, current_user=Depends(get_current_user)):
    """AI Scout — routes to GLM 5.2 via Fireworks AI. Key never touches browser."""
    if not ai_service:
        raise HTTPException(503, "AI service not available")

    user_msg = data.get("message", "").strip()
    history  = data.get("history", [])
    with_ctx = data.get("include_market_context", True)

    if not user_msg:
        raise HTTPException(400, "message is required")

    db  = get_db()
    row = db.execute("SELECT * FROM user_settings WHERE user_id=?",
                     (current_user["id"],)).fetchone()
    glm_key = (row["glm_api_key"] if row and "glm_api_key" in row.keys() else "")

    # Optional market context injection
    market_ctx = {}
    if with_ctx:
        try:
            snap = await market_snapshot()
            market_ctx = {
                "vix":        snap.get("vix"),
                "ivp":        snap.get("ivp"),
                "nifty_spot": snap.get("nifty", {}).get("spot"),
            }
            hold = await get_holdings(current_user)
            withdraw = row["withdrawal_amount"] if row else 0
            pending  = row["pending_credit"]    if row else 0
            market_ctx["effective_corpus"] = hold.get("total_value", 0) - withdraw + pending
        except Exception:
            pass

    messages = ai_service.build_scout_messages(user_msg, history, market_ctx)
    result   = await ai_service.call_glm(messages=messages, api_key=glm_key,
                                          model="accounts/fireworks/models/glm-5p2",
                                          temperature=0.3, max_tokens=1500)

    # Track token usage
    if result.get("tokens", {}).get("total"):
        try:
            db.execute("UPDATE user_settings SET ai_total_tokens = COALESCE(ai_total_tokens,0) + ? WHERE user_id=?",
                       (result["tokens"]["total"], current_user["id"]))
            db.commit()
        except Exception:
            pass

    return {
        "reply":      result["text"],
        "model":      result.get("model") or "glm-5p2",
        "tokens":     result.get("tokens", {}),
        "cost_usd":   ai_service.estimate_cost(result.get("tokens", {})),
        "error":      result.get("error"),
        "market_ctx": market_ctx,
    }


@app.get("/ai/status")
async def ai_status(current_user=Depends(get_current_user)):
    """Check if GLM/Fireworks API key is set and working."""
    if not ai_service:
        return {"configured": False, "message": "AI service module not loaded"}

    db  = get_db()
    row = db.execute("SELECT * FROM user_settings WHERE user_id=?",
                     (current_user["id"],)).fetchone()
    try:
        glm_key      = row["glm_api_key"]    if row else ""
        total_tokens = row["ai_total_tokens"] if row else 0
    except Exception:
        glm_key, total_tokens = "", 0

    if not glm_key:
        return {"configured": False,
                "message": "GLM API key not set — go to Settings → GLM AI Scout"}

    test = await ai_service.call_glm(
        messages=[{"role": "user", "content": "Reply with just: OK"}],
        api_key=glm_key, model="accounts/fireworks/models/glm-5p2", max_tokens=10
    )
    return {
        "configured":   True,
        "working":      test["error"] is None,
        "model":        "accounts/fireworks/models/glm-5p2",
        "provider":     "Fireworks AI",
        "total_tokens": total_tokens,
        "message":      "GLM 5.2 connected ✓" if test["error"] is None else test.get("error",""),
    }


# ════════════════════════════════════════════════════════════════════════════
# HEALTH
# ════════════════════════════════════════════════════════════════════════════
@app.get("/health")
async def health():
    modules = {
        "decision_engine":    decision_engine is not None,
        "replacement_engine": replacement_engine is not None,
        "features_all":       features_all is not None,
        "ai_service":         ai_service is not None,
    }
    return {"status":"ok","version":"2.0.0","time":datetime.now().isoformat(),
            "modules": modules}

# ── Start background scheduler ────────────────────────────────────────────
if features_all:
    try:
        features_all.start_scheduler(app)
    except Exception as e:
        print(f"⚠  Scheduler not started: {e}")


# ════════════════════════════════════════════════════════════════════════════
# MARKET TIMING ENGINE — when to buy, trim, pause SIP
# ════════════════════════════════════════════════════════════════════════════

def _stock_signal(ticker, current_pct, target_pct, vix):
    return stock_signal(ticker, current_pct, target_pct, vix)

@app.get("/market/timing-engine")
async def timing_engine(current_user=Depends(get_current_user)):
    """Full portfolio timing signals for each Core 22 position."""
    iv    = compute_ivp_ivr()
    nf    = compute_indicators("^NSEI")
    vix   = iv["vix"]
    nifty_rsi = nf["rsi"]

    hold_resp = await get_holdings(current_user)
    holdings  = hold_resp.get("holdings", [])
    db        = get_db()
    row       = db.execute("SELECT * FROM user_settings WHERE user_id=?",
                           (current_user["id"],)).fetchone()
    withdraw  = row["withdrawal_amount"] if row else 0
    pending   = row["pending_credit"]    if row else 0
    corpus    = hold_resp.get("total_value", 0)
    effective = corpus - withdraw + pending

    held = {h["tradingsymbol"].upper(): h for h in holdings}

    # SIP mode
    if vix < 13:
        sip_mode, sip_color = "DEPLOY 75%", "var(--am)"
        sip_reason = f"VIX {vix:.1f} < 13 — Market complacent. Deploy 75% SIP, park 25% for dips."
    elif vix <= 16:
        sip_mode, sip_color = "FULL SIP ✓", "var(--gr)"
        sip_reason = f"VIX {vix:.1f} — Normal range. Deploy full ₹1L SIP on schedule."
    elif vix <= 20:
        sip_mode, sip_color = "50% SIP", "var(--am)"
        sip_reason = f"VIX {vix:.1f} — Elevated. Deploy ₹50K this cycle, park ₹50K in liquid."
    elif vix <= 25:
        sip_mode, sip_color = "PAUSE SIP ⚠", "var(--re)"
        sip_reason = f"VIX {vix:.1f} — Fear zone. PAUSE regular SIP. Accumulate in 3 tranches on dips."
    else:
        sip_mode, sip_color = "DOUBLE SIP 🚀", "var(--gr)"
        sip_reason = f"VIX {vix:.1f} — Panic. Deploy 150% SIP + use liquid reserves. Generational opportunity."

    nifty_phase = ("BULL — above 200 DMA ✓" if nf["pct_from_dma"]>2 else
                   "CAUTION — near 200 DMA" if abs(nf["pct_from_dma"])<=2 else
                   "BEAR — below 200 DMA ⚠")

    ETF_SET = {"NIFTYBEES","MON100","JUNIORBEES","GOLDBEES","SILVERETF"}
    signals, etf_sigs = [], []
    for tgt in CORE22_TARGETS:
        tk  = tgt["ticker"]
        h   = held.get(tk)
        val = (h["last_price"]*h["quantity"]) if h else 0
        cp  = (val/effective*100) if effective else 0
        if tk in ETF_SET:
            wt = cp/tgt["target_pct"] if tgt["target_pct"] else 0
            etf_sigs.append({"ticker":tk,"signal":"BUY (not held)" if not h else
                              ("HOLD" if abs(wt-1)<0.15 else ("TRIM" if wt>1.15 else "ADD")),
                              "action":f"SIP ₹{tgt['sip']:,}/month",
                              "color":"var(--bl)" if not h else "var(--gr)",
                              "current_pct":round(cp,2),"target_pct":tgt["target_pct"],
                              "rsi":None,"trim_pct":0,"sip_note":"","priority":2,
                              "pct_from_high":0,"pct_from_low":0})
            continue
        signals.append(_stock_signal(tk, cp, tgt["target_pct"], vix))

    signals.sort(key=lambda x: (x["priority"], -abs(x.get("wt_ratio",1)-1)))
    trim_stocks = [s for s in signals if "TRIM" in s["signal"]]
    trim_proceeds = sum(
        (held.get(s["ticker"],{}).get("last_price",0)*held.get(s["ticker"],{}).get("quantity",0)*s["trim_pct"]/100)
        for s in trim_stocks if held.get(s["ticker"])
    )
    return {"vix":vix,"ivp":iv["ivp"],"nifty_rsi":nifty_rsi,"nifty_phase":nifty_phase,
            "nifty_dma_gap":nf["pct_from_dma"],"sip_mode":sip_mode,"sip_color":sip_color,
            "sip_reason":sip_reason,"effective_corpus":round(effective,0),
            "holdings_count":len(holdings),"signals":signals,"etf_signals":etf_sigs,
            "trim_count":len(trim_stocks),
            "add_count":len([s for s in signals if "ADD" in s["signal"]]),
            "trim_proceeds_est":round(trim_proceeds,0),
            "timestamp":datetime.now().isoformat()}


# ════════════════════════════════════════════════════════════════════════════
# FIXED OPTIONS CHAIN — robust error handling + correct expiry
# ════════════════════════════════════════════════════════════════════════════
@app.get("/market/chain/{symbol}")
async def options_chain_v2(symbol: str, expiry: str = "",
                            current_user=Depends(get_current_user)):
    CONFIGS = {"NIFTY":{"spot_key":"nifty","wing":100,"lot":25,"step":50},
               "BANKNIFTY":{"spot_key":"banknifty","wing":200,"lot":15,"step":100}}
    cfg = CONFIGS.get(symbol.upper(), CONFIGS["NIFTY"])
    try:
        iv        = compute_ivp_ivr()
        spot_data = get_nifty_spot()
        spot      = spot_data.get(cfg["spot_key"], 24000)
        vix       = iv["vix"]
    except Exception:
        spot, vix, iv = 24000, 14.0, {"vix":14.0,"ivp":45.0,"ivr":40.0}

    today   = datetime.now()
    wday    = today.weekday()
    days_to = (3 - wday) % 7
    if days_to == 0:
        days_to = 7   # always take NEXT Thursday, not today
    dte = days_to

    if expiry:
        try:
            exp_dt  = datetime.strptime(expiry, "%Y-%m-%d")
            dte_tmp = (exp_dt - today).days
            if dte_tmp > 0:
                dte = dte_tmp
            else:
                expiry = ""
        except Exception:
            expiry = ""

    if not expiry:
        expiry = (today + timedelta(days=days_to)).strftime("%Y-%m-%d")

    dte = max(dte, 1)

    try:
        kite  = _kite_or_none(current_user["id"])
        chain = kite.get_option_chain(symbol, expiry) if kite else None
        source = "live_kite" if chain else "black_scholes"
        if not chain:
            chain = build_synthetic_chain(spot, vix, dte, cfg["step"])
    except Exception as e:
        chain  = build_synthetic_chain(spot, vix, dte, cfg["step"])
        source = f"black_scholes_fallback"

    try:
        ic = build_ic_structure(spot, vix, dte, cfg["wing"], cfg["wing"], cfg["step"], cfg["lot"])
    except Exception:
        from kite_service import bs_price
        T   = dte/365
        sig = (vix/100)*math.sqrt(T)
        atm = round(spot/cfg["step"])*cfg["step"]
        sc  = atm + cfg["wing"]
        sp  = atm - cfg["wing"]
        sc_px = round(bs_price(spot,sc,T,0.065,sig,"call"),1)
        sp_px = round(bs_price(spot,sp,T,0.065,sig,"put"),1)
        net   = round((sc_px + sp_px)*cfg["lot"],0)
        ic    = {"short_call":sc,"long_call":sc+cfg["wing"],"short_put":sp,"long_put":sp-cfg["wing"],
                 "sc_entry":sc_px,"sp_entry":sp_px,"lc_entry":round(sc_px*0.35,1),"lp_entry":round(sp_px*0.35,1),
                 "net_credit":net,"profit_target":round(net*0.5,0),"stop_loss":round(net*2,0),
                 "call_delta":-0.16,"put_delta":-0.16,"dte":dte}

    put_sum  = sum(s.get("put_ltp",0) for s in chain)
    call_sum = sum(s.get("call_ltp",0) for s in chain) or 1
    pcr      = round(put_sum/call_sum, 2)
    atm_strike = round(spot/cfg["step"])*cfg["step"]
    expiries   = [(today+timedelta(days=((3-today.weekday())%7 or 7)+w*7)).strftime("%Y-%m-%d") for w in range(4)]

    return {"symbol":symbol.upper(),"spot":round(spot,1),"expiry":expiry,"dte":dte,
            "vix":vix,"ivp":iv["ivp"],"atm":atm_strike,"step":cfg["step"],
            "lot":cfg["lot"],"wing":cfg["wing"],"pcr":pcr,"max_pain":atm_strike,
            "deploy_signal":"DEPLOY" if vix<20 and 30<=iv["ivp"]<=80 else "WAIT",
            "ic":ic,"chain":chain,"expiries":expiries,"source":source,
            "timestamp":datetime.now().isoformat()}