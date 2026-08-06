"""
features_all.py — 5 high-impact features
1. Telegram Alerts   2. SIP Optimizer   3. XIRR Calculator
4. Tax Harvesting    5. Screener.in Sync

Copy to backend/ and add to main.py:
  import features_all
  features_all.init_schema()
  features_all.register_routes(app)
  features_all.start_scheduler(app)
"""

import sqlite3, json, math, csv, io
from datetime import datetime, timedelta, date
from typing import Optional
import httpx

# ════════════════════════════════════════════════════════════════════════════
# DB SCHEMA
# ════════════════════════════════════════════════════════════════════════════
def init_schema():
    from database import get_db
    db = get_db()
    db.executescript("""
    CREATE TABLE IF NOT EXISTS transactions (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id     INTEGER REFERENCES users(id),
        ticker      TEXT    NOT NULL,
        txn_date    TEXT    NOT NULL,
        txn_type    TEXT    NOT NULL CHECK(txn_type IN ('BUY','SELL')),
        quantity    REAL    NOT NULL,
        price       REAL    NOT NULL,
        amount      REAL    GENERATED ALWAYS AS (quantity * price) STORED,
        notes       TEXT    DEFAULT '',
        created_at  TEXT    DEFAULT (datetime('now'))
    );
    CREATE TABLE IF NOT EXISTS stock_fundamentals (
        ticker      TEXT PRIMARY KEY,
        roce        REAL, de REAL, pe REAL, pb REAL,
        rev_cagr    REAL, pat_cagr REAL, promoter_pct REAL,
        promoter_pledge REAL DEFAULT 0,
        market_cap  REAL,
        sector      TEXT,
        notes       TEXT,
        source      TEXT DEFAULT 'manual',
        updated_at  TEXT DEFAULT (datetime('now'))
    );
    CREATE TABLE IF NOT EXISTS alert_log (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id     INTEGER REFERENCES users(id),
        alert_type  TEXT,
        ticker      TEXT,
        message     TEXT,
        sent_at     TEXT DEFAULT (datetime('now'))
    );
    CREATE TABLE IF NOT EXISTS sip_plans (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id     INTEGER REFERENCES users(id),
        plan_month  TEXT,
        allocations TEXT,
        total       REAL,
        created_at  TEXT DEFAULT (datetime('now'))
    );
    """)
    # Ensure user_settings has telegram fields
    for col, defval in [
        ("telegram_token",   "''"),
        ("telegram_chat_id", "''"),
        ("alert_vix_high",   "20"),
        ("alert_vix_low",    "13"),
        ("alert_sip_day",    "5"),
        ("alerts_enabled",   "1"),
    ]:
        try:
            db.execute(f"ALTER TABLE user_settings ADD COLUMN {col} TEXT DEFAULT {defval}")
        except Exception:
            pass
    db.commit()


# ════════════════════════════════════════════════════════════════════════════
# 1. TELEGRAM SERVICE
# ════════════════════════════════════════════════════════════════════════════
TG_URL = "https://api.telegram.org/bot{token}/sendMessage"

async def send_telegram(token: str, chat_id: str, message: str,
                        parse_mode: str = "HTML") -> bool:
    if not token or not chat_id:
        return False
    try:
        async with httpx.AsyncClient(timeout=10) as c:
            r = await c.post(
                TG_URL.format(token=token),
                json={"chat_id": chat_id, "text": message,
                      "parse_mode": parse_mode, "disable_web_page_preview": True}
            )
            return r.status_code == 200
    except Exception as e:
        print(f"Telegram error: {e}")
        return False


def fmt_alert(title: str, body: str, emoji: str = "📢") -> str:
    """Format a Telegram HTML message."""
    return (f"{emoji} <b>ASOS Alert — {title}</b>\n\n"
            f"{body}\n\n"
            f"<i>{datetime.now().strftime('%d %b %Y · %H:%M IST')}</i>")


async def get_user_telegram(user_id: int):
    from database import get_db
    row = get_db().execute(
        "SELECT telegram_token, telegram_chat_id, alerts_enabled FROM user_settings WHERE user_id=?",
        (user_id,)
    ).fetchone()
    if not row or not row["telegram_token"] or not row["telegram_chat_id"]:
        return None, None
    if not row["alerts_enabled"]:
        return None, None
    return row["telegram_token"], row["telegram_chat_id"]


