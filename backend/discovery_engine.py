"""
discovery_engine.py — ATHENA Discovery Engine (R-11)
Screener query -> shortlist -> deep analysis -> ranked gems with COMPUTED
entry ladder, target, holding period and exit rules.

Replaces the static Watchlist Scout.

Routes:
  GET  /discovery/presets                 -> built-in screener queries
  POST /discovery/run     {preset|query}  -> full funnel, returns ranked gems
  GET  /discovery/results                 -> last cached run
  POST /discovery/analyze {tickers:[...]} -> analyse specific tickers
  POST /discovery/bulk-deals              -> upload NSE bulk deal CSV (weekly)
  GET  /discovery/bulk-deals              -> stored deals

main.py:
  try: import discovery_engine
  except ImportError: discovery_engine = None
  # lifespan: if discovery_engine: discovery_engine.init_schema()
  # after app: if discovery_engine: discovery_engine.register_routes(app)
"""
import json, math, re, time, csv, io
from datetime import datetime, date, timedelta

SCREENER_QUERY_URL = "https://www.screener.in/screen/raw/"
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/120.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml",
    "Accept-Language": "en-US,en;q=0.9",
}



def _parse_deal_date(s: str) -> str:
    """Normalise NSE (20-JUL-2026) and BSE (01-07-2026) dates to ISO."""
    s = (s or "").strip()
    if not s:
        return date.today().isoformat()
    for fmt in ("%d-%b-%Y", "%d-%B-%Y", "%d-%m-%Y", "%Y-%m-%d", "%d/%m/%Y", "%m/%d/%Y"):
        try:
            return datetime.strptime(s, fmt).date().isoformat()
        except ValueError:
            continue
    return s  # store raw if unrecognised

# ── Preset queries (user-editable; these are the strategy, not hardcoded stocks)
PRESETS = {
    "quality_compounders": {
        "name": "Quality Compounders",
        "desc": "High ROCE, low debt, growing profits, decent size",
        "query": "Return on capital employed > 20 AND Debt to equity < 0.5 AND "
                 "Profit growth 3Years > 15 AND Market Capitalization > 1000",
    },
    "hidden_gems": {
        "name": "Hidden Gems (small/mid)",
        "desc": "Smaller companies with excellent capital efficiency and growth",
        "query": "Market Capitalization > 500 AND Market Capitalization < 15000 AND "
                 "Return on capital employed > 25 AND Sales growth 3Years > 20 AND "
                 "Promoter holding > 50",
    },
    "value_quality": {
        "name": "Value + Quality",
        "desc": "Good businesses trading below industry multiples (PD-3)",
        "query": "Price to Earning < Industry PE AND Return on capital employed > 18 AND "
                 "Debt to equity < 0.7 AND Profit growth 3Years > 10",
    },
    "turnaround": {
        "name": "Turnaround Candidates",
        "desc": "Recent profit inflection after a weak 3-year period",
        "query": "Profit growth > 30 AND Profit growth 3Years < 0 AND Debt to equity < 1 AND "
                 "Market Capitalization > 500",
    },
    "momentum_quality": {
        "name": "Momentum + Quality",
        "desc": "Compounders showing operating momentum",
        "query": "Return on capital employed > 20 AND Sales growth 3Years > 15 AND "
                 "Profit growth > 20 AND Market Capitalization > 1000",
    },
    "dividend_compounders": {
        "name": "Dividend Compounders",
        "desc": "Income plus growth",
        "query": "Dividend yield > 2 AND Return on capital employed > 18 AND "
                 "Profit growth 3Years > 10 AND Debt to equity < 0.6",
    },
}


# ── schema ────────────────────────────────────────────────────────────────
def init_schema():
    from database import get_db
    db = get_db()
    db.executescript("""
    CREATE TABLE IF NOT EXISTS discovery_runs (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER, run_at TEXT, preset TEXT, query TEXT,
        matched INTEGER, analysed INTEGER, results_json TEXT
    );
    CREATE TABLE IF NOT EXISTS bulk_deals (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER, deal_date TEXT, ticker TEXT, client TEXT,
        buy_sell TEXT, quantity REAL, price REAL, uploaded_at TEXT,
        UNIQUE(user_id, deal_date, ticker, client, buy_sell)
    );
    CREATE TABLE IF NOT EXISTS saved_queries (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER, name TEXT, query TEXT, created_at TEXT
    );
    """)
    db.commit()


# ── STAGE 1+2: run a screener query (Screener does the heavy filtering) ────
def run_screener_query(query: str, pages: int = 2) -> dict:
    """Returns {tickers:[...], count, error}. Screener applies filters server-side."""
    import httpx
    tickers, err = [], None
    try:
        with httpx.Client(timeout=25, follow_redirects=True, headers=HEADERS) as c:
            for page in range(1, pages + 1):
                r = c.get(SCREENER_QUERY_URL, params={"query": query, "page": page})
                if r.status_code != 200:
                    err = f"HTTP {r.status_code}"
                    break
                # rows link to /company/TICKER/
                found = re.findall(r'/company/([A-Z0-9&\-]+)/', r.text)
                page_t = []
                for t in found:
                    if t not in tickers and t not in page_t:
                        page_t.append(t)
                if not page_t:
                    break
                tickers.extend(page_t)
                time.sleep(1.0)
    except Exception as e:
        err = str(e)[:120]
    return {"tickers": tickers, "count": len(tickers), "error": err}


