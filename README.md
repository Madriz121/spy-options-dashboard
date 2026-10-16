# SPY Options Dashboard

Live gamma-exposure (GEX), delta-exposure (DEX), volume / open-interest, and
key levels for SPY options — call wall, put wall, gamma flip, max pain.

## How it works

- Data: CBOE free delayed-quotes endpoint (`cdn.cboe.com`), ~15 min delayed,
  no API key. Open interest is from the prior close.
- Backend (`main.py` + `cboe.py` + `analytics.py`): FastAPI. Fetches the full
  SPY chain, computes per-strike GEX/DEX with the SpotGamma convention
  (`GEX = gamma × OI × 100 × spot² × 0.01`, calls positive / puts negative),
  and derives key levels. Responses cached 5 minutes.
- Frontend (`static/index.html`): single-page dashboard (ECharts) with
  expiry filter (0DTE / ≤7 DTE / all), auto-refresh every 5 min.

## Run locally

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
.venv/bin/uvicorn main:app --host 0.0.0.0 --port 8000
```

## Deploy to Render (free)

The repo ships with `render.yaml`. In Render: New → Web Service → connect this
repo. Build command `pip install -r requirements.txt`, start command
`uvicorn main:app --host 0.0.0.0 --port $PORT`. Free tier sleeps after
15 min idle; first visit wakes it in ~30s.

## API

- `GET /api/gex?symbol=SPY&max_dte=7` — full profile + levels + totals
- `GET /api/gex?symbol=SPY&max_dte=7&refresh=true` — bypass cache
- `GET /health`

Educational tool, not investment advice.
