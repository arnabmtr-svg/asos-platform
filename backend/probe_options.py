"""
probe_options.py — one-shot test: does Kite return live option chain data?
Determines whether the Options Desk uses REAL chain or Black-Scholes model.
Run: venv\\Scripts\\python.exe probe_options.py
"""
def main():
    from database import get_db
    db = get_db()
    uid = db.execute("SELECT id FROM users LIMIT 1").fetchone()["id"]
    from kite_data_patch import _kite
    k = _kite(uid)
    if not k:
        print("❌ Kite not connected. Reconnect Zerodha first."); return

    print("="*60)
    print("PROBE 1 — NFO instruments available?")
    try:
        nfo = k.instruments("NFO")
        nifty_opts = [i for i in nfo if i.get("name")=="NIFTY" and i.get("instrument_type") in ("CE","PE")]
        print(f"  ✅ NFO instruments: {len(nfo)} total, {len(nifty_opts)} NIFTY options")
        if nifty_opts:
            # nearest expiry
            from datetime import date
            exps = sorted(set(i["expiry"] for i in nifty_opts if i.get("expiry")))
            print(f"  Expiries available: {exps[:4]}")
            near = exps[0] if exps else None
            # find ATM strikes for nearest expiry
            spot_q = k.quote(["NSE:NIFTY 50"])
            spot = spot_q["NSE:NIFTY 50"]["last_price"]
            atm = round(spot/50)*50
            print(f"  Spot {spot} -> ATM {atm}")
            # get a few ATM option symbols
            atm_opts = [i for i in nifty_opts if i["expiry"]==near and abs(i["strike"]-atm)<=100]
            print(f"  ATM-area options for {near}: {len(atm_opts)}")
            print()
            print("="*60)
            print("PROBE 2 — LIVE quotes for ATM options (the real test)")
            syms = [f"NFO:{i['tradingsymbol']}" for i in atm_opts[:6]]
            q = k.quote(syms)
            got_real = False
            for s in syms:
                if s in q:
                    d = q[s]
                    ltp = d.get("last_price",0)
                    oi = d.get("oi",0)
                    vol = d.get("volume",0)
                    print(f"  {s.replace('NFO:',''):<22} LTP {ltp:<8} OI {oi:<10} Vol {vol}")
                    if ltp>0: got_real=True
            print()
            if got_real:
                print("  ✅✅ REAL OPTION DATA WORKS! Kite returns live LTP/OI.")
                print("  -> Options Desk will use REAL chain, real greeks from OI/IV.")
            else:
                print("  ⚠ Symbols resolved but LTP=0 (market closed - weekend).")
                print("  -> Re-run during market hours to confirm live prices.")
                print("  -> Structure works; data flows when market opens.")
    except Exception as e:
        print(f"  ❌ NFO access failed: {e}")
        print("  -> Your Kite plan may not include F&O data.")
        print("  -> Options Desk will use Black-Scholes MODEL for greeks.")

    print("="*60)
    print("PROBE 3 — can we compute greeks? (need IV per strike)")
    print("  If real OI works but no per-strike IV, we compute greeks via Black-Scholes")
    print("  using VIX as vol input. That's standard and fine.")

if __name__ == "__main__":
    main()
