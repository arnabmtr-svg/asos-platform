"""
replacement_engine.py — Core 22 Position Replacement Engine
Detects exit triggers and scores replacement candidates.

Add to main.py:
  import replacement_engine
  replacement_engine.register_routes(app)
"""

from datetime import datetime, timedelta

# ── 8-metric scorecard ────────────────────────────────────────────────────
def score_candidate(ticker: str, fundamentals: dict) -> dict:
    """Score a stock 0–80 on 8 replacement metrics."""
    f    = fundamentals
    roce = f.get("roce", 20)
    de   = f.get("de", 0.5)
    rev_cagr  = f.get("rev_cagr", 15)
    pat_cagr  = f.get("pat_cagr", 15)
    pe_vs_sec = f.get("pe_vs_sector_pct", 0)   # % premium to sector avg
    promoter  = f.get("promoter_pct", 50)
    moat      = f.get("moat", 5)                # 1–10
    tailwind  = f.get("tailwind", 7)            # 1–10

    # ROCE score
    s1 = 2 if roce < 15 else 4 if roce < 18 else 6 if roce < 22 else 8 if roce < 28 else 10

    # Revenue CAGR
    s2 = 2 if rev_cagr < 10 else 4 if rev_cagr < 15 else 6 if rev_cagr < 20 else 8 if rev_cagr < 30 else 10

    # PAT CAGR
    s3 = 2 if pat_cagr < 10 else 4 if pat_cagr < 15 else 6 if pat_cagr < 20 else 8 if pat_cagr < 30 else 10

    # D/E (banking excluded)
    is_bank = f.get("is_bank", False)
    if is_bank:
        s4 = 5  # neutral for banks
    else:
        s4 = 2 if de > 1.5 else 5 if de > 0.5 else 7 if de > 0.3 else 10

    # P/E vs sector (premium = bad, discount = good)
    s5 = 2 if pe_vs_sec > 50 else 5 if pe_vs_sec > 20 else 7 if pe_vs_sec > 0 else 10

    # Promoter holding
    s6 = 2 if promoter < 35 else 5 if promoter < 50 else 7 if promoter < 65 else 10

    # Moat (passed as 1–10)
    s7 = min(10, max(1, moat))

    # Sector tailwind (passed as 1–10)
    s8 = min(10, max(1, tailwind))

    total = s1 + s2 + s3 + s4 + s5 + s6 + s7 + s8

    return {
        "ticker":  ticker,
        "score":   total,
        "max":     80,
        "pct":     round(total / 80 * 100, 1),
        "breakdown": {
            "roce":        s1, "rev_cagr": s2, "pat_cagr":   s3,
            "de":          s4, "valuation": s5, "promoter":   s6,
            "moat":        s7, "tailwind":  s8,
        }
    }


