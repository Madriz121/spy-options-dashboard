"""Intraday 5-minute candles via Yahoo Finance v8 chart endpoint (no key needed).

Falls back gracefully: if Yahoo is unreachable the API returns an error and
the UI shows a notice instead of breaking.
"""
import json
import time
import urllib.request
from datetime import datetime, timezone

try:
    from zoneinfo import ZoneInfo
    ET = ZoneInfo("America/New_York")
except Exception:  # pragma: no cover
    ET = timezone.utc

YAHOO_CHART = "https://query2.finance.yahoo.com/v8/finance/chart/{symbol}?interval=5m&range=1d"

_cache: dict = {}  # symbol -> (timestamp, payload)


def fetch_intraday(symbol: str, ttl: int = 300) -> dict:
    symbol = symbol.upper()
    now = time.time()
    hit = _cache.get(symbol)
    if hit and now - hit[0] < ttl:
        return hit[1]
    req = urllib.request.Request(
        YAHOO_CHART.format(symbol=symbol), headers={"User-Agent": "Mozilla/5.0"}
    )
    with urllib.request.urlopen(req, timeout=20) as resp:
        result = json.loads(resp.read())["chart"]["result"][0]

    ts = result.get("timestamp") or []
    quote = (result.get("indicators", {}).get("quote") or [{}])[0]
    opens = quote.get("open") or []
    highs = quote.get("high") or []
    lows = quote.get("low") or []
    closes = quote.get("close") or []
    vols = quote.get("volume") or []

    bars = []
    for i, t in enumerate(ts):
        o, h, l, c = opens[i], highs[i], lows[i], closes[i]
        if None in (o, h, l, c):
            continue
        dt = datetime.fromtimestamp(t, timezone.utc).astimezone(ET)
        bars.append({
            "t": dt.strftime("%H:%M"),
            "o": round(o, 2), "h": round(h, 2),
            "l": round(l, 2), "c": round(c, 2),
            "v": vols[i] or 0,
        })

    session_date = (
        datetime.fromtimestamp(ts[-1], timezone.utc).astimezone(ET).strftime("%Y-%m-%d")
        if bars else None
    )
    payload = {
        "symbol": symbol,
        "interval": "5m",
        "date": session_date,
        "bars": bars,
        "source": "Yahoo Finance (5-min, regular session)",
    }
    _cache[symbol] = (now, payload)
    return payload


def clear_cache() -> None:
    _cache.clear()
