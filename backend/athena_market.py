"""
athena_market.py — Live Market Intel (fixes stale events, movers, news)
- /market/events : real computed events (RBI MPC 2026, FOMC 2026, F&O expiry, results season, SIP day)
- /market/news   : live Google News RSS (free, no API key) filtered for Indian markets + holdings
Movers already live via existing /market/movers.

Add to main.py:
  try: import athena_market
  except ImportError: athena_market = None
  ...
  if athena_market: athena_market.register_routes(app)
"""
from datetime import datetime, timedelta, date
import pytz

IST = pytz.timezone("Asia/Kolkata")

# ── Real published calendars (data, not UI hardcoding) ────────────────────
RBI_MPC_2026 = ["2026-02-06","2026-04-08","2026-06-05","2026-08-06","2026-10-01","2026-12-04"]
FOMC_2026    = ["2026-01-28","2026-03-18","2026-04-29","2026-06-17","2026-07-29","2026-09-16","2026-11-04","2026-12-16"]
US_CPI_2026  = ["2026-01-13","2026-02-11","2026-03-11","2026-04-10","2026-05-12","2026-06-10","2026-07-14","2026-08-12","2026-09-11","2026-10-13","2026-11-12","2026-12-10"]
RESULTS_SEASONS = [("2026-07-01","Q1 FY27"),("2026-10-01","Q2 FY27"),("2027-01-01","Q3 FY27"),("2027-04-01","Q4 FY27")]

def _last_thursday(year:int, month:int) -> date:
    if month == 12: nxt = date(year+1,1,1)
    else:           nxt = date(year,month+1,1)
    d = nxt - timedelta(days=1)
    while d.weekday() != 3: d -= timedelta(days=1)
    return d

def _next_thursday(from_d: date) -> date:
    d = from_d + timedelta(days=(3 - from_d.weekday()) % 7 or 7)
    return d

def compute_events(sip_day:int=5, horizon_days:int=45) -> list:
    today = datetime.now(IST).date()
    events = []
    def add(dstr_or_d, title, sub, impact):
        d = date.fromisoformat(dstr_or_d) if isinstance(dstr_or_d,str) else dstr_or_d
        days = (d - today).days
        if 0 <= days <= horizon_days:
            events.append({"date": d.isoformat(), "day": d.day,
                           "month": d.strftime("%b").upper(), "days_away": days,
                           "title": title, "sub": sub, "impact": impact})

    for d in RBI_MPC_2026:
        add(d, "RBI MPC Decision", "Repo rate decision. Rate-sensitives in focus: INDUSINDBK, PIRAMALFIN. Options desk: avoid selling premium 48h before (event filter).", "HIGH")
    for d in FOMC_2026:
        add(d, "US Fed FOMC Meeting", "US rate decision — key driver for FII flows into India and global risk appetite.", "HIGH")
    for d in US_CPI_2026:
        add(d, "US CPI Print", "US inflation data. Below-consensus = dovish Fed = positive India FII flows.", "MEDIUM")
    for dstr, label in RESULTS_SEASONS:
        add(dstr, f"{label} Results Season Begins", "Earnings window for all Core 22 holdings. No lump-sum buys within 5 days of a holding's results (ASOS rule).", "MEDIUM")

    # F&O expiries (computed)
    m_exp = _last_thursday(today.year, today.month)
    if m_exp < today:
        nm = today.month + 1 if today.month < 12 else 1
        ny = today.year if today.month < 12 else today.year + 1
        m_exp = _last_thursday(ny, nm)
    add(m_exp, "F&O Monthly Expiry", "High gamma-risk week for open Iron Condor positions. Monitor DTE and adjust per 50%-target / 2×-stop rules.", "MEDIUM")
    add(_next_thursday(today), "Weekly Options Expiry", "NIFTY weekly expiry Thursday. IC entries per Tuesday-entry / 7-DTE playbook.", "LOW")

    # SIP day (from user settings)
    sip_d = date(today.year, today.month, min(sip_day,28))
    if sip_d < today:
        nm = today.month + 1 if today.month < 12 else 1
        ny = today.year if today.month < 12 else today.year + 1
        sip_d = date(ny, nm, min(sip_day,28))
    add(sip_d, "Monthly SIP Day", "Deploy ₹1L via SIP Optimizer — allocation computed from live RSI + weight gaps + VIX mode.", "HIGH")

    events.sort(key=lambda e: e["days_away"])
    return events