# ── STAGE 4: live technicals from Kite ────────────────────────────────────
def _technicals(ticker: str, user_id=None) -> dict:
    try:
        from kite_data_patch import _kite, _hist_closes, _rsi
        k = _kite(user_id)
        if not k:
            return {}
        closes = _hist_closes(k, ticker, 400)
        if len(closes) < 60:
            return {}
        price = closes[-1]
        high52, low52 = max(closes[-250:]), min(closes[-250:])
        dma50 = sum(closes[-50:]) / 50
        dma200 = sum(closes[-200:]) / 200 if len(closes) >= 200 else dma50
        rsi = _rsi(closes) or 50
        # base detection: last 40 sessions range tightness
        recent = closes[-40:]
        base_tight = (max(recent) - min(recent)) / price * 100 if price else 99
        return {
            "price": round(price, 1),
            "high52": round(high52, 1), "low52": round(low52, 1),
            "pct_from_high": round((price - high52) / high52 * 100, 1),
            "pct_from_low": round((price - low52) / low52 * 100, 1),
            "dma50": round(dma50, 1), "dma200": round(dma200, 1),
            "above_dma50": price > dma50, "above_dma200": price > dma200,
            "pct_vs_dma200": round((price - dma200) / dma200 * 100, 1),
            "rsi": rsi, "base_tightness_pct": round(base_tight, 1),
            "in_base": base_tight < 12,
        }
    except Exception:
        return {}


# ── STAGE 5: composite score ──────────────────────────────────────────────
def composite_score(fund: dict, tech: dict, smart_money: bool) -> dict:
    """quality 40% + valuation 25% + momentum 20% + smart money 15%"""
    from athena_dashboard import quality_score
    q = quality_score(fund)
    qs = q["score"] if q["score"] is not None else 0

    # valuation component (PD-3)
    pe = fund.get("pe")
    if pe is None:      val = 50
    elif pe <= 15:      val = 100
    elif pe <= 25:      val = 85
    elif pe <= 40:      val = 60
    elif pe <= 60:      val = 35
    elif pe <= 90:      val = 15
    else:               val = 5

    # momentum component
    mom = 50
    if tech:
        mom = 0
        mom += 25 if tech.get("above_dma200") else 0
        mom += 15 if tech.get("above_dma50") else 0
        r = tech.get("rsi", 50)
        mom += 20 if 45 <= r <= 65 else (10 if 35 <= r < 45 else 0)
        mom += 20 if tech.get("in_base") else 0
        pfl = tech.get("pct_from_low", 100)
        mom += 20 if pfl < 40 else (10 if pfl < 80 else 0)
        mom = min(100, mom)

    sm = 100 if smart_money else 40
    total = round(qs * 0.40 + val * 0.25 + mom * 0.20 + sm * 0.15, 1)
    grade = ("STRONG BUY" if total >= 78 else "BUY" if total >= 66 else
             "WATCH" if total >= 52 else "PASS")
    return {"composite": total, "grade": grade,
            "components": {"quality": qs, "valuation": val, "momentum": mom, "smart_money": sm},
            "quality_detail": q}


# ── ENTRY / TARGET / EXIT — the "when to enter, for how long, what target" ─
def entry_plan(tech: dict, fund: dict, score: dict) -> dict:
    """Laddered A/B/C/D entries, target, horizon, and exit rules. All computed."""
    if not tech or not tech.get("price"):
        return {"error": "no price data"}
    px = tech["price"]
    pfl = tech.get("pct_from_low", 0)
    pfh = tech.get("pct_from_high", 0)
    rsi = tech.get("rsi", 50)
    above200 = tech.get("above_dma200")
    comp = score["composite"]

    # --- Entry stance -----------------------------------------------------
    if pfh > -8 and rsi > 68:
        stance = "WAIT"
        why = f"Extended: {abs(pfh):.0f}% off 52wk high, RSI {rsi:.0f}. Wait for pullback."
        a_pct = 0.0
    elif tech.get("in_base") and above200:
        stance = "ENTER ON BREAKOUT"
        why = f"Basing tight ({tech['base_tightness_pct']}% range) above 200DMA - buy the breakout."
        a_pct = 0.25
    elif pfl < 25 and comp >= 60:
        stance = "ACCUMULATE NOW"
        why = f"Near 52wk low (+{pfl:.0f}%) with composite {comp}. Start the ladder."
        a_pct = 0.25
    elif above200 and 40 <= rsi <= 62:
        stance = "ENTER"
        why = f"Constructive: above 200DMA, RSI {rsi:.0f} neutral."
        a_pct = 0.25
    else:
        stance = "WAIT"
        why = f"No clean setup: RSI {rsi:.0f}, {'above' if above200 else 'below'} 200DMA."
        a_pct = 0.0

    # --- Laddered levels (A/B/C/D, 25% each) ------------------------------
    base = px if stance != "WAIT" else px * 0.92
    ladder = [
        {"level": "A", "price": round(base, 1),        "alloc_pct": 25,
         "note": "Initial tranche" if a_pct else "Only after setup confirms"},
        {"level": "B", "price": round(base * 0.92, 1), "alloc_pct": 25, "note": "-8% add"},
        {"level": "C", "price": round(base * 0.85, 1), "alloc_pct": 25, "note": "-15% add"},
        {"level": "D", "price": round(base * 0.78, 1), "alloc_pct": 25, "note": "-22% final tranche"},
    ]
    avg_cost = round(sum(l["price"] for l in ladder) / 4, 1)

    # --- Target & horizon (quality drives expected CAGR) ------------------
    q = score["components"]["quality"]
    if   q >= 80: cagr, horizon = 22, 5
    elif q >= 68: cagr, horizon = 18, 4
    elif q >= 55: cagr, horizon = 15, 3
    else:         cagr, horizon = 12, 3
    # valuation haircut: expensive entry lowers forward return
    pe = fund.get("pe")
    if pe and pe > 60: cagr -= 4
    elif pe and pe > 40: cagr -= 2
    cagr = max(8, cagr)
    target = round(avg_cost * ((1 + cagr / 100) ** horizon), 1)
    tgt_1y = round(avg_cost * (1 + cagr / 100), 1)

    # --- Exit rules -------------------------------------------------------
    stop = round(ladder[3]["price"] * 0.93, 1)
    exits = [
        {"type": "THESIS BREAK", "priority": 1,
         "rule": "ROCE falls below 15% OR profit declines 2 consecutive quarters OR promoter pledges >10%",
         "action": "Exit fully - the reason you bought is gone"},
        {"type": "TECHNICAL STOP", "priority": 2,
         "rule": f"Weekly close below Rs {stop} (below D-level) or below 200DMA on volume",
         "action": "Exit - capital protection"},
        {"type": "VALUATION TRIM", "priority": 3,
         "rule": f"PE runs above {round((pe or 30) * 2)} (2x current) or price above Rs {target}",
         "action": "Trim 25-50%, let the rest run"},
        {"type": "BETTER OPPORTUNITY", "priority": 4,
         "rule": "A challenger scores 15+ points higher and you need the slot",
         "action": "Swap - capital is finite"},
    ]

    return {
        "stance": stance, "why": why,
        "ladder": ladder, "avg_cost_if_full": avg_cost,
        "expected_cagr": cagr, "horizon_years": horizon,
        "target_price": target, "target_1y": tgt_1y,
        "upside_pct": round((target - px) / px * 100, 1),
        "stop_loss": stop,
        "max_risk_pct": round((stop - avg_cost) / avg_cost * 100, 1),
        "exit_rules": exits,
    }