# ════════════════════════════════════════════════════════════════════════════
# 2. SIP OPTIMIZER
# ════════════════════════════════════════════════════════════════════════════
CORE22 = [
    {"ticker":"NIFTYBEES", "bucket":1,"target_pct":12,"base_sip":12000,"is_etf":True},
    {"ticker":"MON100",    "bucket":1,"target_pct":10,"base_sip":10000,"is_etf":True},
    {"ticker":"JUNIORBEES","bucket":1,"target_pct": 8,"base_sip": 8000,"is_etf":True},
    {"ticker":"CGPOWER",   "bucket":2,"target_pct": 9,"base_sip": 8000,"is_etf":False},
    {"ticker":"TATAPOWER", "bucket":2,"target_pct": 7,"base_sip": 4000,"is_etf":False},
    {"ticker":"BDL",       "bucket":2,"target_pct": 5,"base_sip": 5000,"is_etf":False},
    {"ticker":"HBLENGINE", "bucket":2,"target_pct": 4,"base_sip": 7000,"is_etf":False},
    {"ticker":"HINDCOPPER","bucket":3,"target_pct": 5,"base_sip": 8000,"is_etf":False},
    {"ticker":"HINDALCO",  "bucket":3,"target_pct": 5,"base_sip": 7000,"is_etf":False},
    {"ticker":"ANGELONE",  "bucket":3,"target_pct": 4,"base_sip": 5000,"is_etf":False},
    {"ticker":"FINCABLES", "bucket":3,"target_pct": 4,"base_sip": 6000,"is_etf":False},
    {"ticker":"GRANULES",  "bucket":3,"target_pct": 4,"base_sip": 6000,"is_etf":False},
    {"ticker":"SONACOMS",  "bucket":3,"target_pct": 3,"base_sip": 3000,"is_etf":False},
    {"ticker":"PRICOLLTD", "bucket":3,"target_pct": 2,"base_sip": 2000,"is_etf":False},
    {"ticker":"INDUSINDBK","bucket":3,"target_pct": 2,"base_sip": 7000,"is_etf":False},
    {"ticker":"RELIANCE",  "bucket":3,"target_pct": 2,"base_sip": 3000,"is_etf":False},
    {"ticker":"PIRAMALFIN","bucket":4,"target_pct": 3,"base_sip": 6000,"is_etf":False},
    {"ticker":"HSCL",      "bucket":4,"target_pct": 3,"base_sip": 2000,"is_etf":False},
    {"ticker":"SHILCHAR",  "bucket":4,"target_pct": 2,"base_sip": 2000,"is_etf":False},
    {"ticker":"GMDCLTD",   "bucket":4,"target_pct": 2,"base_sip": 4000,"is_etf":False},
    {"ticker":"GOLDBEES",  "bucket":5,"target_pct": 3,"base_sip": 1000,"is_etf":True},
    {"ticker":"SILVERETF", "bucket":5,"target_pct": 2,"base_sip": 1000,"is_etf":True},
]

async def compute_sip_allocation(user_id: int, sip_total: float,
                                  effective_corpus: float, vix: float) -> list:
    """Allocate monthly SIP across Core 22 based on RSI + weight gap."""
    from database import get_db
    try:
        from kite_data_patch import _kite, _hist_closes, _rsi as _ks_rsi
    except ImportError:
        _kite = None
    db  = get_db()
    held_rows = db.execute(
        "SELECT ticker, quantity, last_price FROM stored_holdings WHERE user_id=?",
        (user_id,)
    ).fetchall()
    held = {r["ticker"].upper(): r for r in held_rows}

    ETF_BUDGET  = sip_total * 0.30   # always 30% to ETFs
    EQ_BUDGET   = sip_total * 0.70   # 70% optimised across equities

    items = []
    for s in CORE22:
        tk = s["ticker"]
        h  = held.get(tk)
        val = (h["quantity"] * h["last_price"]) if h else 0
        curr_pct = (val / effective_corpus * 100) if effective_corpus else 0
        weight_gap = s["target_pct"] - curr_pct  # positive = underweight

        if s["is_etf"]:
            # ETFs: proportional to base_sip, adjusted for weight gap
            score = max(0.1, s["base_sip"] + weight_gap * 500)
            items.append({**s, "curr_pct": round(curr_pct,2),
                          "weight_gap": round(weight_gap,2), "rsi": None,
                          "score": score, "budget_pool": "etf"})
            continue

        # Equities: fetch RSI
        rsi = 50.0
        try:
            if _kite:
                k = _kite(user_id)
                if k:
                    closes = _hist_closes(k, tk, 180)
                    r = _ks_rsi(closes) if closes else None
                    if r is not None: rsi = r
        except Exception:
            pass

        # Score: more weight to underweight + oversold
        oversold_bonus = max(0, (55 - rsi) / 55)  # 0 at RSI=55, 1 at RSI=0
        wt_score = max(0, weight_gap / s["target_pct"])   # 0 if on-target, 1 if fully missing
        score    = (wt_score * 0.55 + oversold_bonus * 0.45) * 100

        # VIX adjustment: high VIX → bonus to underweight
        if vix > 20:
            score *= 1.2

        # Skip overweight stocks with RSI > 65
        if curr_pct > s["target_pct"] * 1.15 and rsi > 65:
            score = 0   # no SIP here this month

        items.append({**s, "curr_pct": round(curr_pct, 2),
                      "weight_gap": round(weight_gap, 2),
                      "rsi": round(rsi, 1), "score": round(score, 2),
                      "budget_pool": "equity"})

    # Allocate ETF budget
    etf_items = [i for i in items if i["budget_pool"] == "etf"]
    etf_total  = sum(i["score"] for i in etf_items) or 1
    for i in etf_items:
        raw = ETF_BUDGET * i["score"] / etf_total
        i["sip_amount"] = round(raw / 500) * 500   # round to ₹500

    # Allocate equity budget — top 8 by score only
    eq_items = sorted([i for i in items if i["budget_pool"] == "equity"],
                      key=lambda x: -x["score"])
    active_eq = [i for i in eq_items if i["score"] > 0][:8]  # max 8 stocks
    eq_total   = sum(i["score"] for i in active_eq) or 1
    for i in active_eq:
        raw = EQ_BUDGET * i["score"] / eq_total
        i["sip_amount"] = round(raw / 500) * 500
    for i in eq_items:
        if i not in active_eq:
            i["sip_amount"] = 0

    # Add reason strings
    for i in items:
        amt = i.get("sip_amount", 0)
        if amt == 0:
            if i["curr_pct"] > i["target_pct"] * 1.15:
                i["reason"] = f"Overweight {i['curr_pct']:.1f}% vs {i['target_pct']}% — skip this month"
            else:
                i["reason"] = "Outside top-8 priority this month"
        elif i["is_etf"]:
            i["reason"] = f"ETF base SIP · weight {i['curr_pct']:.1f}% vs {i['target_pct']}% target"
        else:
            rsi = i.get("rsi", 50)
            i["reason"] = (
                f"RSI {rsi:.0f} {'(oversold ✓)' if rsi<40 else '(neutral)' if rsi<55 else ''} · "
                f"Underweight {i['weight_gap']:.1f}%"
            )

    return sorted(items, key=lambda x: -x.get("sip_amount", 0))


