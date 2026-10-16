"""Fetch options chains from CBOE's free delayed-quotes endpoint.

No API key required. Data is ~15 minutes delayed; open interest is from the
prior close (updates overnight). Yahoo Finance is intentionally NOT used:
it is blocked from some networks and needs cookie/crumb auth.
"""
import json
import time
import urllib.request

CBOE_URL = "https://cdn.cboe.com/api/global/delayed_quotes/options/{symbol}.json"
USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"

_cache: dict = {}  # symbol -> (timestamp, payload)


def fetch_chain(symbol: str, ttl: int = 300) -> dict:
    """Return the raw CBOE payload for *symbol* (upper-cased), cached for *ttl* seconds."""
    symbol = symbol.upper()
    now = time.time()
    hit = _cache.get(symbol)
    if hit and now - hit[0] < ttl:
        return hit[1]
    req = urllib.request.Request(
        CBOE_URL.format(symbol=symbol), headers={"User-Agent": USER_AGENT}
    )
    with urllib.request.urlopen(req, timeout=25) as resp:
        payload = json.loads(resp.read())["data"]
    payload["_fetched_at"] = now
    _cache[symbol] = (now, payload)
    return payload


def clear_cache() -> None:
    _cache.clear()
