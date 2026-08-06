"""
kite_service.py — Zerodha Kite Connect wrapper
Handles: session, holdings, positions, quotes, options chain, strike selection
All Black-Scholes math is computed here (no Sensibull / TrueData needed)
"""

import math, json
from typing import Optional, List, Dict
from datetime import date

try:
    from kiteconnect import KiteConnect
    KITE_AVAILABLE = True
except ImportError:
    KITE_AVAILABLE = False
    print("⚠  kiteconnect not installed. Run: pip install kiteconnect")


# ── Black-Scholes helpers ─────────────────────────────────────────────────────
def _norm_cdf(x: float) -> float:
    a = [0.254829592, -0.284496736, 1.421413741, -1.453152027, 1.061405429]
    p = 0.3275911
    sign = 1 if x >= 0 else -1
    t = 1.0 / (1.0 + p * abs(x))
    y = 1.0 - (((((a[4]*t + a[3])*t) + a[2])*t + a[1])*t + a[0]) * t * math.exp(-x*x/2)
    return 0.5 * (1 + sign * y)

def bs_delta(S: float, K: float, T: float, r: float, sigma: float, opt="call") -> float:
    if T <= 0 or sigma <= 0:
        return (1.0 if S >= K else 0.0) if opt == "call" else (-1.0 if S < K else 0.0)
    d1 = (math.log(S / K) + (r + 0.5 * sigma**2) * T) / (sigma * math.sqrt(T))
    return _norm_cdf(d1) if opt == "call" else _norm_cdf(d1) - 1

def bs_theta(S: float, K: float, T: float, r: float, sigma: float, opt="call") -> float:
    if T <= 0 or sigma <= 0:
        return 0.0
    d1 = (math.log(S / K) + (r + 0.5 * sigma**2) * T) / (sigma * math.sqrt(T))
    d2 = d1 - sigma * math.sqrt(T)
    nd1 = (1 / math.sqrt(2 * math.pi)) * math.exp(-0.5 * d1**2)
    if opt == "call":
        theta = (-S * nd1 * sigma / (2 * math.sqrt(T)) - r * K * math.exp(-r * T) * _norm_cdf(d2)) / 365
    else:
        theta = (-S * nd1 * sigma / (2 * math.sqrt(T)) + r * K * math.exp(-r * T) * _norm_cdf(-d2)) / 365
    return round(theta, 4)

def bs_gamma(S: float, K: float, T: float, r: float, sigma: float) -> float:
    if T <= 0 or sigma <= 0:
        return 0.0
    d1 = (math.log(S / K) + (r + 0.5 * sigma**2) * T) / (sigma * math.sqrt(T))
    nd1 = (1 / math.sqrt(2 * math.pi)) * math.exp(-0.5 * d1**2)
    return round(nd1 / (S * sigma * math.sqrt(T)), 6)

def bs_price(S: float, K: float, T: float, r: float, sigma: float, opt="call") -> float:
    if T <= 0:
        return max(0, S - K) if opt == "call" else max(0, K - S)
    d1 = (math.log(S / K) + (r + 0.5 * sigma**2) * T) / (sigma * math.sqrt(T))
    d2 = d1 - sigma * math.sqrt(T)
    if opt == "call":
        return S * _norm_cdf(d1) - K * math.exp(-r * T) * _norm_cdf(d2)
    else:
        return K * math.exp(-r * T) * _norm_cdf(-d2) - S * _norm_cdf(-d1)

def find_16delta_strike(spot: float, vix: float, dte: int, step: int, side="call") -> dict:
    """Find the strike closest to 16Δ using Black-Scholes."""
    T     = dte / 365
    sigma = (vix / 100) * math.sqrt(dte / 365)
    r     = 0.065
    lo    = int(spot * 0.88 / step) * step
    hi    = int(spot * 1.12 / step) * step
    best_k, best_diff = lo, 999
    for k in range(lo, hi + step, step):
        delta = abs(bs_delta(spot, k, T, r, sigma, side))
        diff  = abs(delta - 0.16)
        if diff < best_diff:
            best_diff = diff
            best_k    = k
    actual_delta = bs_delta(spot, best_k, T, r, sigma, side)
    return {"short": best_k, "delta": round(actual_delta, 3)}


