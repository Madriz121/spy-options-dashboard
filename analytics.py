"""GEX / DEX analytics over a CBOE options chain.

Conventions (SpotGamma standard):
    GEX per contract = gamma * open_interest * 100 * spot^2 * 0.01
    calls contribute positive, puts contribute negative
    DEX per contract = delta * open_interest * 100 * spot
    calls contribute positive, puts contribute negative

GEX answers: for a 1% move in spot, how many dollars of delta must dealers
trade to re-hedge at this strike. Positive aggregate GEX = dealers long gamma
(stabilizing, mean-reverting). Negative = dealers short gamma (destabilizing,
trend-amplifying).
"""
import re
from datetime import datetime, timezone
from math import erf, exp, log, pi, sqrt

try:
    from zoneinfo import ZoneInfo
    ET = ZoneInfo("America/New_York")
except Exception:  # pragma: no cover
    ET = timezone.utc

RISK_FREE = 0.04  # approx 3m T-bill; gamma is insensitive to small r moves

_SYMBOL_RE = re.compile(r"^([A-Z]+)(\d{6})([CP])(\d{8})$")


def _norm_pdf(x: float) -> float:
    return exp(-0.5 * x * x) / sqrt(2 * pi)


def bs_gamma(spot: float, strike: float, t_years: float, sigma: float,
             r: float = RISK_FREE) -> float:
    """Black-Scholes gamma per share, per $1 move in the underlying."""
    if spot <= 0 or strike <= 0 or t_years <= 0 or sigma <= 0:
        return 0.0
    d1 = (log(spot / strike) + (r + 0.5 * sigma * sigma) * t_years) / (
        sigma * sqrt(t_years)
    )
    return _norm_pdf(d1) / (spot * sigma * sqrt(t_years))


def parse_option_symbol(sym: str):
    """'SPY261016C00300000' -> (expiry_date, is_call, strike)."""
    m = _SYMBOL_RE.match(sym or "")
    if not m:
        return None
    yymmdd, cp, strike_raw = m.group(2), m.group(3), m.group(4)
    expiry = datetime.strptime(yymmdd, "%y%m%d").date()
    return expiry, cp == "C", int(strike_raw) / 1000.0


def _et_now():
    return datetime.now(ET)


def dte_of(expiry) -> int:
    """Whole calendar days from today (ET) to expiry; 0 = expires today."""
    return (expiry - _et_now().date()).days


def _t_years(expiry) -> float:
    """Time to 4pm ET on expiry day, in years (sub-day precision for 0DTE)."""
    now = _et_now()
    expiry_close = datetime(expiry.year, expiry.month, expiry.day, 16, 0, tzinfo=ET)
    secs = (expiry_close - now).total_seconds()
    return max(secs, 60) / (365.0 * 24 * 3600)


def analyze(payload: dict, max_dte: int = 9999) -> dict:
    spot = float(payload.get("current_price") or 0)
    if spot <= 0:
        raise ValueError("no spot price in CBOE payload")

    strikes: dict = {}
    expirations: dict = {}
    latest_trade = None
    # (expiry, strike, is_call) -> (bid, ask, theo, last) for expected-move calc
    quotes: dict = {}

    for c in payload.get("options", []):
        parsed = parse_option_symbol(c.get("option"))
        if not parsed:
            continue
        expiry, is_call, strike = parsed
        dte = dte_of(expiry)
        if dte < 0 or dte > max_dte:
            continue
        expirations[str(expiry)] = dte

        oi = float(c.get("open_interest") or 0)
        vol = float(c.get("volume") or 0)
        if oi <= 0 and vol <= 0:
            continue
        lt = c.get("last_trade_time")
        if lt and (latest_trade is None or lt > latest_trade):
            latest_trade = lt

        gamma = c.get("gamma")
        try:
            gamma = float(gamma)
        except (TypeError, ValueError):
            gamma = 0.0
        if gamma == 0.0 and oi > 0:
            # CBOE zeroes greeks on some deep ITM/illiquid lines; fall back to BS w/ IV
            try:
                iv = float(c.get("iv") or 0)
            except (TypeError, ValueError):
                iv = 0.0
            if iv > 0:
                gamma = bs_gamma(spot, strike, _t_years(expiry), iv)

        try:
            delta = float(c.get("delta") or 0)
        except (TypeError, ValueError):
            delta = 0.0

        sign = 1.0 if is_call else -1.0
        gex = sign * gamma * oi * 100 * spot * spot * 0.01
        dex = sign * delta * oi * 100 * spot

        try:
            bid = float(c.get("bid") or 0)
            ask = float(c.get("ask") or 0)
        except (TypeError, ValueError):
            bid = ask = 0.0
        try:
            theo = float(c.get("theo") or 0)
        except (TypeError, ValueError):
            theo = 0.0
        try:
            last = float(c.get("last_trade_price") or 0)
        except (TypeError, ValueError):
            last = 0.0
        quotes[(str(expiry), strike, is_call)] = (bid, ask, theo, last)

        s = strikes.setdefault(strike, {
            "strike": strike, "call_gex": 0.0, "put_gex": 0.0,
            "call_dex": 0.0, "put_dex": 0.0,
            "call_oi": 0.0, "put_oi": 0.0,
            "call_vol": 0.0, "put_vol": 0.0,
        })
        if is_call:
            s["call_gex"] += gex
            s["call_dex"] += dex
            s["call_oi"] += oi
            s["call_vol"] += vol
        else:
            s["put_gex"] += gex   # already negative via sign
            s["put_dex"] += dex
            s["put_oi"] += oi
            s["put_vol"] += vol

    rows = []
    for strike in sorted(strikes):
        s = strikes[strike]
        s["net_gex"] = s["call_gex"] + s["put_gex"]
        s["net_dex"] = s["call_dex"] + s["put_dex"]
        s["total_oi"] = s["call_oi"] + s["put_oi"]
        s["total_vol"] = s["call_vol"] + s["put_vol"]
        rows.append(s)

    # cumulative net GEX from the bottom up (for the gamma-flip crossing)
    cum = 0.0
    for s in rows:
        cum += s["net_gex"]
        s["cum_gex"] = cum

    levels = _key_levels(rows, spot)
    totals = _totals(rows)
    exp_move = _expected_move(quotes, expirations, spot)
    unusual = _unusual_volume(rows)

    return {
        "spot": round(spot, 2),
        "as_of": latest_trade,
        "fetched_at": payload.get("_fetched_at"),
        "data_delay_note": "CBOE delayed quotes (~15 min); open interest from prior close",
        "strikes": rows,
        "levels": levels,
        "totals": totals,
        "expected_move": exp_move,
        "unusual_volume": unusual,
        "expirations": [
            {"date": d, "dte": expirations[d]} for d in sorted(expirations)
        ],
    }