# ── Smart money check (from uploaded bulk deals) ──────────────────────────
def _has_smart_money(db, uid, ticker, days=45) -> dict:
    cutoff = (date.today() - timedelta(days=days)).isoformat()
    rows = db.execute("""SELECT * FROM bulk_deals WHERE user_id=? AND ticker=? AND deal_date>=?
                         ORDER BY deal_date DESC""", (uid, ticker.upper(), cutoff)).fetchall()
    if not rows:
        return {"found": False, "deals": []}
    deals = [dict(r) for r in rows]
    buys = [d for d in deals if (d.get("buy_sell") or "").upper().startswith("B")]
    return {"found": len(buys) > 0, "buy_count": len(buys), "deals": deals[:5]}


# ── Full funnel ───────────────────────────────────────────────────────────
def analyse_tickers(tickers: list, uid, db, max_analyse: int = 25) -> list:
    import screener_fetch
    screener_fetch.init_schema()
    out = []
    for tk in tickers[:max_analyse]:
        # MASTER SOURCE: cached fundamentals (from your Screener uploads) come first.
        # Only scrape live if we have nothing cached for this ticker.
        fund = screener_fetch.get_cached(tk)
        has_cache = fund and fund.get("fetch_ok") and fund.get("roce") is not None
        if not has_cache:
            scraped = screener_fetch.fetch_one(tk)
            if scraped and scraped.get("fetch_ok"):
                try:
                    screener_fetch._store(scraped)
                except Exception:
                    pass
                fund = scraped
            time.sleep(1.2)
        tech = _technicals(tk, uid)
        sm = _has_smart_money(db, uid, tk)
        score = composite_score(fund, tech, sm["found"])
        plan = entry_plan(tech, fund, score)
        out.append({
            "ticker": tk,
            "composite": score["composite"], "grade": score["grade"],
            "components": score["components"],
            "quality_flags": score["quality_detail"].get("flags", []),
            "fundamentals": {k: fund.get(k) for k in
                             ("roce", "roe", "de", "pe", "promoter_pct",
                              "sales_growth_3y", "profit_growth_3y", "market_cap")},
            "technicals": tech,
            "smart_money": sm,
            "plan": plan,
        })
    out.sort(key=lambda x: x["composite"], reverse=True)
    return out




# ══════════════════════════════════════════════════════════════════════════
# SCREENER EXPORT UPLOAD — you run the query on screener.in, export, upload here
# Format (Screener "Export to Excel"):
#   S.No. | Name | CMP Rs. | P/E | Mar Cap Rs.Cr. | Div Yld % | NP Qtr | Qtr Profit Var % |
#   Sales Qtr | Qtr Sales Var % | ROCE % | ROE % | Debt / Eq | 3Yrs return % |
#   1Yr return % | Change in Prom Hold % | Sales Var 3Yrs %
# ══════════════════════════════════════════════════════════════════════════
def _norm_header(h):
    if h is None: return ""
    return str(h).replace("\xa0", " ").strip().lower()

COLMAP = {
    "name": "name",
    "cmp rs.": "cmp", "cmp": "cmp",
    "p/e": "pe", "pe": "pe",
    "mar cap rs.cr.": "market_cap", "mar cap": "market_cap",
    "div yld %": "dividend_yield", "div yld": "dividend_yield",
    "np qtr rs.cr.": "np_qtr",
    "qtr profit var %": "profit_var_qtr", "qtr profit var": "profit_var_qtr",
    "sales qtr rs.cr.": "sales_qtr",
    "qtr sales var %": "sales_var_qtr", "qtr sales var": "sales_var_qtr",
    "roce %": "roce", "roce": "roce",
    "roe %": "roe", "roe": "roe",
    "debt / eq": "de", "debt/eq": "de", "debt / equity": "de",
    "3yrs return %": "ret_3y", "1yr return %": "ret_1y",
    "change in prom hold %": "prom_change",
    "sales var 3yrs %": "sales_growth_3y",
    "profit var 3yrs %": "profit_growth_3y",
    "promoter holding %": "promoter_pct", "prom hold %": "promoter_pct",
}


