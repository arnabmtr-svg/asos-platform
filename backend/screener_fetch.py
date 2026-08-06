"""
screener_fetch.py — Robust fundamental data fetcher for ATHENA
Fetches ROCE, D/E, PE, ROE, promoter holding, sales growth, profit growth
from screener.in with retry + graceful fallback.

Standalone module. Used by athena_dashboard.py for the "best for long term" check.

Public functions:
  fetch_one(ticker) -> dict           # scrape a single stock
  sync_all(tickers) -> dict           # scrape many, store in DB
  get_cached(ticker) -> dict          # read from DB cache
  get_all_cached() -> list            # all cached fundamentals
  init_schema()                       # create the table
"""

import re, time, html as _html
from datetime import datetime
import httpx

SCREENER_BASE = "https://www.screener.in/company"
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/120.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}

# ── DB schema ──────────────────────────────────────────────────────────────
def init_schema():
    from database import get_db
    db = get_db()
    db.execute("""
        CREATE TABLE IF NOT EXISTS fundamentals (
            ticker         TEXT PRIMARY KEY,
            roce           REAL,
            roe            REAL,
            de             REAL,
            pe             REAL,
            pb             REAL,
            promoter_pct   REAL,
            promoter_pledge REAL,
            sales_growth_3y REAL,
            profit_growth_3y REAL,
            market_cap     REAL,
            dividend_yield REAL,
            face_value     REAL,
            fetched_at     TEXT,
            fetch_ok       INTEGER DEFAULT 1,
            raw_note       TEXT DEFAULT ''
        )
    """)
    db.commit()


# ── HTML number extraction ─────────────────────────────────────────────────
def _num(pattern, text, default=None):
    """Extract a float following a label. Handles commas, %, spaces."""
    m = re.search(pattern, text, re.I | re.S)
    if not m:
        return default
    raw = m.group(1).replace(",", "").replace("%", "").strip()
    try:
        return float(raw)
    except ValueError:
        return default


def _parse_screener_html(html_text: str) -> dict:
    """
    Screener.in renders key ratios in a <ul id="top-ratios"> list where each
    <li> has a <span class="name">Label</span> and <span class="value">N</span>.
    We extract by finding the label then the nearest number after it.
    """
    t = html_text
    out = {}

    # The ratios list — grab the block to search within
    ratios_block = t
    mblock = re.search(r'id="top-ratios".*?</ul>', t, re.S)
    if mblock:
        ratios_block = mblock.group(0)

    def near(label):
        # find label, then first number (with optional decimals) after it
        m = re.search(re.escape(label) + r'.*?([\d,]+\.?\d*)', ratios_block, re.S | re.I)
        if m:
            try:
                return float(m.group(1).replace(",", ""))
            except ValueError:
                return None
        return None

    out["pe"]           = near("Stock P/E")
    out["pb"]           = near("Price to Book") or near("P/B")
    out["market_cap"]   = near("Market Cap")
    out["dividend_yield"] = near("Dividend Yield")
    out["roce"]         = near("ROCE")
    out["roe"]          = near("ROE")
    out["face_value"]   = near("Face Value")

    # Debt to equity often in the ratios or elsewhere
    out["de"] = _num(r'Debt to equity[^\d]*([\d.]+)', t) or near("Debt to equity")

    # Promoter holding
    out["promoter_pct"] = _num(r'Promoter[s]?\s*[Hh]olding[^\d]*([\d.]+)', t) \
                          or _num(r'Promoters?\s*</td>\s*<td[^>]*>([\d.]+)', t)
    # Pledged
    out["promoter_pledge"] = _num(r'[Pp]ledged[^\d]*([\d.]+)', t, 0)

    # Compounded growth (Screener shows "Compounded Sales Growth" / "Compounded Profit Growth" tables)
    # 3-year figures — grab the 3 Years row value
    sales_block = re.search(r'Compounded Sales Growth.*?3 Years[^\d\-]*(-?[\d.]+)', t, re.S | re.I)
    if sales_block:
        try: out["sales_growth_3y"] = float(sales_block.group(1))
        except ValueError: out["sales_growth_3y"] = None
    profit_block = re.search(r'Compounded Profit Growth.*?3 Years[^\d\-]*(-?[\d.]+)', t, re.S | re.I)
    if profit_block:
        try: out["profit_growth_3y"] = float(profit_block.group(1))
        except ValueError: out["profit_growth_3y"] = None

    return out


# ── Single fetch with retry + consolidated/standalone fallback ─────────────
def fetch_one(ticker: str, retries: int = 3) -> dict:
    ticker = ticker.upper().strip()
    urls = [
        f"{SCREENER_BASE}/{ticker}/consolidated/",
        f"{SCREENER_BASE}/{ticker}/",
    ]
    last_err = ""
    for attempt in range(retries):
        for url in urls:
            try:
                with httpx.Client(timeout=15, follow_redirects=True, headers=HEADERS) as c:
                    r = c.get(url)
                    if r.status_code == 200 and "top-ratios" in r.text:
                        data = _parse_screener_html(r.text)
                        # require at least one meaningful field
                        if any(data.get(k) is not None for k in ("roce", "pe", "roe")):
                            data.update({"ticker": ticker, "fetched_at": datetime.now().isoformat(),
                                         "fetch_ok": 1, "raw_note": f"ok via {url.split('/')[-2]}"})
                            return data
                    last_err = f"HTTP {r.status_code}"
            except Exception as e:
                last_err = str(e)[:80]
        time.sleep(1.5 * (attempt + 1))  # backoff between full retries
    return {"ticker": ticker, "fetch_ok": 0, "fetched_at": datetime.now().isoformat(),
            "raw_note": f"FAILED: {last_err}"}


