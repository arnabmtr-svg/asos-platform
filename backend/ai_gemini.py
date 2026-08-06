"""
ai_gemini.py — Gemini 2.5 Flash (FREE tier) integration for ATHENA AI Scout
Free key: https://ai.google.dev  (no card, ~1500 req/day)

Routes:
  POST /ai/gemini-setup    {api_key}      -> save key (never returned to browser)
  GET  /ai/gemini-status                  -> {configured, model}
  POST /ai/gemini-test                    -> {ok, reply} live ping
  POST /ai/gemini-chat     {message,...}  -> market-analysis chat (FREE path)

PRIVACY RULE (enforced here):
  Only MARKET analysis goes through the free tier. Any message mentioning
  holdings/corpus/portfolio/cash is flagged and routed away (caller should
  use the paid path). Free tiers may train on submitted data.

Add to main.py:
  try: import ai_gemini
  except ImportError: ai_gemini = None
  # in lifespan:  if ai_gemini: ai_gemini.init_schema()
  # after app:    if ai_gemini: ai_gemini.register_routes(app)
"""
import json
from datetime import datetime

BASE = "https://generativelanguage.googleapis.com/v1beta"

# Preferred free-tier models, best first. Google retires model IDs periodically,
# so we DISCOVER what the key can actually use instead of hardcoding one.
MODEL_CANDIDATES = [
    "gemini-3-flash",            # current recommended free default (2026)
    "gemini-3.1-flash-lite",     # 15 RPM, highest free throughput
    "gemini-2.5-flash",          # legacy - still works for older projects
    "gemini-2.5-flash-lite",
    "gemini-2.0-flash",
]

_model_cache = {}   # user_id -> resolved model id


def _endpoint(model):
    return f"{BASE}/models/{model}:generateContent"

# Words that indicate PERSONAL portfolio data -> must NOT go to free tier
PRIVATE_MARKERS = (
    "my holding", "my portfolio", "my corpus", "my cash", "my position",
    "holdings", "corpus", "portfolio value", "my sip", "my money",
    "how much do i", "my capital", "my account", "net worth",
)

SYSTEM_PROMPT = (
    "You are ATHENA's market analyst for an Indian equity and derivatives investor. "
    "You analyse markets, sectors, stocks, macro events and options conditions. "
    "Be direct and specific. Use stock names in CAPS. Give concrete numbers. "
    "Indian market context: NSE/BSE, NIFTY, BANKNIFTY, INDIA VIX, SEBI rules. "
    "Never invent data you were not given. If unsure, say so plainly."
)


# ── schema ────────────────────────────────────────────────────────────────
def init_schema():
    from database import get_db
    db = get_db()
    for col, typ, default in [
        ("google_api_key", "TEXT", "''"),
        ("gemini_calls", "INTEGER", "0"),
    ]:
        try:
            db.execute(f"ALTER TABLE user_settings ADD COLUMN {col} {typ} DEFAULT {default}")
            db.commit()
        except Exception:
            pass  # column exists


def _get_key(user_id):
    from database import get_db
    r = get_db().execute(
        "SELECT google_api_key FROM user_settings WHERE user_id=?", (user_id,)
    ).fetchone()
    try:
        return (r["google_api_key"] or "").strip() if r else ""
    except Exception:
        return ""


def is_private(text: str) -> bool:
    t = (text or "").lower()
    return any(m in t for m in PRIVATE_MARKERS)




async def list_models(api_key: str) -> list:
    """Ask Google which models this key can actually use."""
    import httpx
    try:
        async with httpx.AsyncClient(timeout=20) as cl:
            r = await cl.get(f"{BASE}/models?key={api_key}")
        if r.status_code != 200:
            return []
        out = []
        for m in r.json().get("models", []):
            name = (m.get("name") or "").replace("models/", "")
            methods = m.get("supportedGenerationMethods", []) or []
            if "generateContent" in methods:
                out.append(name)
        return out
    except Exception:
        return []