# ════════════════════════════════════════════════════════════════════════════
# 3. XIRR CALCULATOR
# ════════════════════════════════════════════════════════════════════════════
def xirr(cashflows: list) -> float:
    """
    Compute XIRR from a list of (date, amount) tuples.
    Negative amounts = investments (outflows).
    Positive amounts = returns/current value (inflows).
    Uses Newton-Raphson with bisection fallback.
    """
    if not cashflows or len(cashflows) < 2:
        return 0.0

    dates   = [cf[0] for cf in cashflows]
    amounts = [cf[1] for cf in cashflows]
    t0      = dates[0]
    days    = [(d - t0).days / 365.0 for d in dates]

    def npv(r):
        if r <= -1:
            return float('inf')
        return sum(a / (1 + r) ** t for a, t in zip(amounts, days))

    def dnpv(r):
        return sum(-t * a / (1 + r) ** (t + 1) for a, t in zip(amounts, days))

    # Newton-Raphson
    r = 0.15
    for _ in range(200):
        f  = npv(r)
        df = dnpv(r)
        if abs(df) < 1e-12:
            break
        r1 = r - f / df
        if abs(r1 - r) < 1e-7:
            return round(r1 * 100, 2)
        r = r1

    # Bisection fallback
    lo, hi = -0.99, 10.0
    for _ in range(100):
        mid = (lo + hi) / 2
        if npv(mid) > 0:
            lo = mid
        else:
            hi = mid
        if hi - lo < 1e-7:
            break
    return round((lo + hi) / 2 * 100, 2)


def compute_xirr_for_portfolio(user_id: int) -> dict:
    """Compute XIRR from transaction log + current holding values."""
    from database import get_db
    db   = get_db()
    txns = db.execute(
        "SELECT * FROM transactions WHERE user_id=? ORDER BY txn_date ASC",
        (user_id,)
    ).fetchall()

    if not txns:
        return {"xirr": None, "error": "No transactions found. Upload your trade history."}

    cashflows = []
    for t in txns:
        d   = datetime.strptime(t["txn_date"], "%Y-%m-%d").date()
        amt = -(t["quantity"] * t["price"]) if t["txn_type"] == "BUY" else (t["quantity"] * t["price"])
        cashflows.append((d, amt))

    # Add today's holding values as final inflow
    holdings = db.execute(
        "SELECT ticker, quantity, last_price FROM stored_holdings WHERE user_id=?",
        (user_id,)
    ).fetchall()
    today_val = sum(h["quantity"] * h["last_price"] for h in holdings)
    cashflows.append((date.today(), today_val))

    rate = xirr(cashflows)

    # Compute Nifty XIRR over same period
    nifty_xirr = _nifty_xirr(cashflows[0][0], date.today())

    total_invested = sum(-cf[1] for cf in cashflows if cf[1] < 0)
    total_value    = today_val
    abs_return     = round((total_value - total_invested) / total_invested * 100, 1) if total_invested else 0

    return {
        "xirr":              rate,
        "nifty_xirr":        nifty_xirr,
        "alpha":             round(rate - nifty_xirr, 2) if nifty_xirr else None,
        "total_invested":    round(total_invested, 0),
        "current_value":     round(total_value, 0),
        "absolute_return":   abs_return,
        "transaction_count": len(txns),
        "period_start":      txns[0]["txn_date"],
        "period_end":        str(date.today()),
    }


def _nifty_xirr(start: date, end: date) -> Optional[float]:
    """Compute Nifty 50 XIRR for the same investment period."""
    try:
        return None  # Nifty XIRR benchmark: yfinance dead, skip gracefully
        nf = None
        if nf is None:
            return None
        p0 = float(nf["Close"].iloc[0])
        p1 = float(nf["Close"].iloc[-1])
        years = (end - start).days / 365
        if years < 0.01:
            return None
        cagr = (p1 / p0) ** (1 / years) - 1
        return round(cagr * 100, 2)
    except Exception:
        return None