# ── Exit trigger checker ──────────────────────────────────────────────────
def check_exit_triggers(ticker: str, fundamentals: dict, is_bank: bool = False) -> list:
    """Return list of triggered exit conditions."""
    f       = fundamentals
    triggers = []

    roce      = f.get("roce", 20)
    de        = f.get("de", 0.5)
    pat_cagr  = f.get("pat_cagr", 15)
    promoter  = f.get("promoter_pct", 50)
    pledging  = f.get("promoter_pledging_pct", 0)
    underperf = f.get("underperformance_vs_nifty_1yr", 0)  # negative = underperformed
    thesis_ok = f.get("thesis_intact", True)

    # RED-1: Fundamental collapse
    if roce < 18 and not is_bank:
        triggers.append({
            "level": "RED",
            "trigger": "RED-1",
            "description": f"ROCE {roce}% below 18% minimum threshold",
            "action": "Stop SIP immediately. Start 30-day replacement search."
        })
    if de > 1.5 and not is_bank:
        triggers.append({
            "level": "RED",
            "trigger": "RED-1",
            "description": f"D/E ratio {de} above 1.5 limit (non-banking stock)",
            "action": "Stop SIP. Evaluate if this is structural or temporary."
        })
    if pat_cagr < 0:
        triggers.append({
            "level": "RED",
            "trigger": "RED-1",
            "description": f"PAT CAGR {pat_cagr}% — earnings declining",
            "action": "Flag for quarterly results watch. 2 consecutive declines = exit."
        })
    if pledging > 20:
        triggers.append({
            "level": "RED",
            "trigger": "RED-1",
            "description": f"Promoter pledging {pledging}% — governance risk",
            "action": "Immediate review. Exit if pledging continues rising."
        })

    # RED-2: Thesis breakdown
    if not thesis_ok:
        triggers.append({
            "level": "RED",
            "trigger": "RED-2",
            "description": "Investment thesis has broken — structural, not cyclical",
            "action": "Exit regardless of P&L. The thesis is the only reason to hold."
        })

    # AMBER-3: 12-month underperformance
    if underperf < -15:
        triggers.append({
            "level": "AMBER",
            "trigger": "AMBER-3",
            "description": f"Underperformed Nifty 50 by {abs(underperf):.0f}% over 12 months",
            "action": "Run replacement scorecard. Replace if alternative scores 15+ higher."
        })

    # AMBER-4: Promoter holding very low
    if promoter < 35:
        triggers.append({
            "level": "AMBER",
            "trigger": "AMBER-4",
            "description": f"Promoter holding {promoter}% — skin-in-game concern",
            "action": "Monitor quarterly. If continues falling, initiate replacement."
        })

    return triggers