def parse_screener_export(raw: bytes, filename: str = "") -> list:
    """Parse Screener xlsx/csv export -> list of dicts with normalised keys."""
    rows = []
    name_l = (filename or "").lower()
    if name_l.endswith((".xlsx", ".xlsm")):
        try:
            import openpyxl, io as _io
            wb = openpyxl.load_workbook(_io.BytesIO(raw), data_only=True)
            ws = wb.worksheets[0]
            data = list(ws.iter_rows(values_only=True))
        except ImportError:
            raise RuntimeError("openpyxl not installed. Run: pip install openpyxl")
    else:
        text = raw.decode("utf-8-sig", errors="ignore")
        data = [r for r in csv.reader(io.StringIO(text))]

    if not data:
        return []
    # find header row (the one containing 'Name')
    hidx = 0
    for i, r in enumerate(data[:10]):
        vals = [_norm_header(x) for x in (r or [])]
        if "name" in vals:
            hidx = i
            break
    header = [_norm_header(x) for x in data[hidx]]
    keys = [COLMAP.get(h, h) for h in header]

    for r in data[hidx + 1:]:
        if not r or all(x is None or str(x).strip() == "" for x in r):
            continue
        rec = {}
        for k, v in zip(keys, r):
            if not k or k in ("s.no.", "s.no"):
                continue
            rec[k] = v
        if not rec.get("name"):
            continue
        # numeric coercion
        for nk in ("cmp", "pe", "market_cap", "dividend_yield", "roce", "roe", "de",
                   "sales_growth_3y", "profit_growth_3y", "profit_var_qtr",
                   "sales_var_qtr", "ret_1y", "ret_3y", "prom_change", "promoter_pct"):
            if nk in rec and rec[nk] is not None:
                try:
                    rec[nk] = float(str(rec[nk]).replace(",", "").replace("%", "").strip())
                except (ValueError, AttributeError):
                    rec[nk] = None
        rows.append(rec)
    return rows


_instr_cache = {"at": 0, "list": []}


def _kite_instruments(user_id=None):
    """NSE equity instruments (tradingsymbol + company name) for name->ticker matching."""
    if time.time() - _instr_cache["at"] < 86400 and _instr_cache["list"]:
        return _instr_cache["list"]
    try:
        from kite_data_patch import _kite
        k = _kite(user_id)
        if not k:
            return []
        insts = [i for i in k.instruments("NSE") if i.get("instrument_type") == "EQ"]
        _instr_cache["at"] = time.time()
        _instr_cache["list"] = insts
        return insts
    except Exception:
        return _instr_cache["list"]


def _clean(s):
    s = (s or "").upper()
    for junk in (" LTD", " LIMITED", " INDIA", ".", "&", "-", "'", "(", ")", ","):
        s = s.replace(junk, "")
    return "".join(s.split())


def match_ticker(company_name: str, user_id=None) -> dict:
    """Map a Screener display name to an NSE tradingsymbol."""
    insts = _kite_instruments(user_id)
    if not insts:
        return {"ticker": None, "confidence": 0, "reason": "Kite instruments unavailable"}
    target = _clean(company_name)
    if not target:
        return {"ticker": None, "confidence": 0, "reason": "empty name"}

    best, best_score = None, 0
    for i in insts:
        ts = i.get("tradingsymbol", "")
        nm = _clean(i.get("name", ""))
        if not nm:
            continue
        if nm == target:
            return {"ticker": ts, "confidence": 100, "matched_name": i.get("name")}
        # prefix / containment scoring (Screener truncates names e.g. "Waaree Renewab.")
        if target and (nm.startswith(target) or target.startswith(nm)):
            score = 90 - abs(len(nm) - len(target))
        elif target in nm or nm in target:
            score = 75 - abs(len(nm) - len(target))
        elif _clean(ts) == target:
            score = 85
        else:
            continue
        if score > best_score:
            best, best_score = i, score
    if best and best_score >= 60:
        return {"ticker": best["tradingsymbol"], "confidence": best_score,
                "matched_name": best.get("name")}
    return {"ticker": None, "confidence": 0, "reason": "no match"}


def _persist_upload_fundamentals(tk, fund, name=""):
    """Write uploaded Screener fundamentals into the shared cache (master source)."""
    if not tk:
        return
    try:
        import screener_fetch
        screener_fetch.init_schema()
        data = {
            "ticker": tk,
            "roce": fund.get("roce"), "roe": fund.get("roe"), "de": fund.get("de"),
            "pe": fund.get("pe"), "pb": None,
            "promoter_pct": fund.get("promoter_pct"), "promoter_pledge": 0,
            "sales_growth_3y": fund.get("sales_growth_3y"),
            "profit_growth_3y": fund.get("profit_growth_3y"),
            "market_cap": fund.get("market_cap"),
            "dividend_yield": fund.get("dividend_yield"), "face_value": None,
            "fetched_at": datetime.now().isoformat(),
            "fetch_ok": 1, "raw_note": f"from screener upload ({name})" if name else "screener upload",
        }
        screener_fetch._store(data)
    except Exception as e:
        print(f"persist upload fundamentals failed for {tk}: {e}")