async def resolve_model(api_key: str, user_id=None) -> str:
    """Pick the best available model for this key. Cached per user."""
    if user_id is not None and user_id in _model_cache:
        return _model_cache[user_id]
    available = await list_models(api_key)
    chosen = None
    if available:
        # exact preference match first
        for cand in MODEL_CANDIDATES:
            if cand in available:
                chosen = cand
                break
        # else any flash model (cheapest/free tier)
        if not chosen:
            flashes = [m for m in available if "flash" in m and "preview" not in m]
            if flashes:
                chosen = sorted(flashes)[-1]
        if not chosen:
            chosen = available[0]
    if not chosen:
        chosen = MODEL_CANDIDATES[0]
    if user_id is not None:
        _model_cache[user_id] = chosen
    return chosen


# ── core call ─────────────────────────────────────────────────────────────
async def call_gemini(api_key: str, message: str, history=None,
                      system: str = SYSTEM_PROMPT, max_tokens: int = 1200,
                      temperature: float = 0.3, model: str = None,
                      user_id=None) -> dict:
    """Single call to Gemini. Auto-resolves model. Returns {text, error, tokens, model}."""
    if not model:
        model = await resolve_model(api_key, user_id)
    import httpx
    contents = []
    for h in (history or [])[-8:]:
        role = "user" if h.get("role") == "user" else "model"
        contents.append({"role": role, "parts": [{"text": h.get("content", "")}]})
    contents.append({"role": "user", "parts": [{"text": message}]})

    payload = {
        "contents": contents,
        "systemInstruction": {"parts": [{"text": system}]},
        "generationConfig": {
            "temperature": temperature,
            "maxOutputTokens": max_tokens,
        },
    }
    try:
        async with httpx.AsyncClient(timeout=45) as c:
            r = await c.post(f"{_endpoint(model)}?key={api_key}", json=payload)
        # model retired / unavailable -> rediscover once and retry
        if r.status_code == 404:
            if user_id is not None:
                _model_cache.pop(user_id, None)
            newm = await resolve_model(api_key, user_id)
            if newm and newm != model:
                model = newm
                async with httpx.AsyncClient(timeout=45) as c:
                    r = await c.post(f"{_endpoint(model)}?key={api_key}", json=payload)
        if r.status_code != 200:
            detail = ""
            try:
                detail = r.json().get("error", {}).get("message", "")[:200]
            except Exception:
                detail = r.text[:200]
            return {"text": "", "error": f"HTTP {r.status_code}: {detail}", "tokens": {}, "model": model}
        d = r.json()
        cand = (d.get("candidates") or [{}])[0]
        parts = (cand.get("content") or {}).get("parts") or []
        text = "".join(p.get("text", "") for p in parts).strip()
        usage = d.get("usageMetadata", {}) or {}
        return {
            "text": text or "(empty response)",
            "error": None,
            "model": model,
            "tokens": {
                "prompt": usage.get("promptTokenCount", 0),
                "output": usage.get("candidatesTokenCount", 0),
                "total": usage.get("totalTokenCount", 0),
            },
        }
    except Exception as e:
        return {"text": "", "error": str(e)[:200], "tokens": {}, "model": model}