# ════════════════════════════════════════════════════════════════════════════
# 4. TAX HARVESTING
# ════════════════════════════════════════════════════════════════════════════
LTCG_EXEMPTION  = 125000   # ₹1.25L per FY
LTCG_TAX_RATE   = 0.125    # 12.5%
STCG_TAX_RATE   = 0.20     # 20%

def compute_tax_harvest(user_id: int) -> dict:
    """Compute LTCG / LTCL positions and FY2027 tax plan."""
    from database import get_db
    db   = get_db()

    # Get buy transactions
    txns = db.execute(
        "SELECT * FROM transactions WHERE user_id=? AND txn_type='BUY' ORDER BY txn_date ASC",
        (user_id,)
    ).fetchall()
    holdings = db.execute(
        "SELECT ticker, quantity, last_price FROM stored_holdings WHERE user_id=?",
        (user_id,)
    ).fetchall()

    # Build holding lots with buy dates
    lots = {}
    for t in txns:
        tk = t["ticker"]
        if tk not in lots:
            lots[tk] = []
        lots[tk].append({
            "date":  datetime.strptime(t["txn_date"], "%Y-%m-%d").date(),
            "qty":   t["quantity"],
            "price": t["price"]
        })

    held = {h["ticker"]: h["last_price"] for h in holdings}
    today = date.today()
    fy_end = date(today.year if today.month >= 4 else today.year - 1, 3, 31) + timedelta(days=366)
    if fy_end.month != 3:
        fy_end = date(today.year + 1, 3, 31)

    ltcg_positions = []  # unrealised LTCG (harvest to use exemption)
    ltcl_positions = []  # unrealised LTCL (harvest to offset gains)
    stcg_positions = []  # held < 1 year (don't harvest for tax purposes)

    for tk, lot_list in lots.items():
        ltp = held.get(tk)
        if not ltp:
            continue
        for lot in lot_list:
            days_held = (today - lot["date"]).days
            gain      = (ltp - lot["price"]) * lot["qty"]
            if days_held >= 365:
                entry = {
                    "ticker":   tk,
                    "buy_date": str(lot["date"]),
                    "qty":      lot["qty"],
                    "buy_price":lot["price"],
                    "ltp":      ltp,
                    "gain":     round(gain, 0),
                    "days_held":days_held,
                    "tax_type": "LTCG" if gain > 0 else "LTCL"
                }
                if gain > 0:
                    ltcg_positions.append(entry)
                else:
                    ltcl_positions.append(entry)
            else:
                stcg_positions.append({
                    "ticker":   tk, "buy_date": str(lot["date"]),
                    "qty":      lot["qty"], "gain": round(gain, 0),
                    "days_held":days_held, "days_to_ltcg": 365 - days_held
                })

    # Sort by gain size
    ltcg_positions.sort(key=lambda x: -x["gain"])
    ltcl_positions.sort(key=lambda x: x["gain"])

    total_ltcg = sum(p["gain"] for p in ltcg_positions)
    total_ltcl = abs(sum(p["gain"] for p in ltcl_positions))

    # Tax plan
    net_ltcg_after_offset = max(0, total_ltcg - total_ltcl)
    taxable_ltcg          = max(0, net_ltcg_after_offset - LTCG_EXEMPTION)
    tax_payable           = round(taxable_ltcg * LTCG_TAX_RATE, 0)

    # Optimal harvest plan — harvest enough LTCL to minimise tax
    harvest_ltcl = []
    remaining_gain = total_ltcg - LTCG_EXEMPTION
    for p in ltcl_positions:
        if remaining_gain <= 0:
            break
        harvest_ltcl.append(p)
        remaining_gain -= abs(p["gain"])

    # Future STCG positions approaching 1-year mark
    approaching = sorted(
        [p for p in stcg_positions if p["days_to_ltcg"] <= 45 and p["gain"] < 0],
        key=lambda x: x["days_to_ltcg"]
    )

    return {
        "fy":                  "FY 2026-27",
        "ltcg_positions":      ltcg_positions,
        "ltcl_positions":      ltcl_positions,
        "stcg_positions":      stcg_positions,
        "approaching_ltcg":    approaching,
        "total_ltcg":          round(total_ltcg, 0),
        "total_ltcl":          round(total_ltcl, 0),
        "net_ltcg":            round(net_ltcg_after_offset, 0),
        "taxable_ltcg":        round(taxable_ltcg, 0),
        "tax_payable":         round(tax_payable, 0),
        "tax_saved_if_harvest":round(min(total_ltcl, total_ltcg) * LTCG_TAX_RATE, 0),
        "harvest_ltcl_plan":   harvest_ltcl,
        "ltcg_exemption":      LTCG_EXEMPTION,
        "harvest_deadline":    str(fy_end),
        "plan_summary": (
            f"Total LTCG: ₹{total_ltcg:,.0f} · LTCL to offset: ₹{total_ltcl:,.0f} · "
            f"Net taxable: ₹{taxable_ltcg:,.0f} · Tax payable: ₹{tax_payable:,.0f}"
        )
    }


