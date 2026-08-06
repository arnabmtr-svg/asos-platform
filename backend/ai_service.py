"""
ai_service.py — GLM 5.2 via Fireworks AI
Drop into backend/ folder alongside main.py

Fireworks AI endpoint: https://api.fireworks.ai/inference/v1/chat/completions
Model: accounts/fireworks/models/glm-5p2
Key format: fw_xxxxx (your Fireworks AI key)
Pricing: $1.40/M input · $0.26/M cached · $4.40/M output
Context: 1,040,576 tokens (1M)
"""

import httpx
from datetime import datetime

# ── Endpoints ─────────────────────────────────────────────────────────────
FW_ENDPOINT   = "https://api.fireworks.ai/inference/v1/chat/completions"
FW_MODEL      = "accounts/fireworks/models/glm-5p2"
FW_MODEL_FAST = "accounts/fireworks/models/llama-v3p3-70b-instruct"  # fast fallback

# ── ASOS system prompt ────────────────────────────────────────────────────
ASOS_SYSTEM = """You are the ASOS AI Scout — expert financial intelligence inside Arnab Mitra's personal wealth platform.

## Arnab's portfolio (ASOS framework)
- Goal: ₹19.8 Cr corpus by 2047 at 20% CAGR
- Monthly SIP: ₹1,00,000 split across Core 22
- Effective corpus: ~₹29–31L (holdings − ₹8.58L withdrawal + pending T+1)
- Core 22 = 22 stocks across 5 buckets

## Core 22 allocation
B1 Index ETFs (30%): NIFTYBEES 12% · MON100 10% · JUNIORBEES 8%
B2 Capex/Defence (25%): CGPOWER 9% · TATAPOWER 7% · BDL 5% · HBLENGINE 4%
B3 Compounders (30%): HINDCOPPER 5% · HINDALCO 5% · ANGELONE 4% · FINCABLES 4% · GRANULES 4% · SONACOMS 3% · PRICOLLTD 2% · INDUSINDBK 2% · RELIANCE 2%
B4 Tactical (10%): PIRAMALFIN 3.5% · HSCL 3% · SHILCHAR 2% · GMDCLTD 1.5%
B5 Crisis Reserve (5%): GOLDBEES 3% · SILVERETF 2%

## ASOS rules always applied
VIX timing: <13 deploy 75% · 13–16 full SIP · 16–20 half SIP · 20–25 pause · >25 double SIP
Ladder entry: A = 52-week high, B = A×0.90 (buy 2%), C = B×0.90 (buy 2%), D = C×0.90 (buy 2%), target each level up
Iron Condor: VIX<20 + IVP 30–80% + ADX<20 → deploy 16Δ, 5 lots, target 50%, stop 2×
RSI signals: >65 + overweight → TRIM 10% · <35 + underweight → STRONG ADD 2× SIP
ASOS stock filter: ROCE>22%, D/E<1, FCF+ve, promoter holding >50%, 10yr sector tailwind

## Response style
- Always specific: actual ₹ values, % allocations, entry/exit prices, stop losses
- Max 4 paragraphs for complex analysis, 2 for simple questions
- Reference Arnab's actual Core 22 stocks — not generic advice
- Flag risks with numbers: "if X falls 15% from here, stop is ₹Y"
- Today: """ + datetime.now().strftime("%d %b %Y") + """
"""


