# TradeBridge AI / Live Data Service

FastAPI provides a direct, ephemeral Deriv tick relay for TradeBridge. The live relay does not run AI, place trades, or write tick data to Supabase. Existing historical-analysis endpoints remain separate and unchanged.

## Live tick feed

- `GET /health` — liveness endpoint.
- `WS /live/ticks?symbol=1HZ100V` — subscribes to the requested public Deriv market and forwards ticks as they arrive.
- Supported symbols must match `^[A-Za-z0-9_]{2,32}$`; the browser UI offers the Volatility 10/25/50/75/100 (1s) markets.
- Live messages include `type: "tick"`, `symbol`, `quote`, `epoch`, and `pip_size`. Status/error messages are also sent.
- This endpoint has no database writes, cache, replay, or historical buffer. Data received during a disconnect is not replayed.
- The browser should reconnect with backoff after a closed connection. The current implementation opens an upstream Deriv subscription per connected browser client; it does not yet multiplex clients over one shared Deriv connection.

### Configuration

Set `DERIV_APP_ID` as a server-side environment variable on the FastAPI host. It must be a numeric Deriv application ID. Do not expose secrets in browser variables.

For the TradeBridge UI, set `NEXT_PUBLIC_TRADEBRIDGE_LIVE_WS_URL` to the full deployed endpoint, for example `wss://YOUR-ALWAYS-ON-HOST/live/ticks`. Use `wss://` in production. This URL is public configuration, not a secret.

**Hosting requirement:** run this FastAPI app on a host that supports long-lived ASGI processes and WebSocket connections. A serverless deployment that suspends processes is not suitable for a persistent collector/relay. Configure TLS, connection limits, origin controls, and rate limiting at the hosting/reverse-proxy layer before production exposure.

## Local setup

Requires Python 3.11+.

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
pip install -r requirements.txt
$env:DERIV_APP_ID="YOUR_DERIV_APP_ID"
uvicorn app.main:app --host 127.0.0.1 --port 8000
```

The live feed is available at `ws://127.0.0.1:8000/live/ticks?symbol=1HZ100V`. The endpoint requires a Deriv application ID configuration but does not require a trading-account token because it only relays public market ticks.

## Existing research endpoints

- `POST /analysis/digits` — authenticated descriptive analysis of a supplied digit array.
- `GET /analysis/digits/latest?symbol=1HZ100V&limit=1000` — authenticated historical analysis from `public.market_ticks`. This legacy endpoint still requires Supabase configuration; the live WebSocket endpoint does not call it or write to that table.

Set `TRADEBRIDGE_API_TOKEN` for those HTTP analysis endpoints. If using historical analysis, also set `SUPABASE_URL` and `SUPABASE_SERVICE_ROLE_KEY` server-side. Never expose these values in browser code.

## Tests

```powershell
python -m pytest -q
```

The live feed only relays current market ticks. It does not place orders, predict outcomes, or establish a trading edge.