# ════════════════════════════════════════════════════════════════════════════
# 5. SCREENER.IN SYNC
# ════════════════════════════════════════════════════════════════════════════
async def scrape_screener(ticker: str) -> dict:
    """Scrape key fundamentals from screener.in for one stock."""
    try:
        import re
        url = f"https://www.screener.in/company/{ticker}/consolidated/"
        async with httpx.AsyncClient(timeout=15, follow_redirects=True) as c:
            r = await c.get(url, headers={"User-Agent": "Mozilla/5.0"})
            if r.status_code == 404:
                # try standalone
                r2 = await c.get(
                    f"https://www.screener.in/company/{ticker}/",
                    headers={"User-Agent": "Mozilla/5.0"}
                )
                html = r2.text
            else:
                html = r.text

        # Extract key numbers using regex on the rendered HTML
        def extract(pattern, text, default=None):
            m = re.search(pattern, text)
            if m:
                try:
                    return float(m.group(1).replace(",",""))
                except Exception:
                    return default
            return default

        roce   = extract(r'Return on capital employed.*?(\d+\.?\d*)\s*%', html)
        de     = extract(r'Debt to equity.*?(\d+\.?\d*)', html)
        pe     = extract(r'Stock P/E.*?(\d+\.?\d*)', html)
        pb     = extract(r'Price to Book value.*?(\d+\.?\d*)', html)
        promo  = extract(r'Promoter Holding.*?(\d+\.?\d*)\s*%', html)
        pledge = extract(r'[Pp]ledged.*?(\d+\.?\d*)\s*%', html, 0)

        return {
            "ticker":  ticker,
            "roce":    roce,
            "de":      de,
            "pe":      pe,
            "pb":      pb,
            "promoter_pct":     promo,
            "promoter_pledge":  pledge,
            "source":  "screener.in",
            "scraped_at": datetime.now().isoformat()
        }
    except Exception as e:
        return {"ticker": ticker, "error": str(e)}


async def sync_all_fundamentals(user_id: int) -> dict:
    """Sync fundamentals for all Core 22 + watchlist stocks."""
    from database import get_db
    import asyncio

    tickers = [s["ticker"] for s in CORE22 if not s["is_etf"]]
    db = get_db()
    wl = db.execute("SELECT ticker FROM watchlist WHERE user_id=?", (user_id,)).fetchall()
    tickers += [w["ticker"] for w in wl]
    tickers  = list(set(tickers))

    results = {"updated": [], "failed": [], "total": len(tickers)}
    for tk in tickers:
        data = await scrape_screener(tk)
        if "error" in data:
            results["failed"].append(tk)
            continue
        try:
            db.execute("""
                INSERT INTO stock_fundamentals
                  (ticker, roce, de, pe, pb, promoter_pct, promoter_pledge, source, updated_at)
                VALUES (?,?,?,?,?,?,?,?,datetime('now'))
                ON CONFLICT(ticker) DO UPDATE SET
                  roce=excluded.roce, de=excluded.de, pe=excluded.pe, pb=excluded.pb,
                  promoter_pct=excluded.promoter_pct, promoter_pledge=excluded.promoter_pledge,
                  source=excluded.source, updated_at=excluded.updated_at
            """, (tk, data.get("roce"), data.get("de"), data.get("pe"),
                  data.get("pb"), data.get("promoter_pct"), data.get("promoter_pledge"), "screener.in"))
            results["updated"].append(tk)
        except Exception:
            results["failed"].append(tk)
        await asyncio.sleep(2)  # polite delay

    db.commit()
    return results