async def call_glm(messages: list, api_key: str,
                   model: str = None,
                   temperature: float = 0.35,
                   max_tokens: int = 1500) -> dict:
    """
    Call GLM 5.2 via Fireworks AI.
    Returns: {text, model, tokens, cost_usd, error}
    """
    if not api_key or not api_key.startswith("fw_"):
        return {
            "text":  "⚠ Fireworks AI key not set or invalid. Go to Settings → GLM AI Scout → enter your fw_xxx key.",
            "model": None, "tokens": {}, "cost_usd": 0, "error": "no_api_key"
        }

    use_model = model or FW_MODEL
    payload   = {
        "model":       use_model,
        "messages":    messages,
        "temperature": temperature,
        "max_tokens":  max_tokens,
    }
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type":  "application/json",
        "Accept":        "application/json",
    }

    async with httpx.AsyncClient(timeout=60.0) as client:
        try:
            r = await client.post(FW_ENDPOINT, json=payload, headers=headers)
            if r.status_code == 200:
                data   = r.json()
                text   = data["choices"][0]["message"]["content"]
                usage  = data.get("usage", {})
                inp    = usage.get("prompt_tokens", 0)
                out    = usage.get("completion_tokens", 0)
                cached = usage.get("prompt_tokens_details", {}).get("cached_tokens", 0)
                # Fireworks pricing: $1.40/M input, $0.26/M cached, $4.40/M output
                cost   = ((inp - cached) * 1.40 + cached * 0.26 + out * 4.40) / 1_000_000
                return {
                    "text":    text,
                    "model":   data.get("model", use_model),
                    "tokens":  {"input": inp, "output": out, "cached": cached, "total": inp + out},
                    "cost_usd": round(cost, 6),
                    "error":   None,
                }
            elif r.status_code == 401:
                return {"text": "⚠ Invalid Fireworks API key. Check Settings → GLM AI Scout.",
                        "model": None, "tokens": {}, "cost_usd": 0, "error": "invalid_key"}
            elif r.status_code == 429:
                return {"text": "⚠ Fireworks rate limit hit. Try again in 30 seconds.",
                        "model": None, "tokens": {}, "cost_usd": 0, "error": "rate_limited"}
            elif r.status_code == 402:
                return {"text": "⚠ Fireworks account out of credits. Top up at fireworks.ai",
                        "model": None, "tokens": {}, "cost_usd": 0, "error": "no_credits"}
            else:
                return {"text": f"⚠ Fireworks error {r.status_code}: {r.text[:300]}",
                        "model": None, "tokens": {}, "cost_usd": 0, "error": f"http_{r.status_code}"}

        except httpx.TimeoutException:
            return {"text": "⚠ Request timed out. GLM 5.2 is a large model — try again.",
                    "model": None, "tokens": {}, "cost_usd": 0, "error": "timeout"}
        except Exception as e:
            return {"text": f"⚠ Connection error: {str(e)[:200]}",
                    "model": None, "tokens": {}, "cost_usd": 0, "error": str(e)[:100]}


def build_scout_messages(user_message: str,
                          history: list,
                          market_context: dict = None) -> list:
    """Build messages array with system prompt + live context + history."""
    system = ASOS_SYSTEM

    # Inject live market snapshot if available
    if market_context:
        lines = []
        if market_context.get("vix"):
            v = market_context["vix"]
            sip = ("DOUBLE SIP" if v > 25 else "PAUSE SIP" if v > 20 else
                   "HALF SIP" if v > 16 else "FULL SIP" if v > 13 else "75% SIP")
            lines.append(f"India VIX: {v} → {sip}")
        if market_context.get("ivp"):
            lines.append(f"IVP: {market_context['ivp']}% (IC deploy: {'YES' if 30 <= market_context['ivp'] <= 80 else 'NO'})")
        if market_context.get("nifty_spot"):
            lines.append(f"Nifty spot: ₹{market_context['nifty_spot']:,.0f}")
        if market_context.get("effective_corpus"):
            ec = market_context["effective_corpus"]
            lines.append(f"Effective corpus: ₹{ec/1e5:.2f}L")
        if lines:
            system += "\n\n## Live market data right now\n" + "\n".join(lines)

    messages = [{"role": "system", "content": system}]
    for turn in (history or [])[-8:]:
        messages.append({"role": turn["role"], "content": turn["content"]})
    messages.append({"role": "user", "content": user_message})
    return messages


def estimate_cost(tokens: dict) -> float:
    """Fireworks AI GLM 5.2 pricing."""
    inp    = tokens.get("input", 0)
    out    = tokens.get("output", 0)
    cached = tokens.get("cached", 0)
    return round(((inp - cached) * 1.40 + cached * 0.26 + out * 4.40) / 1_000_000, 6)


# ── Quick test ────────────────────────────────────────────────────────────
if __name__ == "__main__":
    import asyncio
    api_key = input("Enter your Fireworks API key (fw_xxx): ").strip()
    result  = asyncio.run(call_glm(
        messages=[{"role":"user","content":"What is 2+2? Reply in one word."}],
        api_key=api_key, max_tokens=20
    ))
    print("Response:", result["text"])
    print("Tokens:  ", result["tokens"])
    print("Cost:    ", f"${result['cost_usd']}")