def analyse_all_rows_quick(rows: list, uid) -> list:
    """
    FAST pre-rank: score ALL rows on fundamentals only (no Kite calls),
    persist each to the shared cache, return sorted by quick score.
    This makes uploaded data the master source AND lets us pick the true top-N.
    """
    from athena_dashboard import quality_score
    scored = []
    for rec in rows:
        nm = str(rec.get("name") or "").strip()
        m = match_ticker(nm, uid)
        tk = m.get("ticker")
        fund = {
            "roce": rec.get("roce"), "roe": rec.get("roe"), "de": rec.get("de"),
            "pe": rec.get("pe"), "market_cap": rec.get("market_cap"),
            "dividend_yield": rec.get("dividend_yield"),
            "sales_growth_3y": rec.get("sales_growth_3y"),
            "profit_growth_3y": rec.get("profit_growth_3y")
                                if rec.get("profit_growth_3y") is not None
                                else rec.get("profit_var_qtr"),
            "promoter_pct": rec.get("promoter_pct"), "promoter_pledge": 0, "fetch_ok": 1,
        }
        # persist to shared cache (this is the "master source" wiring)
        if tk:
            _persist_upload_fundamentals(tk, fund, nm)
        q = quality_score(fund)
        # quick score = quality + light valuation, no technicals yet
        pe = fund.get("pe")
        val = 100 if (pe and pe<=15) else 80 if (pe and pe<=25) else 55 if (pe and pe<=40) else 25 if (pe and pe<=70) else 10 if pe else 50
        quick = round((q["score"] or 0)*0.7 + val*0.3, 1)
        scored.append({"rec": rec, "name": nm, "ticker": tk, "match": m,
                       "fund": fund, "quick_score": quick, "quality": q["score"]})
    scored.sort(key=lambda x: x["quick_score"], reverse=True)
    return scored


def analyse_export_rows(rows: list, uid, db, max_analyse: int = 25) -> list:
    """Score + plan each uploaded row. Fundamentals come from the export itself.
    Now PRE-RANKS all rows, persists to cache, then deep-analyses the true top-N."""
    # Pre-rank ALL rows fast + persist fundamentals to shared cache
    ranked = analyse_all_rows_quick(rows, uid)
    top = ranked[:max_analyse]
    out, unmatched = [], []
    for item in top:
        rec = item["rec"]
        nm = item["name"]; m = item["match"]; tk = item["ticker"]; fund = item["fund"]
        tech = _technicals(tk, uid) if tk else {}
        if not tech and rec.get("cmp"):
            tech = {"price": rec["cmp"]}   # at least price from the export
        sm = _has_smart_money(db, uid, tk) if tk else {"found": False, "deals": []}
        score = composite_score(fund, tech, sm["found"])
        plan = entry_plan(tech, fund, score)
        if not tk:
            unmatched.append(nm)
        out.append({
            "ticker": tk or nm, "company_name": nm,
            "ticker_match": m,
            "composite": score["composite"], "grade": score["grade"],
            "components": score["components"],
            "quality_flags": score["quality_detail"].get("flags", []),
            "fundamentals": fund, "technicals": tech,
            "smart_money": sm, "plan": plan,
            "export_extras": {"ret_1y": rec.get("ret_1y"), "ret_3y": rec.get("ret_3y"),
                              "prom_change": rec.get("prom_change"),
                              "sales_var_qtr": rec.get("sales_var_qtr")},
        })
    out.sort(key=lambda x: x["composite"], reverse=True)
    return out




# ══════════════════════════════════════════════════════════════════════════
# SMART MONEY RADAR — discover stocks FROM bulk deals (not just boost existing)
# ══════════════════════════════════════════════════════════════════════════
# Known institutional / marquee buyer fingerprints (expandable).
INSTITUTIONAL_MARKERS = (
    "MUTUAL FUND", "MF", "INSURANCE", "LIFE INSURANCE", "LIC",
    "FOREIGN PORTFOLIO", "FPI", "FII", "PENSION", "AIF", "ALTERNATIVE INVESTMENT",
    "CAPITAL", "ASSET MANAGEMENT", "INVESTMENT", "FUND", "SECURITIES",
    "PORTFOLIO", "ADVISORS", "VENTURES", "HOLDINGS", "TRUST",
)
# Marquee individual investors (big bull names) - buying is a stronger signal
MARQUEE = ("KACHOLIA", "JHUNJHUNWALA", "KEDIA", "DAMANI", "AGARWAL",
           "GOEL", "KARNANI", "VIJAY", "MUKUL", "DOLLY", "ASHISH")


def _is_institutional(client: str) -> dict:
    c = (client or "").upper()
    for m in MARQUEE:
        if m in c:
            return {"inst": True, "type": "marquee", "weight": 1.5}
    for m in INSTITUTIONAL_MARKERS:
        if m in c:
            return {"inst": True, "type": "institution", "weight": 1.0}
    return {"inst": False, "type": "other", "weight": 0.3}


def scan_smart_money(uid, db, days: int = 45, min_net_buy_value: float = 5e6) -> list:
    """
    Find stocks with meaningful NET institutional buying in bulk deals.
    Returns candidates worth running through the quality funnel.
    """
    cutoff = (date.today() - timedelta(days=days)).isoformat()
    rows = db.execute("""SELECT ticker, client, buy_sell, quantity, price, deal_date
                         FROM bulk_deals WHERE user_id=? AND deal_date>=?""",
                      (uid, cutoff)).fetchall()
    agg = {}
    for r in rows:
        tk = r["ticker"]
        if not tk:
            continue
        info = _is_institutional(r["client"])
        val = (r["quantity"] or 0) * (r["price"] or 0)
        signed = val if (r["buy_sell"] == "BUY") else -val
        weighted = signed * info["weight"]
        a = agg.setdefault(tk, {"net_value": 0, "weighted_net": 0, "buy_val": 0, "sell_val": 0,
                                "inst_buyers": set(), "marquee": False, "deal_count": 0,
                                "buyers": []})
        a["net_value"] += signed
        a["weighted_net"] += weighted
        a["deal_count"] += 1
        if r["buy_sell"] == "BUY":
            a["buy_val"] += val
            if info["inst"]:
                a["inst_buyers"].add(r["client"][:40])
                a["buyers"].append({"client": r["client"][:50], "value": round(val,0),
                                    "type": info["type"], "date": r["deal_date"]})
            if info["type"] == "marquee":
                a["marquee"] = True
        else:
            a["sell_val"] += val

    # keep only meaningful net institutional buying
    candidates = []
    for tk, a in agg.items():
        if a["weighted_net"] < min_net_buy_value:
            continue
        if not a["inst_buyers"] and not a["marquee"]:
            continue  # must have at least one institutional/marquee buyer
        candidates.append({
            "ticker": tk,
            "net_buy_value": round(a["net_value"], 0),
            "weighted_signal": round(a["weighted_net"], 0),
            "buy_value": round(a["buy_val"], 0), "sell_value": round(a["sell_val"], 0),
            "institutional_buyers": len(a["inst_buyers"]),
            "marquee_buyer": a["marquee"],
            "deal_count": a["deal_count"],
            "top_buyers": sorted(a["buyers"], key=lambda x: x["value"], reverse=True)[:3],
        })
    candidates.sort(key=lambda x: x["weighted_signal"], reverse=True)
    return candidates