def build_synthetic_chain(spot: float, vix: float, dte: int, step: int) -> List[dict]:
    """
    Build a complete options chain using Black-Scholes when Kite instruments
    are not yet loaded or for demo mode.
    """
    T     = max(dte / 365, 0.001)
    sigma = (vix / 100) * math.sqrt(T)
    r     = 0.065
    lo    = int(spot * 0.92 / step) * step
    hi    = int(spot * 1.08 / step) * step
    chain = []
    for K in range(lo, hi + step, step):
        c_delta = round(bs_delta(spot, K, T, r, sigma, "call"), 3)
        p_delta = round(bs_delta(spot, K, T, r, sigma, "put"),  3)
        c_price = round(bs_price(spot, K, T, r, sigma, "call"), 1)
        p_price = round(bs_price(spot, K, T, r, sigma, "put"),  1)
        chain.append({
            "strike":      K,
            "call_ltp":    c_price,
            "call_bid":    round(c_price * 0.97, 1),
            "call_ask":    round(c_price * 1.03, 1),
            "call_delta":  c_delta,
            "call_theta":  bs_theta(spot, K, T, r, sigma, "call"),
            "call_gamma":  bs_gamma(spot, K, T, r, sigma),
            "call_iv":     round(vix * (0.9 + abs(c_delta - 0.5) * 0.2), 1),
            "put_ltp":     p_price,
            "put_bid":     round(p_price * 0.97, 1),
            "put_ask":     round(p_price * 1.03, 1),
            "put_delta":   p_delta,
            "put_theta":   bs_theta(spot, K, T, r, sigma, "put"),
            "put_gamma":   bs_gamma(spot, K, T, r, sigma),
            "put_iv":      round(vix * (0.9 + abs(p_delta + 0.5) * 0.2), 1),
            "is_atm":      K == int(round(spot / step) * step),
            "call_16d":    abs(c_delta - 0.16) < 0.04,
            "put_16d":     abs(abs(p_delta) - 0.16) < 0.04,
        })
    return chain


def build_ic_structure(spot: float, vix: float, dte: int,
                       wing_call: int, wing_put: int, step: int, lot: int) -> dict:
    """Build complete IC structure: strikes, entry prices, exit levels."""
    call_s = find_16delta_strike(spot, vix, dte, step, "call")
    put_s  = find_16delta_strike(spot, vix, dte, step, "put")
    T      = max(dte / 365, 0.001)
    sigma  = (vix / 100) * math.sqrt(T)
    r      = 0.065

    sc, lc = call_s["short"], call_s["short"] + wing_call
    sp, lp = put_s["short"],  put_s["short"]  - wing_put

    sc_px = round(bs_price(spot, sc, T, r, sigma, "call"), 1)
    lc_px = round(bs_price(spot, lc, T, r, sigma, "call"), 1)
    sp_px = round(bs_price(spot, sp, T, r, sigma, "put"),  1)
    lp_px = round(bs_price(spot, lp, T, r, sigma, "put"),  1)

    net_per_lot = ((sc_px - lc_px) + (sp_px - lp_px)) * lot
    return {
        "short_call": sc, "long_call": lc,
        "short_put":  sp, "long_put":  lp,
        "sc_entry":   sc_px, "lc_entry": lc_px,
        "sp_entry":   sp_px, "lp_entry": lp_px,
        "call_credit_per_share": round(sc_px - lc_px, 1),
        "put_credit_per_share":  round(sp_px - lp_px, 1),
        "net_credit": round(net_per_lot, 0),
        "profit_target": round(net_per_lot * 0.50, 0),
        "stop_loss":     round(net_per_lot * 2.00, 0),
        "bwb_trigger":   round(net_per_lot * 1.50, 0),
        "call_delta": call_s["delta"],
        "put_delta":  put_s["delta"],
        "dte": dte,
    }


