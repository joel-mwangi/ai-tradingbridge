# TradeBridge AI

A Python research API for descriptive market-tick analysis. It does not place trades or claim a predictive edge.

## Requirements

- Python 3.11 or newer
- A Supabase project containing the `public.market_ticks` table for database-backed analysis

## Local setup

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
pip install -r requirements.txt
```

Create a local `.env` file (never commit it):

```dotenv
TRADEBRIDGE_API_TOKEN=GENERATE_A_LONG_RANDOM_SERVER_TO_SERVER_TOKEN
SUPABASE_URL=https://YOUR_PROJECT.supabase.co
SUPABASE_SERVICE_ROLE_KEY=YOUR_SERVER_ONLY_SERVICE_ROLE_KEY
DERIV_SYMBOL=1HZ100V
```

Generate a strong token locally (do not reuse a password): `python -c "import secrets; print(secrets.token_urlsafe(48))"`.

The analysis endpoints require `Authorization: Bearer <TRADEBRIDGE_API_TOKEN>`. The token must be stored only in server-side environment variables and supplied by a trusted backend; never embed it in browser JavaScript or a public mobile app. If a browser client needs the API, route requests through an authenticated TradeBridge server-side endpoint. The service returns 503 for analysis requests if the token is not configured, rather than running unauthenticated.

The Supabase service-role key bypasses Row Level Security. Keep it only in server-side environment variables or deployment secret settings; never put it in browser code or expose it in API responses. Prefer a dedicated, least-privilege database access design when practical.

## Run and test

```powershell
python -m pytest -q
uvicorn app.main:app --host 127.0.0.1 --port 8000
```

Open `/docs` for interactive API documentation. Supply the bearer token in the Authorize dialog to call analysis endpoints.

## Endpoints

- `GET /health` — public liveness only; does not reveal secret configuration.
- `POST /analysis/digits` — authenticated descriptive analysis of a supplied array of digits.
- `GET /analysis/digits/latest?symbol=1HZ100V&limit=1000` — authenticated query of stored ticks from `public.market_ticks`; accepted limits are 10–10,000 rows.

The API bounds request size, database result count, and upstream request timeout. Configure additional rate limiting and request-body limits at the API gateway or deployment platform; do not rely on in-process limits for a serverless or multi-worker deployment.

## Architecture

Deriv WebSocket -> Cloudflare tick Worker -> authenticated TradeBridge ingestion API -> Supabase -> this research API -> TradeBridge UI.

Use the Cloudflare Worker as the primary collector. The local CSV collector is optional for isolated testing; do not run redundant collectors unless you deliberately need a second dataset.

## Interpretation

Frequency summaries and chi-square statistics are descriptive diagnostics, not trading signals. They do not prove future outcomes are predictable. Strategy claims require pre-registered hypotheses, adequate sample sizes, chronological train/validation/test splits, and realistic payout/cost modelling.