# ══════════════════════════════════════════════════════════════════════════
# SUBSTITUTION ENGINE — connect discovery candidates to Core 22
# Answers: what to BUY now, what to SUBSTITUTE, funded from fresh SIP
# ══════════════════════════════════════════════════════════════════════════
SUBSTITUTION_THRESHOLD = 15   # challenger must beat incumbent by this many points

def _core22_with_scores(uid, db):
    """Current Core 22 holdings with live quality scores."""
    from athena_dashboard import quality_score
    import screener_fetch
    try:
        rows = db.execute("""SELECT ticker, target_pct, bucket FROM core22_targets
                             WHERE user_id=? AND active=1""", (uid,)).fetchall()
    except Exception:
        rows = []
    held = {}
    try:
        for h in db.execute("SELECT ticker, quantity, last_price FROM stored_holdings WHERE user_id=?",
                            (uid,)).fetchall():
            held[h["ticker"]] = h
    except Exception:
        pass
    out = []
    for r in rows:
        tk = r["ticker"]
        fund = screener_fetch.get_cached(tk)
        q = quality_score(fund)
        h = held.get(tk)
        out.append({
            "ticker": tk, "bucket": r["bucket"], "target_pct": r["target_pct"],
            "quality_score": q["score"], "quality_grade": q["grade"],
            "flags": q["flags"], "held": bool(h),
            "value": round((h["quantity"]*h["last_price"]),0) if h else 0,
            "roce": fund.get("roce"), "pe": fund.get("pe"),
        })
    return out


def build_action_list(uid, db, candidates: list) -> dict:
    """
    Given discovery candidates (already scored+planned), decide for each:
      ADD NEW (fill gap, fund from SIP) | SUBSTITUTE (beats a weak holding) | WATCH | SKIP
    """
    core = _core22_with_scores(uid, db)
    core_tickers = {c["ticker"] for c in core}
    core_buckets = {}
    for c in core:
        core_buckets.setdefault(c["bucket"], []).append(c)
    # weakest holdings become substitution targets
    weak = sorted([c for c in core if c["quality_score"] is not None and c["quality_score"] < 55],
                  key=lambda x: x["quality_score"])

    buy_now, substitute, watch = [], [], []
    for g in candidates:
        tk = g["ticker"]; score = g.get("composite", 0); grade = g.get("grade", "")
        if tk in core_tickers:
            continue  # already own it
        if grade not in ("STRONG BUY", "BUY"):
            if grade == "WATCH":
                watch.append({"ticker": tk, "composite": score, "grade": grade,
                              "why": g.get("plan", {}).get("why", ""),
                              "stance": g.get("plan", {}).get("stance", "")})
            continue

        # Does it clearly beat a weak holding? -> SUBSTITUTE
        best_swap = None
        for w in weak:
            if w["quality_score"] is None:
                continue
            edge = score - w["quality_score"]
            if edge >= SUBSTITUTION_THRESHOLD:
                if not best_swap or edge > best_swap["edge"]:
                    best_swap = {"replace": w["ticker"], "replace_score": w["quality_score"],
                                 "replace_flags": w["flags"], "replace_value": w["value"],
                                 "edge": round(edge, 1)}
        if best_swap:
            substitute.append({
                "buy": tk, "buy_score": score, "buy_grade": grade,
                "buy_plan": g.get("plan", {}),
                "buy_fundamentals": g.get("fundamentals", {}),
                "smart_money": g.get("smart_money", {}).get("found", False),
                **best_swap,
                "reason": f"{tk} ({score}) beats {best_swap['replace']} ({best_swap['replace_score']}) by {best_swap['edge']} pts",
            })
        else:
            # No weak holding to replace -> ADD NEW from fresh SIP
            buy_now.append({
                "ticker": tk, "composite": score, "grade": grade,
                "plan": g.get("plan", {}),
                "fundamentals": g.get("fundamentals", {}),
                "smart_money": g.get("smart_money", {}).get("found", False),
                "stance": g.get("plan", {}).get("stance", ""),
                "reason": "Quality candidate, no weak holding to swap - fund from fresh SIP",
            })

    buy_now.sort(key=lambda x: x["composite"], reverse=True)
    substitute.sort(key=lambda x: x["edge"], reverse=True)
    return {
        "buy_now": buy_now, "substitute": substitute, "watch": watch,
        "core22_weak": [{"ticker": w["ticker"], "score": w["quality_score"], "flags": w["flags"]}
                        for w in weak],
        "threshold": SUBSTITUTION_THRESHOLD,
        "generated_at": datetime.now().isoformat(),
    }


