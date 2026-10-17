# TradeBridge AI

A Python research API for descriptive market-tick analysis. It does not place trades or claim a predictive edge.

## Requirements

- Python 3.11 or newer
- An always-on ASGI host for Deriv WebSocket sessions (not a serverless function runtime)
- Supabase Auth and the `deriv_account_links` table for authenticated account ownership checks

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

TradeBridge browser -> authenticated FastAPI HTTP/WebSocket gateway -> Deriv OAuth/Options APIs. Live market ticks and history are forwarded in memory only and are not written to the database. Legacy analysis endpoints remain separate from this migration and may read historical rows that already exist in `public.market_ticks`.

## Interpretation

Frequency summaries and chi-square statistics are descriptive diagnostics, not trading signals. They do not prove future outcomes are predictable. Strategy claims require pre-registered hypotheses, adequate sample sizes, chronological train/validation/test splits, and realistic payout/cost modelling.

## FastAPI-owned Deriv gateway

This service can own Deriv OAuth code exchange, in-memory credential custody, account lookup, Demo-only authenticated trading WebSockets, and ephemeral public market-data WebSockets.

Configure these server-only environment variables before enabling the frontend migration:

```dotenv
DERIV_OAUTH_CLIENT_ID=YOUR_DERIV_OAUTH_CLIENT_ID
DERIV_OAUTH_REDIRECT_URI=https://YOUR_FRONTEND_HOST/api/auth/deriv/callback
TRADEBRIDGE_SESSION_BRIDGE_TOKEN=LONG_RANDOM_SHARED_SECRET
TRADEBRIDGE_CREDENTIAL_ENCRYPTION_KEY=FERNET_KEY_GENERATED_LOCALLY
TRADEBRIDGE_ALLOWED_ORIGINS=https://YOUR_FRONTEND_HOST,http://localhost:3000
SUPABASE_URL=https://YOUR_PROJECT.supabase.co
SUPABASE_ANON_KEY=YOUR_SUPABASE_PUBLISHABLE_OR_ANON_KEY
SUPABASE_SERVICE_ROLE_KEY=YOUR_SERVER_ONLY_SERVICE_ROLE_KEY
TRADEBRIDGE_DEMO_MAX_STAKE=1000
```

The `TRADEBRIDGE_SESSION_BRIDGE_TOKEN` must match the server-only value configured in Next.js. Never expose it through `NEXT_PUBLIC_*`. The OAuth redirect URI must exactly match the callback URL registered with Deriv. Account lookup fails closed unless it can verify the account IDs against `public.deriv_account_links`.

- `POST /deriv/internal/oauth/exchange` — trusted Next.js callback sends the OAuth code and PKCE verifier; FastAPI exchanges them and keeps the access token server-side.
- `POST /deriv/accounts` — authenticated, ownership-filtered account list.
- `GET /deriv/session` and `DELETE /deriv/session` — authenticated safe session status and disconnect.
- `POST /deriv/ws-ticket` — issue a 30-second single-use WebSocket ticket after Supabase session validation.
- `WS /deriv/ws?account_id=...` — authenticated Demo-only Options WebSocket proxy with server-side stake, contract, duration, and sell-request validation.
- `WS /deriv/live/market` — public market-data WebSocket proxy; no live tick persistence.

Deriv OAuth credentials are encrypted at rest in `public.deriv_backend_credentials`; the Fernet key must be generated locally and stored only in the backend's secret environment, separate from Supabase. Active WebSocket connections still close on process restart and clients must reconnect. Keep a single ASGI worker/replica until one-time WebSocket tickets are backed by a shared store; otherwise a ticket issued on one worker may arrive at another worker. The credential table migration must be applied before enabling the backend. Real-account execution is explicitly blocked. Treat this as a migration candidate until automated tests and the end-to-end Deriv workflow are verified.

## Bot execution status

The standalone bot worker and its worker-specific tests have been removed from this repository. Bot execution is therefore not operational through a background worker. The bot-control API may still contain routes that depend on a worker heartbeat; those routes should fail closed until bot execution is redesigned so **FastAPI is the only component that communicates with Deriv**. Do not start or enable automated bot trading, and keep Real-account automation disabled. The worker removal does not delete database tables or change the Supabase schema.
