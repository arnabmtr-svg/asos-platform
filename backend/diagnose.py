"""
diagnose.py — pinpoint exactly where Kite data fails.
Run from backend folder:  python diagnose.py
"""
import sys

print("="*60)
print("STEP 1 — Can we import kite_data_patch?")
try:
    import kite_data_patch as kp
    print("  OK — imported")
    print("  Has _kite:", hasattr(kp, "_kite"))
    print("  Has get_movers:", hasattr(kp, "get_movers"))
except Exception as e:
    print("  FAIL:", e); sys.exit()

print("="*60)
print("STEP 2 — Is there a token in the database?")
try:
    from database import get_db
    db = get_db()
    rows = db.execute("SELECT id, kite_api_key, kite_api_secret, kite_access_token, kite_connected FROM users").fetchall()
    for r in rows:
        tok = r["kite_access_token"]
        print(f"  User {r['id']}: api_key={'SET' if r['kite_api_key'] else 'EMPTY'} "
              f"secret={'SET' if r['kite_api_secret'] else 'EMPTY'} "
              f"token={'SET ('+str(len(tok))+' chars)' if tok else 'EMPTY'} "
              f"connected={r['kite_connected']}")
except Exception as e:
    print("  FAIL:", e)

print("="*60)
print("STEP 3 — Can _kite() build a KiteConnect object?")
try:
    k = kp._kite(None)
    print("  _kite() returned:", type(k).__name__ if k else "None")
    if k is None:
        print("  >>> This is the problem. Token not found or KiteService returned None.")
except Exception as e:
    print("  FAIL:", e)

print("="*60)
print("STEP 4 — Does KiteService store the object as ._kite or .kite?")
try:
    from kite_service import KiteService
    db = get_db()
    r = db.execute("SELECT kite_api_key,kite_api_secret,kite_access_token FROM users WHERE kite_access_token IS NOT NULL AND kite_access_token!='' LIMIT 1").fetchone()
    if r:
        svc = KiteService(r["kite_api_key"], r["kite_api_secret"], r["kite_access_token"])
        print("  svc._kite exists:", hasattr(svc, "_kite"), "->", type(getattr(svc,"_kite",None)).__name__)
        print("  svc.kite exists:", hasattr(svc, "kite"))
        print("  KITE_AVAILABLE:", getattr(__import__('kite_service'),'KITE_AVAILABLE','?'))
    else:
        print("  No token in DB to test with")
except Exception as e:
    print("  FAIL:", e)

print("="*60)
print("STEP 5 — Try a real quote call")
try:
    k = kp._kite(None)
    if k:
        q = k.quote(["NSE:INFY"])
        print("  quote() returned:", q if q else "EMPTY")
    else:
        print("  Skipped — no kite object")
except Exception as e:
    print("  FAIL:", type(e).__name__, str(e)[:200])

print("="*60)
print("STEP 6 — get_movers() end to end")
try:
    print(" ", kp.get_movers(None))
except Exception as e:
    print("  FAIL:", e)