def register_routes(app):
    from fastapi import Depends, HTTPException, UploadFile, File
    from auth import get_current_user
    from database import get_db

    @app.get("/discovery/presets")
    async def presets(current_user=Depends(get_current_user)):
        db = get_db()
        saved = [dict(r) for r in db.execute(
            "SELECT id,name,query FROM saved_queries WHERE user_id=?",
            (current_user["id"],)).fetchall()]
        return {"presets": PRESETS, "saved": saved}

    @app.post("/discovery/run")
    async def run(data: dict, current_user=Depends(get_current_user)):
        uid = current_user["id"]; db = get_db(); init_schema()
        preset = data.get("preset")
        query = data.get("query")
        if preset and preset in PRESETS:
            query = PRESETS[preset]["query"]
        if not query:
            raise HTTPException(400, "preset or query required")

        res = run_screener_query(query, pages=int(data.get("pages", 2)))
        if res["error"] and not res["tickers"]:
            raise HTTPException(502, f"Screener query failed: {res['error']}")

        # always include Core 22 + existing watchlist in the universe (Option C)
        extra = []
        try:
            for r in db.execute("SELECT ticker FROM core22_targets WHERE user_id=? AND active=1",
                                (uid,)).fetchall():
                extra.append(r["ticker"])
        except Exception:
            pass
        universe = res["tickers"] + [t for t in extra if t not in res["tickers"]] \
                   if data.get("include_core22") else res["tickers"]

        results = analyse_tickers(universe, uid, db,
                                  max_analyse=int(data.get("max_analyse", 20)))
        db.execute("""INSERT INTO discovery_runs (user_id,run_at,preset,query,matched,analysed,results_json)
                      VALUES (?,datetime('now'),?,?,?,?,?)""",
                   (uid, preset or "custom", query, res["count"], len(results),
                    json.dumps(results)))
        db.commit()
        return {"query": query, "preset": preset, "matched": res["count"],
                "analysed": len(results), "gems": results,
                "screener_error": res["error"],
                "generated_at": datetime.now().isoformat()}

    @app.post("/discovery/upload-screener")
    async def upload_screener(file: UploadFile = File(...),
                              max_analyse: int = 25,
                              current_user=Depends(get_current_user)):
        """
        Upload the Screener query export (xlsx or csv).
        Run your query on screener.in -> Export to Excel -> upload here.
        """
        uid = current_user["id"]; db = get_db(); init_schema()
        raw = await file.read()
        try:
            rows = parse_screener_export(raw, file.filename or "")
        except RuntimeError as e:
            raise HTTPException(400, str(e))
        if not rows:
            raise HTTPException(400, "Could not parse any rows. Is this a Screener export?")
        gems = analyse_export_rows(rows, uid, db, max_analyse=max_analyse)
        unmatched = [g["company_name"] for g in gems if not g["ticker_match"].get("ticker")]
        db.execute("""INSERT INTO discovery_runs (user_id,run_at,preset,query,matched,analysed,results_json)
                      VALUES (?,datetime('now'),?,?,?,?,?)""",
                   (uid, "screener_upload", file.filename or "upload",
                    len(rows), len(gems), json.dumps(gems)))
        db.commit()
        return {"source": "screener_export", "file": file.filename,
                "rows_parsed": len(rows), "analysed": len(gems),
                "unmatched_names": unmatched, "gems": gems,
                "generated_at": datetime.now().isoformat()}

    @app.get("/discovery/action-list")
    async def action_list(current_user=Depends(get_current_user)):
        """
        THE decision: from your last discovery run + smart-money, what to BUY now
        and what to SUBSTITUTE in Core 22. Combines both sources.
        """
        uid = current_user["id"]; db = get_db(); init_schema()
        # gather candidates: last upload run + smart-money worthy
        candidates = []
        last = db.execute("""SELECT results_json FROM discovery_runs WHERE user_id=?
                             ORDER BY id DESC LIMIT 1""", (uid,)).fetchone()
        if last:
            try:
                candidates.extend(json.loads(last["results_json"] or "[]"))
            except Exception:
                pass
        # add smart-money worthy stocks
        try:
            sm_cands = scan_smart_money(uid, db, days=45)
            if sm_cands:
                sm_gems = analyse_tickers([s["ticker"] for s in sm_cands[:10]], uid, db, max_analyse=10)
                sm_map = {s["ticker"]: s for s in sm_cands}
                for g in sm_gems:
                    g["smart_money"] = {"found": True, **sm_map.get(g["ticker"], {})}
                    if g["ticker"] not in {c.get("ticker") for c in candidates}:
                        candidates.append(g)
        except Exception as e:
            print(f"smart money merge failed: {e}")

        if not candidates:
            return {"buy_now": [], "substitute": [], "watch": [],
                    "message": "No candidates. Upload a Screener export or bulk deals first."}
        return build_action_list(uid, db, candidates)

    @app.get("/discovery/smart-money")
    async def smart_money(days: int = 45, analyse: int = 15, current_user=Depends(get_current_user)):
        """
        DISCOVER stocks from bulk deals: find meaningful institutional buying,
        then run each through the quality+valuation+technical funnel.
        Only surfaces smart-money buys that ALSO pass your investment bar.
        """
        uid = current_user["id"]; db = get_db(); init_schema()
        candidates = scan_smart_money(uid, db, days=days)
        if not candidates:
            return {"candidates": [], "worthy": [],
                    "message": "No meaningful net institutional buying found. Upload recent bulk deals first."}
        # run the top candidates through the full funnel
        tickers = [c["ticker"] for c in candidates[:analyse]]
        gems = analyse_tickers(tickers, uid, db, max_analyse=len(tickers))
        # merge smart-money context into each gem
        sm_map = {c["ticker"]: c for c in candidates}
        for g in gems:
            g["smart_money_detail"] = sm_map.get(g["ticker"], {})
        # split: worth buying (grade BUY+) vs smart-money-but-fails-quality
        worthy = [g for g in gems if g["grade"] in ("STRONG BUY", "BUY")]
        watch  = [g for g in gems if g["grade"] == "WATCH"]
        avoid  = [g for g in gems if g["grade"] == "PASS"]
        return {
            "scanned_deals_days": days,
            "smart_money_stocks": len(candidates),
            "analysed": len(gems),
            "worthy": worthy,          # smart money bought AND passes your bar
            "watch": watch,
            "smart_money_but_weak": [   # smart money bought but fails quality
                {"ticker": g["ticker"], "grade": g["grade"], "composite": g["composite"],
                 "flags": g["quality_flags"],
                 "buyers": g["smart_money_detail"].get("top_buyers", [])}
                for g in avoid],
            "all_candidates": candidates[:analyse],
            "generated_at": datetime.now().isoformat(),
        }

    @app.get("/discovery/results")
    async def last_results(current_user=Depends(get_current_user)):
        db = get_db()
        r = db.execute("""SELECT * FROM discovery_runs WHERE user_id=?
                          ORDER BY id DESC LIMIT 1""", (current_user["id"],)).fetchone()
        if not r:
            return {"gems": [], "message": "No discovery run yet"}
        return {"run_at": r["run_at"], "preset": r["preset"], "query": r["query"],
                "matched": r["matched"], "gems": json.loads(r["results_json"] or "[]")}

    @app.post("/discovery/analyze")
    async def analyze(data: dict, current_user=Depends(get_current_user)):
        uid = current_user["id"]; db = get_db(); init_schema()
        tickers = [t.upper().strip() for t in (data.get("tickers") or []) if t.strip()]
        if not tickers:
            raise HTTPException(400, "tickers required")
        return {"gems": analyse_tickers(tickers, uid, db, max_analyse=len(tickers))}

    @app.post("/discovery/bulk-deals")
    async def upload_bulk(file: UploadFile = File(...), current_user=Depends(get_current_user)):
        """
        Bulk/block deals CSV — supports BOTH NSE and BSE formats (auto-detected).
        NSE cols: Date, Symbol, Security Name, Client Name, Buy/Sell, Quantity Traded, Trade Price
        BSE cols: Deal Date, Security Code, Company, Client Name, Deal Type, Quantity, Price
        """
        uid = current_user["id"]; db = get_db(); init_schema()
        text = (await file.read()).decode("utf-8-sig", errors="ignore")
        reader = csv.DictReader(io.StringIO(text))
        n, exch, buys, sells = 0, "unknown", 0, 0
        for row in reader:
            row = {(k or "").strip().lower(): (v or "").strip() for k, v in row.items()}

            # ── symbol/ticker: NSE gives Symbol, BSE gives only a numeric code + Company ──
            sym = (row.get("symbol") or "").upper()
            if sym:
                tk, exch = sym, "NSE"
            else:
                # BSE: use company name -> match to NSE symbol later; store company as ticker for now
                comp = (row.get("company") or row.get("security name") or "").upper()
                code = row.get("security code") or ""
                tk = comp or code
                exch = "BSE"
            if not tk:
                continue

            # ── date: NSE 20-JUL-2026 | BSE 01-07-2026 ──
            d = row.get("date") or row.get("deal date") or ""
            d_iso = _parse_deal_date(d)

            client = row.get("client name") or row.get("client") or ""

            # ── buy/sell: NSE BUY/SELL | BSE P(urchase)/S(ell) ──
            bs_raw = (row.get("buy / sell") or row.get("buy/sell") or
                      row.get("deal type") or "").upper()
            if bs_raw.startswith("B") or bs_raw.startswith("P"):
                bs = "BUY"; buys += 1
            elif bs_raw.startswith("S"):
                bs = "SELL"; sells += 1
            else:
                bs = bs_raw

            try:
                qty = float((row.get("quantity traded") or row.get("quantity") or "0").replace(",", ""))
                pxs = (row.get("trade price / wght. avg. price") or row.get("price") or "0")
                px = float(pxs.replace(",", ""))
            except ValueError:
                qty, px = 0, 0

            # For BSE, try to resolve the company name to an NSE tradingsymbol so it
            # matches the same ticker your holdings/discovery use.
            if exch == "BSE" and row.get("company"):
                m = match_ticker(row.get("company"), uid)
                if m.get("ticker"):
                    tk = m["ticker"]

            try:
                db.execute("""INSERT OR IGNORE INTO bulk_deals
                    (user_id,deal_date,ticker,client,buy_sell,quantity,price,uploaded_at)
                    VALUES (?,?,?,?,?,?,?,datetime('now'))""",
                    (uid, d_iso, tk, client, bs, qty, px))
                n += 1
            except Exception:
                pass
        db.commit()
        return {"message": f"{n} deals imported ({exch} format)", "count": n,
                "exchange": exch, "buys": buys, "sells": sells}

    @app.get("/discovery/bulk-deals")
    async def get_bulk(days: int = 45, current_user=Depends(get_current_user)):
        db = get_db()
        cutoff = (date.today() - timedelta(days=days)).isoformat()
        rows = db.execute("""SELECT * FROM bulk_deals WHERE user_id=? AND deal_date>=?
                             ORDER BY deal_date DESC LIMIT 200""",
                          (current_user["id"], cutoff)).fetchall()
        return {"deals": [dict(r) for r in rows], "count": len(rows)}

    @app.post("/discovery/save-query")
    async def save_query(data: dict, current_user=Depends(get_current_user)):
        db = get_db(); init_schema()
        db.execute("INSERT INTO saved_queries (user_id,name,query,created_at) VALUES (?,?,?,datetime('now'))",
                   (current_user["id"], data.get("name", "My query"), data.get("query", "")))
        db.commit()
        return {"message": "query saved"}