# ── routes ────────────────────────────────────────────────────────────────
def register_routes(app):
    from fastapi import Depends, HTTPException
    from auth import get_current_user
    from database import get_db

    @app.post("/ai/gemini-setup")
    async def gemini_setup(data: dict, current_user=Depends(get_current_user)):
        key = (data.get("api_key") or "").strip()
        if not key:
            raise HTTPException(400, "api_key required - the key field was empty")
        # No format check: key formats change. Gemini itself validates on /ai/gemini-test.
        init_schema()
        db = get_db()
        uid = current_user["id"]
        try:
            exists = db.execute("SELECT 1 FROM user_settings WHERE user_id=?", (uid,)).fetchone()
            if exists:
                db.execute("UPDATE user_settings SET google_api_key=? WHERE user_id=?", (key, uid))
            else:
                db.execute("""INSERT INTO user_settings (user_id, google_api_key, sip_amount,
                              target_cagr, target_year) VALUES (?,?,100000,20,2047)""", (uid, key))
            db.commit()
        except Exception as e:
            raise HTTPException(500, f"DB error saving key: {str(e)[:150]}")
        return {"message": "Gemini key saved", "configured": True}

    @app.get("/ai/gemini-status")
    async def gemini_status(current_user=Depends(get_current_user)):
        key = _get_key(current_user["id"])
        model = await resolve_model(key, current_user["id"]) if key else None
        db = get_db()
        try:
            r = db.execute("SELECT gemini_calls FROM user_settings WHERE user_id=?",
                           (current_user["id"],)).fetchone()
            calls = r["gemini_calls"] if r else 0
        except Exception:
            calls = 0
        return {
            "configured": bool(key),
            "model": model,
            "provider": "Google AI (free tier)",
            "calls_made": calls or 0,
            "message": (f"Gemini key set - using {model}" if key else "No key - get one free at ai.google.dev"),
        }


    @app.get("/ai/gemini-models")
    async def gemini_models(current_user=Depends(get_current_user)):
        key = _get_key(current_user["id"])
        if not key:
            return {"models": [], "error": "no key saved"}
        avail = await list_models(key)
        chosen = await resolve_model(key, current_user["id"]) if avail else None
        return {"models": avail, "using": chosen, "count": len(avail)}

    @app.post("/ai/gemini-test")
    async def gemini_test(current_user=Depends(get_current_user)):
        key = _get_key(current_user["id"])
        if not key:
            return {"ok": False, "error": "No key saved. Paste your AIza... key and Save first."}
        avail = await list_models(key)
        if not avail:
            return {"ok": False, "error": "Key rejected by Google, or no models available. "
                                          "Check the key at ai.google.dev."}
        res = await call_gemini(key, "Reply with exactly: OK", max_tokens=20,
                                user_id=current_user["id"])
        if res["error"]:
            return {"ok": False, "error": res["error"], "available_models": avail[:8]}
        return {"ok": True, "reply": res["text"][:100], "model": res.get("model"),
                "tokens": res.get("tokens", {}), "available_models": avail[:8]}

    @app.post("/ai/gemini-chat")
    async def gemini_chat(data: dict, current_user=Depends(get_current_user)):
        """
        Market-analysis chat on the FREE tier.
        Refuses personal-portfolio questions (privacy) and tells caller to use paid path.
        """
        uid = current_user["id"]
        key = _get_key(uid)
        if not key:
            raise HTTPException(400, "Gemini key not set — Settings -> Gemini card")

        msg = (data.get("message") or "").strip()
        if not msg:
            raise HTTPException(400, "message required")

        if is_private(msg):
            return {
                "reply": ("This question involves your personal portfolio data. For privacy, "
                          "portfolio-specific queries don't go through the free tier "
                          "(free tiers may train on submitted data). Use the paid AI Scout path "
                          "for anything about your holdings, corpus, or cash."),
                "routed": "blocked_private",
                "model": None,
            }

        # optional live market context (public data only — safe for free tier)
        ctx = ""
        if data.get("include_market_context", True):
            try:
                from kite_data_patch import compute_ivp_ivr, get_nifty_spot
                iv = compute_ivp_ivr(uid)
                sp = get_nifty_spot(uid)
                ctx = (f"\n\n[Live market: NIFTY {sp.get('nifty')} ({sp.get('nifty_chg')}%), "
                       f"BANKNIFTY {sp.get('banknifty')}, INDIA VIX {iv.get('vix')}, "
                       f"IVP {iv.get('ivp')}%, IVR {iv.get('ivr')}%]")
            except Exception:
                pass
            try:
                import income_engine
                reg = income_engine.get_regime("NIFTY", uid, store=False)
                ctx += f"\n[Regime: {reg['regime']} ({reg['confidence']:.0f}%) - {reg.get('why','')}]"
            except Exception:
                pass

        res = await call_gemini(key, msg + ctx, history=data.get("history", []), user_id=uid)
        if res["error"]:
            return {"reply": f"Gemini error: {res['error']}", "error": res["error"], "model": res.get("model")}

        try:
            db = get_db()
            db.execute("UPDATE user_settings SET gemini_calls=COALESCE(gemini_calls,0)+1 WHERE user_id=?", (uid,))
            db.commit()
        except Exception:
            pass

        return {
            "reply": res["text"],
            "model": res.get("model"),
            "provider": "Google AI (free)",
            "tokens": res.get("tokens", {}),
            "cost_usd": 0.0,
            "routed": "gemini_free",
        }