# ════════════════════════════════════════════════════════════════════════════
# FASTAPI ROUTES
# ════════════════════════════════════════════════════════════════════════════
def register_routes(app):
    from fastapi import Depends, HTTPException, UploadFile, File
    from auth import get_current_user

    # ── TELEGRAM ──────────────────────────────────────────────────────────
    @app.post("/alerts/test")
    async def test_telegram(current_user=Depends(get_current_user)):
        tok, cid = await get_user_telegram(current_user["id"])
        if not tok:
            raise HTTPException(400, "Telegram not configured. Set token and chat ID in Settings.")
        msg = fmt_alert("Test alert ✓",
            f"Hello <b>{current_user.get('name','Arnab')}</b>! Your ASOS alerts are live.\n\n"
            f"You will receive:\n"
            f"• VIX spike alerts (above 18 or 20)\n"
            f"• Ladder entry level hits\n"
            f"• SIP reminders on the 5th\n"
            f"• RSI crossing 35 or 65 for Core 22\n"
            f"• Daily 9:15 AM market open summary", "🧪")
        ok = await send_telegram(tok, cid, msg)
        return {"sent": ok, "message": "Test message sent!" if ok else "Send failed — check token/chat ID"}

    @app.post("/alerts/send")
    async def send_alert(data: dict, current_user=Depends(get_current_user)):
        """Manual alert send (for testing specific alerts)."""
        tok, cid = await get_user_telegram(current_user["id"])
        if not tok:
            raise HTTPException(400, "Telegram not configured.")
        ok = await send_telegram(tok, cid, data.get("message",""))
        return {"sent": ok}

    @app.get("/alerts/history")
    async def alert_history(current_user=Depends(get_current_user)):
        from database import get_db
        rows = get_db().execute(
            "SELECT * FROM alert_log WHERE user_id=? ORDER BY sent_at DESC LIMIT 50",
            (current_user["id"],)
        ).fetchall()
        return {"alerts": [dict(r) for r in rows]}

    # ── SIP OPTIMIZER ─────────────────────────────────────────────────────
    @app.get("/portfolio/sip-optimize")
    async def sip_optimize(current_user=Depends(get_current_user)):
        from database import get_db
        from kite_data_patch import compute_ivp_ivr
        uid = current_user["id"]
        db  = get_db()
        row = db.execute("SELECT * FROM user_settings WHERE user_id=?", (uid,)).fetchone()
        sip_total  = row["sip_amount"]        if row else 100000
        withdrawal = row["withdrawal_amount"] if row else 0
        pending    = row["pending_credit"]    if row else 0

        # Get corpus
        held = db.execute("SELECT SUM(quantity*last_price) as val FROM stored_holdings WHERE user_id=?",
                          (uid,)).fetchone()
        corpus    = (held["val"] or 0)
        effective = corpus - withdrawal + pending

        try:
            iv  = compute_ivp_ivr()
            vix = iv["vix"]
        except Exception:
            vix = 14.0

        allocations = await compute_sip_allocation(uid, sip_total, effective, vix)
        total_planned = sum(a.get("sip_amount", 0) for a in allocations)

        # Determine SIP mode from VIX
        sip_mode = ("FULL SIP" if 13 <= vix <= 16 else
                    "75% SIP" if vix < 13 else
                    "50% SIP" if vix <= 20 else
                    "PAUSE" if vix <= 25 else "DOUBLE SIP")

        return {
            "sip_total":     sip_total,
            "effective_corpus": round(effective, 0),
            "vix":           vix,
            "sip_mode":      sip_mode,
            "allocations":   allocations,
            "total_planned": total_planned,
            "month":         datetime.now().strftime("%B %Y"),
            "sip_day":       row["sip_date"] if row else 5,
            "note": (f"VIX {vix:.1f} → {sip_mode}. "
                     f"Deploy ₹{total_planned:,.0f} on the "
                     f"{row['sip_date'] if row else 5}th."),
        }

    # ── TRANSACTIONS ──────────────────────────────────────────────────────
    @app.post("/portfolio/transactions/upload")
    async def upload_transactions(file: UploadFile = File(...),
                                   current_user=Depends(get_current_user)):
        """
        Upload trade history CSV.
        Format: Date,Ticker,Type,Qty,Price
        Example: 2024-06-05,CGPOWER,BUY,50,620.50
        """
        content = await file.read()
        text    = content.decode("utf-8-sig")
        from database import get_db
        db = get_db(); uid = current_user["id"]; count = 0
        reader = csv.DictReader(io.StringIO(text))
        for row in reader:
            r = {k.strip().lower(): v.strip() for k, v in row.items()}
            try:
                db.execute("""
                    INSERT OR IGNORE INTO transactions
                      (user_id,ticker,txn_date,txn_type,quantity,price,notes)
                    VALUES (?,?,?,?,?,?,?)
                """, (uid, r.get("ticker","").upper().replace(" ",""),
                      r.get("date",""), r.get("type","BUY").upper(),
                      float(r.get("qty","0").replace(",","")),
                      float(r.get("price","0").replace(",","")),
                      r.get("notes","")))
                count += 1
            except Exception:
                continue
        db.commit()
        return {"message": f"✓ {count} transactions imported", "count": count}

    @app.get("/portfolio/transactions")
    async def get_transactions(current_user=Depends(get_current_user)):
        from database import get_db
        rows = get_db().execute(
            "SELECT * FROM transactions WHERE user_id=? ORDER BY txn_date DESC LIMIT 200",
            (current_user["id"],)
        ).fetchall()
        return {"transactions": [dict(r) for r in rows]}

    @app.post("/portfolio/transactions")
    async def add_transaction(data: dict, current_user=Depends(get_current_user)):
        from database import get_db
        db = get_db()
        db.execute("""
            INSERT INTO transactions (user_id,ticker,txn_date,txn_type,quantity,price,notes)
            VALUES (?,?,?,?,?,?,?)
        """, (current_user["id"], data["ticker"].upper(),
              data["date"], data["type"].upper(),
              float(data["quantity"]), float(data["price"]),
              data.get("notes","")))
        db.commit()
        return {"message": f"✓ {data['ticker']} {data['type']} logged"}

    # ── XIRR ──────────────────────────────────────────────────────────────
    @app.get("/portfolio/xirr")
    async def portfolio_xirr(current_user=Depends(get_current_user)):
        result = compute_xirr_for_portfolio(current_user["id"])
        return result

    # ── TAX HARVESTING ────────────────────────────────────────────────────
    @app.get("/portfolio/tax-harvest")
    async def tax_harvest(current_user=Depends(get_current_user)):
        return compute_tax_harvest(current_user["id"])

    # ── SCREENER SYNC ─────────────────────────────────────────────────────
    @app.post("/portfolio/screener-sync")
    async def screener_sync(current_user=Depends(get_current_user)):
        """Trigger immediate Screener.in sync for all stocks."""
        result = await sync_all_fundamentals(current_user["id"])
        return result

    @app.get("/portfolio/fundamentals")
    async def get_fundamentals(current_user=Depends(get_current_user)):
        from database import get_db
        rows = get_db().execute(
            "SELECT * FROM stock_fundamentals ORDER BY ticker"
        ).fetchall()
        return {"fundamentals": [dict(r) for r in rows]}

    @app.get("/portfolio/fundamentals/{ticker}")
    async def get_fundamentals_one(ticker: str,
                                    current_user=Depends(get_current_user)):
        from database import get_db
        row = get_db().execute(
            "SELECT * FROM stock_fundamentals WHERE ticker=?",
            (ticker.upper(),)
        ).fetchone()
        if not row:
            # Try scraping live
            data = await scrape_screener(ticker.upper())
            return {"ticker": ticker.upper(), "data": data, "source": "live_scrape"}
        return {"ticker": ticker.upper(), "data": dict(row), "source": "cached"}