# ── Live news via Google News RSS (free) ──────────────────────────────────
_news_cache = {"at": None, "items": []}

async def fetch_news(tickers: list) -> list:
    import httpx, re, html as htmllib
    global _news_cache
    now = datetime.now()
    if _news_cache["at"] and (now - _news_cache["at"]).seconds < 900:
        return _news_cache["items"]

    queries = ["NSE India stock market", "RBI monetary policy"]
    # Add top 3 portfolio tickers as queries
    for t in (tickers or [])[:3]:
        queries.append(f"{t} NSE stock")

    items = []
    async with httpx.AsyncClient(timeout=12, follow_redirects=True,
                                 headers={"User-Agent":"Mozilla/5.0"}) as c:
        for q in queries:
            try:
                url = f"https://news.google.com/rss/search?q={q.replace(' ','+')}&hl=en-IN&gl=IN&ceid=IN:en"
                r = await c.get(url)
                if r.status_code != 200: continue
                xml = r.text
                for m in re.finditer(r"<item>(.*?)</item>", xml, re.S):
                    block = m.group(1)
                    t  = re.search(r"<title>(.*?)</title>", block, re.S)
                    pd = re.search(r"<pubDate>(.*?)</pubDate>", block, re.S)
                    src= re.search(r"<source[^>]*>(.*?)</source>", block, re.S)
                    if not t: continue
                    title = htmllib.unescape(re.sub(r"<.*?>","",t.group(1))).strip()
                    source = htmllib.unescape(src.group(1)).strip() if src else "Google News"
                    when = ""
                    if pd:
                        try:
                            dt = datetime.strptime(pd.group(1).strip()[:25], "%a, %d %b %Y %H:%M:%S")
                            when = dt.strftime("%d %b, %H:%M")
                        except Exception:
                            when = pd.group(1).strip()[:16]
                    items.append({"title": title[:160], "source": source, "time": when, "query": q})
                    if len([i for i in items if i["query"]==q]) >= 3: break
            except Exception:
                continue

    # Dedupe by title
    seen, unique = set(), []
    for i in items:
        k = i["title"][:60]
        if k in seen: continue
        seen.add(k); unique.append(i)
    _news_cache = {"at": now, "items": unique[:10]}
    return unique[:10]


def register_routes(app):
    from fastapi import Depends
    from auth import get_current_user
    from database import get_db

    @app.get("/market/events")
    async def market_events(current_user=Depends(get_current_user)):
        db = get_db()
        row = db.execute("SELECT sip_date FROM user_settings WHERE user_id=?",
                         (current_user["id"],)).fetchone()
        sip_day = int(row["sip_date"]) if row and row["sip_date"] else 5
        return {"events": compute_events(sip_day), "generated_at": datetime.now(IST).isoformat()}

    @app.get("/market/news")
    async def market_news(current_user=Depends(get_current_user)):
        db = get_db()
        rows = db.execute("""SELECT ticker FROM stored_holdings WHERE user_id=?
                             ORDER BY quantity*last_price DESC LIMIT 3""",
                          (current_user["id"],)).fetchall()
        tickers = [r["ticker"] for r in rows] or ["CGPOWER","HINDCOPPER"]
        items = await fetch_news(tickers)
        return {"news": items, "cached_minutes": 15,
                "note": "Live via Google News RSS — refreshes every 15 min"}