def _mid(bid: float, ask: float, theo: float, last: float) -> float:
    if bid > 0 and ask > 0:
        return (bid + ask) / 2
    return theo or last or 0.0


def _expected_move(quotes: dict, expirations: dict, spot: float):
    """ATM straddle mid-price for the nearest *live* expiry -> dollar expected move."""
    if not expirations or spot <= 0:
        return None
    now = _et_now()
    live = {
        d: dte for d, dte in expirations.items()
        if dte > 0 or (dte == 0 and now.hour < 16)
    }
    if not live:
        return None
    near = min(live, key=lambda d: live[d])
    strikes = sorted({k for (d, k, _) in quotes if d == near})
    if not strikes:
        return None
    atm = min(strikes, key=lambda k: abs(k - spot))
    call = quotes.get((near, atm, True))
    put = quotes.get((near, atm, False))
    if not call or not put:
        return None
    em = _mid(*call) + _mid(*put)
    if em <= 0:
        return None
    return {
        "dollars": round(em, 2),
        "pct": round(em / spot * 100, 2),
        "expiry": near,
        "strike": atm,
        "upper": round(spot + em, 2),
        "lower": round(spot - em, 2),
    }


def _unusual_volume(rows: list) -> list:
    """Strikes where today's volume >= 2x open interest (min 500 contracts)."""
    hot = []
    for s in rows:
        if s["total_vol"] >= 500 and s["total_oi"] > 0:
            ratio = s["total_vol"] / s["total_oi"]
            if ratio >= 2.0:
                hot.append({
                    "strike": s["strike"],
                    "volume": int(s["total_vol"]),
                    "oi": int(s["total_oi"]),
                    "ratio": round(ratio, 1),
                })
    hot.sort(key=lambda h: h["ratio"], reverse=True)
    return hot[:8]


def _key_levels(rows: list, spot: float) -> dict:
    if not rows:
        return {}
    # Key levels are only meaningful near the money: far-OTM strikes with
    # stale/structural OI (e.g. deep puts) would otherwise dominate the flip
    # and top-OI lists. Restrict to a ±15% band around spot.
    band = [s for s in rows if abs(s["strike"] - spot) / spot <= 0.15]
    if not band:
        band = rows
    call_wall = max(band, key=lambda s: s["call_gex"])
    put_wall = min(band, key=lambda s: s["put_gex"])  # most negative

    # gamma flip: zero crossing of cumulative net GEX closest to spot
    flip = None
    best_dist = float("inf")
    for a, b in zip(band, band[1:]):
        if a["cum_gex"] == 0:
            cand = a["strike"]
        elif a["cum_gex"] * b["cum_gex"] < 0:
            cand = (a["strike"] + b["strike"]) / 2
        else:
            continue
        dist = abs(cand - spot)
        if dist < best_dist:
            best_dist, flip = dist, cand

    # max pain: strike minimizing total option-holder payout at expiry
    pain = None
    best_pain = float("inf")
    for s in band:
        k = s["strike"]
        payout = 0.0
        for t in band:
            payout += t["call_oi"] * max(0.0, k - t["strike"])
            payout += t["put_oi"] * max(0.0, t["strike"] - k)
        if payout < best_pain:
            best_pain, pain = payout, k

    top_oi = sorted(band, key=lambda s: s["total_oi"], reverse=True)[:3]
    top_vol = sorted(band, key=lambda s: s["total_vol"], reverse=True)[:5]

    return {
        "call_wall": call_wall["strike"],
        "call_wall_gex": round(call_wall["call_gex"] / 1e9, 3),
        "put_wall": put_wall["strike"],
        "put_wall_gex": round(put_wall["put_gex"] / 1e9, 3),
        "gamma_flip": round(flip, 2) if flip is not None else None,
        "max_pain": pain,
        "top_oi": [t["strike"] for t in top_oi],
        "top_vol": [t["strike"] for t in top_vol],
    }


def _totals(rows: list) -> dict:
    net_gex = sum(s["net_gex"] for s in rows)
    net_dex = sum(s["net_dex"] for s in rows)
    call_vol = sum(s["call_vol"] for s in rows)
    put_vol = sum(s["put_vol"] for s in rows)
    call_oi = sum(s["call_oi"] for s in rows)
    put_oi = sum(s["put_oi"] for s in rows)
    return {
        "net_gex_bn": round(net_gex / 1e9, 3),
        "net_dex_bn": round(net_dex / 1e9, 3),
        "put_call_vol": round(put_vol / call_vol, 3) if call_vol else None,
        "put_call_oi": round(put_oi / call_oi, 3) if call_oi else None,
        "regime": "positive gamma" if net_gex > 0 else "negative gamma",
        "strikes_count": len(rows),
    }