# ════════════════════════════════════════════════════════════════════════════
# BACKGROUND SCHEDULER
# ════════════════════════════════════════════════════════════════════════════
def start_scheduler(app_instance):
    """
    Start APScheduler background jobs.
    Call from main.py lifespan after init_schema().
    """
    try:
        from apscheduler.schedulers.asyncio import AsyncIOScheduler
        import pytz
        IST = pytz.timezone("Asia/Kolkata")
        sched = AsyncIOScheduler(timezone=IST)

        # ── Daily market open summary (9:16 AM IST, weekdays) ────────────
        sched.add_job(_job_market_open,    'cron', day_of_week='mon-fri',
                       hour=9, minute=16)

        # ── VIX check every 30 min during market hours ───────────────────
        sched.add_job(_job_vix_check,      'cron', day_of_week='mon-fri',
                       hour='9-15', minute='0,30')

        # ── RSI alerts (daily, EOD) ───────────────────────────────────────
        sched.add_job(_job_rsi_alerts,     'cron', day_of_week='mon-fri',
                       hour=16, minute=0)

        # ── SIP reminder (5th of each month, 8 AM) ───────────────────────
        sched.add_job(_job_sip_reminder,   'cron', day=5, hour=8)

        # ── Weekly Screener sync (Sunday 8 PM) ───────────────────────────
        sched.add_job(_job_screener_sync,  'cron', day_of_week='sun', hour=20)

        # ── Weekly portfolio digest (Sunday 7 PM) ────────────────────────
        sched.add_job(_job_weekly_digest,  'cron', day_of_week='sun', hour=19)

        sched.start()
        print("✅  Background scheduler started (market alerts, SIP reminder, Screener sync)")
        return sched
    except ImportError:
        print("⚠  APScheduler not installed. Run: pip install apscheduler pytz")
    except Exception as e:
        print(f"⚠  Scheduler failed: {e}")


async def _job_market_open():
    """9:16 AM — send market open summary to all users."""
    try:
        from database import get_db
        from kite_data_patch import compute_ivp_ivr
        iv   = compute_ivp_ivr()
        vix  = iv["vix"]; ivp = iv["ivp"]
        sip_note = ("⚡ DEPLOY SIP — VIX normal" if 13 <= vix <= 16 else
                    "⚠ VIX elevated — deploy 50% SIP only" if vix <= 20 else
                    "🔴 HIGH VIX — PAUSE SIP, buy tranches on dips" if vix <= 25 else
                    "🚀 PANIC VIX — DOUBLE SIP, deploy reserves")
        msg = fmt_alert("Market Open",
            f"<b>India VIX:</b> {vix}\n<b>IVP:</b> {ivp}%\n\n{sip_note}",
            "🌅")
        db = get_db()
        users = db.execute("SELECT user_id, telegram_token, telegram_chat_id, alerts_enabled FROM user_settings").fetchall()
        for u in users:
            if u["alerts_enabled"] and u["telegram_token"] and u["telegram_chat_id"]:
                await send_telegram(u["telegram_token"], u["telegram_chat_id"], msg)
    except Exception as e:
        print(f"Market open job error: {e}")


async def _job_vix_check():
    """Every 30 min — alert if VIX crosses key levels."""
    try:
        from database import get_db
        from kite_data_patch import compute_ivp_ivr
        iv  = compute_ivp_ivr()
        vix = iv["vix"]
        db  = get_db()
        users = db.execute(
            "SELECT user_id, telegram_token, telegram_chat_id, alert_vix_high, alerts_enabled FROM user_settings"
        ).fetchall()
        for u in users:
            if not (u["alerts_enabled"] and u["telegram_token"] and u["telegram_chat_id"]):
                continue
            threshold = float(u["alert_vix_high"] or 20)
            if vix >= threshold:
                msg = fmt_alert(f"VIX Alert — {vix}",
                    f"India VIX crossed <b>{threshold}</b> → now at <b>{vix}</b>\n\n"
                    f"• Pause regular SIP\n• Hold cash for dip buys\n• Check ladder levels", "⚠️")
                await send_telegram(u["telegram_token"], u["telegram_chat_id"], msg)
                # Log alert
                db.execute(
                    "INSERT INTO alert_log (user_id,alert_type,message) VALUES (?,?,?)",
                    (u["user_id"], "VIX_HIGH", f"VIX={vix}")
                )
                db.commit()
    except Exception as e:
        print(f"VIX check error: {e}")


