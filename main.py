"""SPY options dashboard backend: FastAPI serving the UI + /api/gex JSON."""
import os
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime

from fastapi import FastAPI, Query
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

import analytics
import cboe
import news as news_mod
import prices

app = FastAPI(title="SPY Options Dashboard")

BASE = os.path.dirname(os.path.abspath(__file__))
app.mount("/static", StaticFiles(directory=os.path.join(BASE, "static")), name="static")


@app.get("/")
def index():
    return FileResponse(os.path.join(BASE, "static", "index.html"))


@app.get("/health")
def health():
    return {"ok": True, "ts": time.time()}


@app.get("/api/intraday")
def intraday(
    symbol: str = Query("SPY", max_length=10),
    refresh: bool = Query(False),
):
    """5-minute candles for the current/last session."""
    try:
        if refresh:
            prices.clear_cache()
        return JSONResponse(prices.fetch_intraday(symbol, ttl=300))
    except Exception as e:
        return JSONResponse({"error": f"{type(e).__name__}: {e}"}, status_code=502)


@app.get("/api/news")
def news(
    symbol: str = Query("SPY", max_length=10),
    refresh: bool = Query(False),
):
    """EOD market news + keyword sentiment -> tomorrow's bull/bear lean.
    Includes a per-ticker section for the viewed symbol."""
    try:
        if refresh:
            news_mod.clear_cache()
        payload = news_mod.fetch_news(ttl=900)
        try:
            payload["ticker"] = news_mod.fetch_ticker_news(symbol, ttl=900)
        except Exception:
            payload["ticker"] = None
        return JSONResponse(payload)
    except Exception as e:  # keep the UI alive with a clear error
        return JSONResponse({"error": f"{type(e).__name__}: {e}"}, status_code=502)


@app.get("/api/gex")
def gex(
    symbol: str = Query("SPY", max_length=10),
    max_dte: int = Query(9999, ge=0, le=9999),
    expiry: str | None = Query(None, description="Single expiry YYYY-MM-DD"),
    fridays: bool = Query(False, description="Only Friday expiries"),
    refresh: bool = Query(False),
):
    """GEX/DEX profile + key levels. Cached 5 min unless refresh=true."""
    try:
        if refresh:
            cboe.clear_cache()
        payload = cboe.fetch_chain(symbol, ttl=300)
        result = analytics.analyze(payload, max_dte=max_dte, only_expiry=expiry,
                                   only_fridays=fridays)
        result["symbol"] = symbol.upper()
        result["max_dte"] = max_dte
        result["expiry"] = expiry
        result["fridays"] = fridays
        # Ship only strikes near the money: every consumer (charts ±12%,
        # heatmap ±10%) works inside ±15%. Levels/totals are already computed
        # from the full set above, so the numbers don't change — the JSON
        # just gets much smaller (TSLA full chain: 237 -> 45 strikes).
        spot = result["spot"]
        result["strikes"] = [s for s in result["strikes"]
                             if abs(s["strike"] - spot) / spot <= 0.15]
        return JSONResponse(result)
    except Exception as e:  # keep the UI alive with a clear error
        return JSONResponse({"error": f"{type(e).__name__}: {e}"}, status_code=502)


MATRIX_TICKERS = ["SPY", "QQQ", "IWM", "DIA", "TSLA", "NVDA"]


def _expirations(symbol: str, payload: dict) -> list:
    """Sorted [{date, dte}] from a raw CBOE payload, without full analysis.
    Cached 1h: the expiry list only changes when new weeklies are listed."""
    now = time.time()
    hit = _exp_cache.get(symbol)
    if hit and now - hit[0] < 3600:
        return hit[1]
    today = analytics._et_now().date()
    seen: dict = {}
    for c in payload.get("options", []):
        parsed = analytics.parse_option_symbol(c.get("option"))
        if not parsed:
            continue
        expiry, _, _ = parsed
        dte = (expiry - today).days
        if dte >= 0:
            seen[str(expiry)] = dte
    exp_list = [{"date": d, "dte": seen[d]} for d in sorted(seen)]
    _exp_cache[symbol] = (now, exp_list)
    return exp_list


_exp_cache: dict = {}  # symbol -> (timestamp, exp_list)


def _pick_matrix_expiry(exp_list: list, expiry: str | None, fridays: bool) -> str:
    """Same rule the frontend used: exact single expiry if the ticker has
    it (else its next expiry after that date), nearest Friday in Fridays
    mode, otherwise the ticker's nearest expiry."""
    if expiry:
        hit = next((e for e in exp_list if e["date"] == expiry), None)
        if hit:
            return hit["date"]
        after = next((e for e in exp_list if e["date"] > expiry), None)
        return (after or exp_list[-1])["date"]
    if fridays:
        fri = next((e for e in exp_list
                    if e["dte"] <= 35
                    and datetime.strptime(e["date"], "%Y-%m-%d").weekday() == 4), None)
        if fri:
            return fri["date"]
    return exp_list[0]["date"]


def _matrix_panel(t: str, expiry: str | None, fridays: bool) -> dict:
    try:
        payload = cboe.fetch_chain(t, ttl=300)
        exp_list = _expirations(t, payload)
        if not exp_list:
            return {"symbol": t, "error": True}
        exp = _pick_matrix_expiry(exp_list, expiry, fridays)
        r = analytics.analyze(payload, only_expiry=exp)
        spot = r["spot"]
        band = [s for s in r["strikes"]
                if abs(s["strike"] - spot) / spot <= 0.04 and s["net_gex"] != 0]
        band.sort(key=lambda s: -s["strike"])
        return {
            "symbol": t,
            "spot": spot,
            "expiry": exp,
            "net_gex_bn": r["totals"]["net_gex_bn"],
            "strikes": [{"strike": s["strike"], "net_gex": round(s["net_gex"], 1)}
                        for s in band],
        }
    except Exception:
        return {"symbol": t, "error": True}


@app.get("/api/matrix")
def matrix(
    expiry: str | None = Query(None, description="Single expiry YYYY-MM-DD"),
    fridays: bool = Query(False, description="Nearest Friday expiry per ticker"),
    refresh: bool = Query(False),
):
    """All six Matrix panels in one call. The six chains are fetched in
    parallel server-side and each panel is pre-trimmed to ±4% of spot —
    replaces the 12 separate /api/gex calls the tab used to make."""
    try:
        if refresh:
            cboe.clear_cache()
            _exp_cache.clear()
        with ThreadPoolExecutor(max_workers=6) as pool:
            panels = list(pool.map(
                lambda t: _matrix_panel(t, expiry, fridays), MATRIX_TICKERS))
        return JSONResponse({"panels": panels})
    except Exception as e:
        return JSONResponse({"error": f"{type(e).__name__}: {e}"}, status_code=502)
