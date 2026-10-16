"""SPY options dashboard backend: FastAPI serving the UI + /api/gex JSON."""
import os
import time

from fastapi import FastAPI, Query
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

import analytics
import cboe

app = FastAPI(title="SPY Options Dashboard")

BASE = os.path.dirname(os.path.abspath(__file__))
app.mount("/static", StaticFiles(directory=os.path.join(BASE, "static")), name="static")


@app.get("/")
def index():
    return FileResponse(os.path.join(BASE, "static", "index.html"))


@app.get("/health")
def health():
    return {"ok": True, "ts": time.time()}


@app.get("/api/gex")
def gex(
    symbol: str = Query("SPY", max_length=10),
    max_dte: int = Query(9999, ge=0, le=9999),
    refresh: bool = Query(False),
):
    """GEX/DEX profile + key levels. Cached 5 min unless refresh=true."""
    try:
        if refresh:
            cboe.clear_cache()
        payload = cboe.fetch_chain(symbol, ttl=300)
        result = analytics.analyze(payload, max_dte=max_dte)
        result["symbol"] = symbol.upper()
        result["max_dte"] = max_dte
        return JSONResponse(result)
    except Exception as e:  # keep the UI alive with a clear error
        return JSONResponse({"error": f"{type(e).__name__}: {e}"}, status_code=502)