async def _job_rsi_alerts():
    """EOD — check RSI for all Core 22 stocks and alert crossings."""
    try:
        from database import get_db
        from kite_data_patch import _kite, _hist_closes, _rsi as _ks_rsi
        db    = get_db()
        users = db.execute(
            "SELECT user_id, telegram_token, telegram_chat_id, alerts_enabled FROM user_settings"
        ).fetchall()
        alerts = []
        k = _kite(None)
        for s in CORE22:
            if s["is_etf"]: continue
            tk = s["ticker"]
            try:
                closes = _hist_closes(k, tk, 90) if k else []
                if not closes or len(closes) < 15: continue
                rsi  = _ks_rsi(closes) or 50
                prev = _ks_rsi(closes[:-1]) or 50
                if rsi < 35 and prev >= 35:
                    alerts.append({"ticker":tk,"rsi":rsi,"type":"OVERSOLD",
                        "msg":f"<b>{tk}</b> RSI crossed <b>below 35</b> ({rsi:.1f}) → STRONG ADD signal"})
                elif rsi > 65 and prev <= 65:
                    alerts.append({"ticker":tk,"rsi":rsi,"type":"OVERBOUGHT",
                        "msg":f"<b>{tk}</b> RSI crossed <b>above 65</b> ({rsi:.1f}) → Consider trimming if overweight"})
            except Exception:
                continue

        if not alerts: return
        msg = fmt_alert("RSI Alerts — EOD",
            "\n".join(a["msg"] for a in alerts), "📊")
        for u in users:
            if u["alerts_enabled"] and u["telegram_token"] and u["telegram_chat_id"]:
                await send_telegram(u["telegram_token"], u["telegram_chat_id"], msg)
                for a in alerts:
                    db.execute(
                        "INSERT INTO alert_log (user_id,alert_type,ticker,message) VALUES (?,?,?,?)",
                        (u["user_id"], f"RSI_{a['type']}", a["ticker"], a["msg"])
                    )
                db.commit()
    except Exception as e:
        print(f"RSI alert error: {e}")


async def _job_sip_reminder():
    """5th of month — SIP reminder with optimised allocation."""
    try:
        from database import get_db
        db    = get_db()
        users = db.execute(
            "SELECT user_id, telegram_token, telegram_chat_id, sip_amount, sip_date, alerts_enabled FROM user_settings"
        ).fetchall()
        for u in users:
            if not (u["alerts_enabled"] and u["telegram_token"] and u["telegram_chat_id"]):
                continue
            today = datetime.now().day
            sip_day = int(u["sip_date"] or 5)
            if today != sip_day:
                continue
            sip = int(u["sip_amount"] or 100000)
            msg = fmt_alert("SIP Day Reminder",
                f"Today is your SIP day! Deploy <b>₹{sip:,}</b> across Core 22.\n\n"
                f"Open the ASOS platform → SIP Optimizer → get today's exact allocation.\n\n"
                f"Check VIX before deploying — if VIX &gt; 18, reduce SIP by 50%.", "💰")
            await send_telegram(u["telegram_token"], u["telegram_chat_id"], msg)
    except Exception as e:
        print(f"SIP reminder error: {e}")


async def _job_screener_sync():
    """Sunday 8 PM — sync fundamentals from Screener.in."""
    try:
        from database import get_db
        db    = get_db()
        users = db.execute("SELECT DISTINCT user_id FROM user_settings").fetchall()
        for u in users:
            result = await sync_all_fundamentals(u["user_id"])
            print(f"Screener sync user {u['user_id']}: {result['updated']} updated, {result['failed']} failed")
    except Exception as e:
        print(f"Screener sync error: {e}")


async def _job_weekly_digest():
    """Sunday 7 PM — weekly portfolio digest."""
    try:
        from database import get_db
        from kite_data_patch import compute_ivp_ivr
        iv   = compute_ivp_ivr()
        vix  = iv["vix"]; ivp = iv["ivp"]
        db   = get_db()
        users = db.execute(
            "SELECT user_id, telegram_token, telegram_chat_id, sip_amount, alerts_enabled FROM user_settings"
        ).fetchall()
        for u in users:
            if not (u["alerts_enabled"] and u["telegram_token"] and u["telegram_chat_id"]):
                continue
            held = db.execute(
                "SELECT COUNT(*) as n, SUM(quantity*last_price) as val FROM stored_holdings WHERE user_id=?",
                (u["user_id"],)
            ).fetchone()
            n   = held["n"] or 0
            val = round(held["val"] or 0, 0)
            msg = fmt_alert("Weekly Portfolio Digest",
                f"<b>Portfolio:</b> {n} holdings · ₹{val/1e5:.2f}L\n"
                f"<b>VIX:</b> {vix} · <b>IVP:</b> {ivp}%\n\n"
                f"Next SIP: ₹{int(u['sip_amount'] or 100000):,} — see SIP Optimizer for this month's allocation.\n"
                f"Run Buy/Sell Radar for RSI + entry signals.", "📋")
            await send_telegram(u["telegram_token"], u["telegram_chat_id"], msg)
    except Exception as e:
        print(f"Weekly digest error: {e}")