# ── FastAPI routes ────────────────────────────────────────────────────────
def register_routes(app):
    from fastapi import Depends, HTTPException
    from auth import get_current_user

    @app.post("/portfolio/replacement-analysis")
    async def replacement_analysis(data: dict,
                                   current_user=Depends(get_current_user)):
        """
        Run full replacement analysis for a Core 22 stock.
        Body: {
          "current_ticker": "INDUSINDBK",
          "current_fundamentals": {roce, de, rev_cagr, pat_cagr, ...},
          "candidates": [
            {"ticker":"KOTAKBANK", "fundamentals":{...}},
            {"ticker":"HDFCBANK",  "fundamentals":{...}},
          ]
        }
        """
        current_ticker = data.get("current_ticker", "").upper()
        current_fund   = data.get("current_fundamentals", {})
        candidates     = data.get("candidates", [])

        if not current_ticker:
            raise HTTPException(400, "current_ticker required")

        is_bank = current_fund.get("is_bank", False)

        # Score current stock
        current_score   = score_candidate(current_ticker, current_fund)
        exit_triggers   = check_exit_triggers(current_ticker, current_fund, is_bank)

        # Score each candidate
        scored_candidates = []
        for c in candidates:
            t = c.get("ticker","").upper()
            f = c.get("fundamentals", {})
            s = score_candidate(t, f)
            s["gap"]       = s["score"] - current_score["score"]
            s["recommend"] = (
                "REPLACE — gap ≥ 15" if s["gap"] >= 15 else
                "CONSIDER — gap 8–14, only if trigger also fired" if s["gap"] >= 8 else
                "HOLD CURRENT — insufficient advantage"
            )
            scored_candidates.append(s)

        scored_candidates.sort(key=lambda x: -x["score"])
        best = scored_candidates[0] if scored_candidates else None

        # Overall verdict
        red_count  = sum(1 for t in exit_triggers if t["level"] == "RED")
        amber_count = sum(1 for t in exit_triggers if t["level"] == "AMBER")
        gap         = best["gap"] if best else 0

        if red_count >= 2 or (red_count >= 1 and gap >= 8):
            verdict = "EXIT — multiple RED triggers or RED + strong alternative"
        elif red_count == 1 and gap < 8:
            verdict = "REVIEW — RED trigger fired but no strong alternative yet"
        elif amber_count >= 2 and gap >= 15:
            verdict = "CONSIDER REPLACING — weak fundamentals + strong alternative"
        elif gap >= 15 and not exit_triggers:
            verdict = "OPTIONAL REPLACE — significantly better alternative available"
        else:
            verdict = "HOLD — no compelling reason to replace"

        # Tax timing
        holding_days = data.get("holding_days", 400)
        tax_note = (
            f"Held {holding_days}d — LTCG applies (12.5% above ₹1.25L). "
            f"{'Tax efficient — hold crossed 1yr.' if holding_days >= 365 else 'Hold to 1yr to avoid STCG if possible.'}"
        )

        return {
            "current":          current_score,
            "exit_triggers":    exit_triggers,
            "red_count":        red_count,
            "amber_count":      amber_count,
            "candidates":       scored_candidates,
            "best_alternative": best,
            "verdict":          verdict,
            "threshold":        "Replace only if gap ≥ 15 points OR RED trigger fired",
            "tax_note":         tax_note,
            "next_steps": [
                "Stop SIP on current stock immediately" if red_count else "Reduce SIP to 50%",
                f"Ask GLM Scout: full comparison {current_ticker} vs {best['ticker'] if best else 'alternatives'}",
                "Check holding period for LTCG efficiency",
                "Exit in 2–3 tranches over 2–4 weeks",
                f"Enter {best['ticker'] if best else 'replacement'} via A/B/C/D ladder",
            ],
            "timestamp": datetime.now().isoformat()
        }


    @app.get("/portfolio/core22-health")
    async def core22_health(current_user=Depends(get_current_user)):
        """
        Daily Core 22 health check — flags any positions needing review.
        Uses static fundamentals (update weekly via Screener sync).
        """
        from decision_engine import FUNDAMENTALS, THESIS_REVIEW_STOCKS

        # Known risk positions with static fundamental data
        RISK_POSITIONS = {
            "INDUSINDBK": {
                "roce": 15, "de": 8.0, "rev_cagr": 12, "pat_cagr": -5,
                "promoter_pct": 14, "pledging": 0, "is_bank": True,
                "thesis_intact": False,
                "note": "Promoter concerns + NPA trajectory + PAT under pressure"
            },
            "TATAPOWER": {
                "roce": 14, "de": 1.1, "rev_cagr": 18, "pat_cagr": 22,
                "promoter_pct": 37, "pledging": 0, "is_bank": False,
                "thesis_intact": True,
                "note": "ROCE below threshold but renewables thesis intact. Watch D/E."
            },
            "RELIANCE": {
                "roce": 12, "de": 0.4, "rev_cagr": 8, "pat_cagr": 6,
                "promoter_pct": 50, "pledging": 0, "is_bank": False,
                "thesis_intact": True,
                "note": "ROCE below threshold. Position size (2%) too small to matter."
            },
            "PIRAMALFIN": {
                "roce": 11, "de": 4.0, "rev_cagr": 20, "pat_cagr": 35,
                "promoter_pct": 46, "pledging": 0, "is_bank": True,
                "thesis_intact": True,
                "note": "NBFC rebuild — ROCE recovering. PAT growth strong."
            },
        }

        flags = []
        for ticker, fund in RISK_POSITIONS.items():
            triggers = check_exit_triggers(ticker, fund, fund.get("is_bank", False))
            if triggers:
                flags.append({
                    "ticker":   ticker,
                    "triggers": triggers,
                    "note":     fund.get("note",""),
                    "action":   "review" if any(t["level"]=="RED" for t in triggers) else "monitor"
                })

        return {
            "flags":          flags,
            "total_flagged":  len(flags),
            "red_flags":      sum(1 for f in flags if f["action"]=="review"),
            "message": (
                f"{len(flags)} Core 22 positions need attention" if flags
                else "All Core 22 positions fundamentally healthy"
            ),
            "last_updated": datetime.now().isoformat(),
            "next_screener_sync": "Weekly — update fundamentals from Screener.in"
        }