# ── Store / read cache ─────────────────────────────────────────────────────
def _store(data: dict):
    from database import get_db
    db = get_db()
    db.execute("""
        INSERT INTO fundamentals
          (ticker,roce,roe,de,pe,pb,promoter_pct,promoter_pledge,
           sales_growth_3y,profit_growth_3y,market_cap,dividend_yield,
           face_value,fetched_at,fetch_ok,raw_note)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        ON CONFLICT(ticker) DO UPDATE SET
          roce=COALESCE(excluded.roce, fundamentals.roce),
          roe=COALESCE(excluded.roe, fundamentals.roe),
          de=COALESCE(excluded.de, fundamentals.de),
          pe=COALESCE(excluded.pe, fundamentals.pe),
          pb=COALESCE(excluded.pb, fundamentals.pb),
          promoter_pct=COALESCE(excluded.promoter_pct, fundamentals.promoter_pct),
          promoter_pledge=COALESCE(excluded.promoter_pledge, fundamentals.promoter_pledge),
          sales_growth_3y=COALESCE(excluded.sales_growth_3y, fundamentals.sales_growth_3y),
          profit_growth_3y=COALESCE(excluded.profit_growth_3y, fundamentals.profit_growth_3y),
          market_cap=COALESCE(excluded.market_cap, fundamentals.market_cap),
          dividend_yield=COALESCE(excluded.dividend_yield, fundamentals.dividend_yield),
          face_value=COALESCE(excluded.face_value, fundamentals.face_value),
          fetched_at=excluded.fetched_at,
          fetch_ok=excluded.fetch_ok,
          raw_note=excluded.raw_note
    """, (data.get("ticker"), data.get("roce"), data.get("roe"), data.get("de"),
          data.get("pe"), data.get("pb"), data.get("promoter_pct"), data.get("promoter_pledge"),
          data.get("sales_growth_3y"), data.get("profit_growth_3y"), data.get("market_cap"),
          data.get("dividend_yield"), data.get("face_value"), data.get("fetched_at"),
          data.get("fetch_ok", 1), data.get("raw_note", "")))
    db.commit()


def sync_all(tickers: list, delay: float = 2.0) -> dict:
    """Fetch fundamentals for a list of tickers, store in DB. Returns summary."""
    init_schema()
    ok, failed = [], []
    for tk in tickers:
        data = fetch_one(tk)
        _store(data)
        (ok if data.get("fetch_ok") else failed).append(tk)
        time.sleep(delay)  # polite to screener.in
    return {"updated": ok, "failed": failed, "total": len(tickers),
            "synced_at": datetime.now().isoformat()}


def get_cached(ticker: str) -> dict:
    from database import get_db
    r = get_db().execute("SELECT * FROM fundamentals WHERE ticker=?",
                         (ticker.upper(),)).fetchone()
    return dict(r) if r else {}


def get_all_cached() -> list:
    from database import get_db
    rows = get_db().execute("SELECT * FROM fundamentals ORDER BY ticker").fetchall()
    return [dict(r) for r in rows]




# ── DIAGNOSTIC: verify parsing works on YOUR machine ──────────────────────
def diagnose(ticker: str = "CGPOWER"):
    """
    Run this on your machine to confirm screener fetching + parsing works:
        python screener_fetch.py --diagnose CGPOWER
    Prints raw fetch status and every parsed field.
    """
    ticker = ticker.upper()
    print(f"\n{'='*55}")
    print(f"SCREENER DIAGNOSTIC — {ticker}")
    print('='*55)
    urls = [f"{SCREENER_BASE}/{ticker}/consolidated/", f"{SCREENER_BASE}/{ticker}/"]
    for url in urls:
        print(f"\nTrying: {url}")
        try:
            with httpx.Client(timeout=15, follow_redirects=True, headers=HEADERS) as c:
                r = c.get(url)
                print(f"  HTTP status: {r.status_code}")
                print(f"  Response size: {len(r.text)} chars")
                print(f"  Has 'top-ratios': {'top-ratios' in r.text}")
                if r.status_code == 200 and "top-ratios" in r.text:
                    data = _parse_screener_html(r.text)
                    print("  PARSED FIELDS:")
                    for k, v in data.items():
                        flag = "OK" if v is not None else "-- (not found)"
                        print(f"    {k:18} {str(v):12} {flag}")
                    found = sum(1 for v in data.values() if v is not None)
                    print(f"\n  RESULT: {found}/{len(data)} fields parsed successfully")
                    if found >= 4:
                        print("  VERDICT: Parsing WORKS. Safe to sync all Core 22.")
                    else:
                        print("  VERDICT: Weak parse. Screener may have changed layout - tell Claude.")
                    return
        except Exception as e:
            print(f"  ERROR: {e}")
    print("\n  VERDICT: Could not fetch. Check internet / screener.in reachable in browser.")


# ── CLI test ───────────────────────────────────────────────────────────────
if __name__ == "__main__":
    import sys
    if len(sys.argv) > 1 and sys.argv[1] == "--diagnose":
        diagnose(sys.argv[2] if len(sys.argv) > 2 else "CGPOWER")
    else:
        tk = sys.argv[1] if len(sys.argv) > 1 else "CGPOWER"
        print(f"Fetching {tk} from screener.in...")
        d = fetch_one(tk)
        for k, v in d.items():
            print(f"  {k:18} {v}")