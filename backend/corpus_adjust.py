"""
corpus_adjust.py — Corpus / withdrawal adjustment endpoints
Fixes: the stale withdrawal_amount that double-counts against live holdings.

Your live holdings already reflect past withdrawals, so withdrawal_amount should
normally be 0. This module lets you view and set it cleanly.

main.py:
  try: import corpus_adjust
  except ImportError: corpus_adjust = None
  # after app:  if corpus_adjust: corpus_adjust.register_routes(app)
"""
from datetime import datetime


def register_routes(app):
    from fastapi import Depends, HTTPException
    from auth import get_current_user
    from database import get_db

    @app.get("/corpus/adjustment")
    async def get_adjustment(current_user=Depends(get_current_user)):
        db = get_db(); uid = current_user["id"]
        r = db.execute("""SELECT withdrawal_amount, pending_credit
                          FROM user_settings WHERE user_id=?""", (uid,)).fetchone()
        w = (r["withdrawal_amount"] if r else 0) or 0
        p = (r["pending_credit"] if r else 0) or 0
        return {"withdrawal_amount": w, "pending_credit": p,
                "note": ("Live holdings already reflect past withdrawals. "
                         "If this is non-zero it is subtracted AGAIN from your corpus "
                         "(double-counting). Set to 0 unless you have un-reflected cash out.")}

    @app.post("/corpus/set-withdrawal")
    async def set_withdrawal(data: dict, current_user=Depends(get_current_user)):
        db = get_db(); uid = current_user["id"]
        amt = float(data.get("withdrawal_amount", 0) or 0)
        exists = db.execute("SELECT 1 FROM user_settings WHERE user_id=?", (uid,)).fetchone()
        if exists:
            db.execute("UPDATE user_settings SET withdrawal_amount=? WHERE user_id=?", (amt, uid))
        else:
            db.execute("""INSERT INTO user_settings (user_id, withdrawal_amount, sip_amount,
                          target_cagr, target_year) VALUES (?,?,100000,20,2047)""", (uid, amt))
        db.commit()
        return {"message": f"withdrawal_amount set to {amt:.0f}", "withdrawal_amount": amt}

    @app.post("/corpus/set-pending")
    async def set_pending(data: dict, current_user=Depends(get_current_user)):
        db = get_db(); uid = current_user["id"]
        amt = float(data.get("pending_credit", 0) or 0)
        exists = db.execute("SELECT 1 FROM user_settings WHERE user_id=?", (uid,)).fetchone()
        if exists:
            db.execute("UPDATE user_settings SET pending_credit=? WHERE user_id=?", (amt, uid))
        else:
            db.execute("""INSERT INTO user_settings (user_id, pending_credit, sip_amount,
                          target_cagr, target_year) VALUES (?,?,100000,20,2047)""", (uid, amt))
        db.commit()
        return {"message": f"pending_credit set to {amt:.0f}", "pending_credit": amt}
