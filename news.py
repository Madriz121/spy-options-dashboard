"""End-of-day market news + keyword sentiment -> bull/bear outlook for tomorrow.

Sources are free RSS feeds (no API key). Sentiment is a transparent
keyword score of headlines — a lean, not a prediction. One big story can
dominate the count, and sarcasm/nuance is invisible to it.
"""
import re
import time
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime

try:
    from zoneinfo import ZoneInfo
    ET_ZONE = ZoneInfo("America/New_York")
except Exception:  # pragma: no cover
    ET_ZONE = timezone.utc

FEEDS = [
    ("CNBC", "https://www.cnbc.com/id/100003114/device/rss/rss.html"),
    ("CNBC Markets", "https://www.cnbc.com/id/10000664/device/rss/rss.html"),
    ("Investing.com", "https://www.investing.com/rss/news.rss"),
    ("Seeking Alpha", "https://seekingalpha.com/market-news.xml"),
]

# word -> weight. Singulars and plurals both listed.
BULL = {
    "rally": 2, "rallies": 2, "surge": 2, "surges": 2, "soar": 2, "soars": 2,
    "jump": 1, "jumps": 1, "gain": 1, "gains": 1, "beat": 2, "beats": 2,
    "strong": 1, "record": 2, "upgrade": 2, "upgrades": 2, "optimism": 1,
    "rebound": 2, "rebounds": 2, "higher": 1, "rise": 1, "rises": 1,
    "bullish": 2, "growth": 1, "climb": 1, "climbs": 1, "advance": 1,
    "advances": 1, "positive": 1, "outperform": 2, "buy": 1, "boom": 2,
    "recovery": 1, "recovers": 1, "up": 0, "win": 1, "wins": 1,
}
BEAR = {
    "plunge": 2, "plunges": 2, "slump": 2, "slumps": 2, "fall": 1, "falls": 1,
    "drop": 1, "drops": 1, "miss": 2, "misses": 2, "weak": 1, "weakness": 1,
    "downgrade": 2, "downgrades": 2, "fear": 1, "fears": 1, "selloff": 2,
    "sell-off": 2, "tumble": 2, "tumbles": 2, "recession": 2, "bearish": 2,
    "decline": 1, "declines": 1, "lower": 1, "slide": 1, "slides": 1,
    "negative": 1, "underperform": 2, "sell": 1, "crash": 2, "crashes": 2,
    "worry": 1, "worries": 1, "concern": 1, "concerns": 1, "risk": 1,
    "risks": 1, "rout": 2, "correction": 1, "volatile": 1, "volatility": 1,
    "layoff": 1, "layoffs": 1, "bankruptcy": 2, "default": 2,
}

_WORD_RE = re.compile(r"[a-z][a-z\-]+")
_cache: dict = {}


def _score(text: str) -> int:
    s = 0
    for w in _WORD_RE.findall(text.lower()):
        if w in BULL:
            s += BULL[w]
        elif w in BEAR:
            s -= BEAR[w]
    return s


def _norm_title(t: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", t.lower()).strip()


def _fetch_feed(source: str, url: str):
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=20) as resp:
        root = ET.fromstring(resp.read())
    out = []
    for it in root.findall(".//item"):
        title = (it.findtext("title") or "").strip()
        link = (it.findtext("link") or "").strip()
        if not title:
            continue
        try:
            dt = parsedate_to_datetime(it.findtext("pubDate") or "")
            if dt is not None and dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
        except (TypeError, ValueError):
            dt = None
        out.append({"title": title, "link": link, "source": source, "dt": dt})
    return out


def fetch_news(ttl: int = 900) -> dict:
    now = time.time()
    hit = _cache.get("news")
    if hit and now - hit[0] < ttl:
        return hit[1]

    items = []
    for source, url in FEEDS:
        try:
            items.extend(_fetch_feed(source, url))
        except Exception:
            continue  # one dead feed must not kill the section
    if not items:
        raise RuntimeError("no news feeds reachable")

    payload = _build(items, cap=40)
    _cache["news"] = (now, payload)
    return payload


def fetch_ticker_news(symbol: str, ttl: int = 900) -> dict:
    """News about one ticker: Google News search + Yahoo quote feed."""
    symbol = symbol.upper()
    now = time.time()
    key = ("ticker", symbol)
    hit = _cache.get(key)
    if hit and now - hit[0] < ttl:
        return hit[1]

    feeds = [
        ("Google News", f"https://news.google.com/rss/search?q={symbol}%20stock&hl=en-US&gl=US&ceid=US:en"),
        ("Yahoo Finance", f"https://feeds.finance.yahoo.com/rss/2.0/headline?s={symbol}&region=US&lang=en-US"),
    ]
    items = []
    for source, url in feeds:
        try:
            items.extend(_fetch_feed(source, url))
        except Exception:
            continue
    if not items:
        raise RuntimeError(f"no news for {symbol}")
    payload = _build(items, cap=25, symbol=symbol)
    _cache[key] = (now, payload)
    return payload


_OCC_RE = re.compile(r"\d{6}[CP]\d{8}")  # option-contract spam in symbol searches


def _build(items: list, cap: int, symbol: str | None = None) -> dict:
    # Today + yesterday (ET): on weekends this keeps Friday's news in view.
    today = datetime.now(ET_ZONE).date()
    recent = [
        it for it in items
        if it["dt"] is not None and (it["dt"].astimezone(ET_ZONE).date() - today).days in (0, -1)
    ]
    pool = recent if len(recent) >= 10 else items
    pool = sorted(pool, key=lambda it: it["dt"] or datetime.min.replace(tzinfo=timezone.utc),
                  reverse=True)

    seen, headlines = set(), []
    for it in pool:
        if symbol and _OCC_RE.search(it["title"]):
            continue  # skip option-quote spam, e.g. "SPY261105C00745000"
        key = _norm_title(it["title"])
        if key in seen:
            continue
        seen.add(key)
        s = _score(it["title"])
        headlines.append({
            "title": it["title"],
            "link": it["link"],
            "source": it["source"],
            "time": it["dt"].astimezone(ET_ZONE).strftime("%a %H:%M") if it["dt"] else "",
            "score": s,
            "sentiment": "bull" if s > 0 else ("bear" if s < 0 else "neutral"),
        })
        if len(headlines) >= cap:
            break

    bull_score = sum(h["score"] for h in headlines if h["score"] > 0)
    bear_score = sum(-h["score"] for h in headlines if h["score"] < 0)
    net = bull_score - bear_score
    counts = {
        "bull": sum(1 for h in headlines if h["sentiment"] == "bull"),
        "bear": sum(1 for h in headlines if h["sentiment"] == "bear"),
        "neutral": sum(1 for h in headlines if h["sentiment"] == "neutral"),
    }
    if net >= 10:
        verdict = "Strongly Bullish"
    elif net >= 5:
        verdict = "Bullish"
    elif net <= -10:
        verdict = "Strongly Bearish"
    elif net <= -5:
        verdict = "Bearish"
    else:
        verdict = "Neutral"

    payload = {
        "verdict": verdict,
        "net": net,
        "bull_score": bull_score,
        "bear_score": bear_score,
        "counts": counts,
        "headline_count": len(headlines),
        "window": "Headlines from today + yesterday (ET)",
        "as_of": datetime.now(ET_ZONE).strftime("%Y-%m-%d %H:%M ET"),
        "method": "Keyword sentiment on headlines — a lean, not a prediction",
        "headlines": headlines,
    }
    if symbol:
        payload["symbol"] = symbol
    return payload


def clear_cache() -> None:
    _cache.clear()