# ── Main service class ────────────────────────────────────────────────────────
class KiteService:
    def __init__(self, api_key: str, api_secret: str, access_token: str = None):
        self.api_key    = api_key
        self.api_secret = api_secret
        self._kite      = None
        if KITE_AVAILABLE and api_key:
            self._kite = KiteConnect(api_key=api_key)
            if access_token:
                self._kite.set_access_token(access_token)

    def generate_session(self, request_token: str) -> Optional[dict]:
        if not self._kite:
            return None
        try:
            session = self._kite.generate_session(request_token, api_secret=self.api_secret)
            self._kite.set_access_token(session["access_token"])
            return session
        except Exception as e:
            print(f"Kite session error: {e}")
            return None

    def get_holdings(self) -> Optional[List[dict]]:
        if not self._kite:
            return self._mock_holdings()
        try:
            return self._kite.holdings()
        except Exception as e:
            print(f"Holdings error: {e}")
            return self._mock_holdings()

    def get_positions(self) -> Optional[dict]:
        if not self._kite:
            return {"net": [], "day": []}
        try:
            return self._kite.positions()
        except Exception as e:
            print(f"Positions error: {e}")
            return {"net": [], "day": []}

    def get_quotes(self, instruments: List[str]) -> Optional[dict]:
        if not self._kite:
            return {}
        try:
            return self._kite.quote(instruments)
        except Exception as e:
            print(f"Quote error: {e}")
            return {}

    def get_option_chain(self, symbol: str, expiry: str) -> Optional[List[dict]]:
        """Try live Kite chain, fall back to synthetic."""
        if not self._kite:
            return None
        try:
            instruments = self._kite.instruments("NFO")
            filtered = [
                i for i in instruments
                if i["name"] == symbol
                and str(i["expiry"]) == expiry
                and i["segment"] == "NFO-OPT"
            ]
            if not filtered:
                return None
            instrument_tokens = [f"NFO:{i['tradingsymbol']}" for i in filtered[:80]]
            quotes = self._kite.quote(instrument_tokens)
            chain_dict: Dict[float, dict] = {}
            for sym, q in quotes.items():
                inst = next((i for i in filtered if f"NFO:{i['tradingsymbol']}" == sym), None)
                if not inst:
                    continue
                K   = float(inst["strike"])
                opt = inst["instrument_type"]  # CE or PE
                if K not in chain_dict:
                    chain_dict[K] = {"strike": K}
                prefix = "call" if opt == "CE" else "put"
                chain_dict[K][f"{prefix}_ltp"]   = q.get("last_price", 0)
                chain_dict[K][f"{prefix}_bid"]   = q.get("depth", {}).get("buy",  [{}])[0].get("price", 0)
                chain_dict[K][f"{prefix}_ask"]   = q.get("depth", {}).get("sell", [{}])[0].get("price", 0)
                chain_dict[K][f"{prefix}_oi"]    = q.get("oi", 0)
                chain_dict[K][f"{prefix}_volume"]= q.get("volume", 0)
                chain_dict[K][f"{prefix}_iv"]    = q.get("implied_volatility", 0)
            return sorted(chain_dict.values(), key=lambda x: x["strike"])
        except Exception as e:
            print(f"Option chain error: {e}")
            return None
    
    """
GTT PATCH — add these methods to the KiteService class in kite_service.py
Paste before the last closing line of the class.
"""

    def place_gtt(self, trigger_type: str, tradingsymbol: str,
                  exchange: str, trigger_values: list,
                  last_price: float, orders: list) -> dict:
        """
        Place a GTT (Good Till Triggered) order on Zerodha.
        trigger_type: "single" or "two-leg" (OCO)
        trigger_values: [price] for single, [stop, target] for two-leg
        orders: list of order dicts with transaction_type, quantity, product, order_type, price
        """
        if not self.kite:
            return {"error": "Kite not connected", "trigger_id": None}
        try:
            result = self.kite.place_gtt(
                trigger_type  = (self.kite.GTT_TYPE_SINGLE if trigger_type=="single"
                                 else self.kite.GTT_TYPE_OCO),
                tradingsymbol = tradingsymbol,
                exchange      = exchange,
                trigger_values= trigger_values,
                last_price    = last_price,
                orders        = orders,
            )
            return {"trigger_id": result, "status": "placed"}
        except Exception as e:
            return {"error": str(e), "trigger_id": None}

    def get_gtts(self) -> list:
        """Get all active GTT orders."""
        if not self.kite:
            return []
        try:
            return self.kite.get_gtts() or []
        except Exception:
            return []

    def delete_gtt(self, trigger_id: int) -> dict:
        """Cancel a GTT order."""
        if not self.kite:
            return {"error": "Kite not connected"}
        try:
            self.kite.delete_gtt(trigger_id)
            return {"message": f"GTT {trigger_id} cancelled"}
        except Exception as e:
            return {"error": str(e)}

    def modify_gtt(self, trigger_id: int, trigger_type: str,
                   tradingsymbol: str, exchange: str,
                   trigger_values: list, last_price: float,
                   orders: list) -> dict:
        """Modify an existing GTT order."""
        if not self.kite:
            return {"error": "Kite not connected"}
        try:
            result = self.kite.modify_gtt(
                trigger_id    = trigger_id,
                trigger_type  = (self.kite.GTT_TYPE_SINGLE if trigger_type=="single"
                                 else self.kite.GTT_TYPE_OCO),
                tradingsymbol = tradingsymbol,
                exchange      = exchange,
                trigger_values= trigger_values,
                last_price    = last_price,
                orders        = orders,
            )
            return {"trigger_id": result, "status": "modified"}
        except Exception as e:
            return {"error": str(e)}

    def _mock_holdings(self) -> List[dict]:
        """Demo data when Kite is not connected."""
        return [
            {"tradingsymbol": "CGPOWER",    "quantity": 149, "average_price": 620.5,  "last_price": 935.1},
            {"tradingsymbol": "HINDCOPPER", "quantity": 40,  "average_price": 252.3,  "last_price": 555.4},
            {"tradingsymbol": "HINDALCO",   "quantity": 22,  "average_price": 608.2,  "last_price": 1149.7},
            {"tradingsymbol": "ANGELONE",   "quantity": 492, "average_price": 247.8,  "last_price": 337.1},
            {"tradingsymbol": "GRANULES",   "quantity": 180, "average_price": 621.5,  "last_price": 783.7},
            {"tradingsymbol": "FINCABLES",  "quantity": 119, "average_price": 993.3,  "last_price": 1177.9},
        ]
