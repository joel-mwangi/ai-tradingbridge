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


## Demo bot execution (implementation candidate; deployment not verified)

- `app/bot_state.py` defines legal durable trade transitions.
- `app/bot_control.py` exposes authenticated `/bots/runs` create/list/detail/stop endpoints. Run creation fails closed unless the worker heartbeat is fresh and the linked Deriv account is verified as Demo.
- `app/bot_worker.py` is the separately runnable persistent worker: `python -m app.bot_worker`. It claims queued runs, persists the trade ledger before side effects, requests proposals, buys only after validation, monitors settlement, and records broker-confirmed payout/profit.
- `migrations/20261017010000_bot_execution_ledger.sql` creates run, trade, and worker-heartbeat tables with RLS enabled and no browser write policies.
- Ambiguous buy acknowledgements become `outcome_unknown` / `reconciliation_required`; the worker does not retry that purchase. A known contract ID can be re-monitored after a restart.

This code is still a PR candidate, not a verified production service. The migration has not been confirmed applied to the correct TradeBridge Supabase project, the worker has not been deployed to a persistent host, and a live controlled Demo lifecycle has not passed. Do not treat API acceptance or CI as proof of real broker execution. Real-account automated execution must remain disabled.


## Durable Demo bot execution (separate worker)

Automated bot runs are server-authoritative and Demo-only. The FastAPI control API stores run commands in `bot_runs`; a separate persistent process consumes them and writes each broker interaction to `bot_trades`. The worker must run on a long-lived host, not inside a Vercel request handler.

Start the worker with:

```bash
python -m app.bot_worker
```

Configure `SUPABASE_URL`, `SUPABASE_SERVICE_ROLE_KEY`, and the same `TRADEBRIDGE_CREDENTIAL_ENCRYPTION_KEY` used by FastAPI. The worker obtains an account-scoped Demo Options WebSocket URL from Deriv using the stored server-side OAuth credential. It does not accept Real accounts and it rechecks that the account remains linked to the user before execution.

The API will not queue a new run unless `bot_worker_heartbeats` has a recent heartbeat. If a buy request may have reached Deriv but its acknowledgement is missing, the worker records `outcome_unknown` and never resubmits that purchase. Known contract IDs can be re-monitored after transient disconnects; repeated monitoring failures or unknown purchase outcomes remain locked for reconciliation.

Apply `migrations/20261017010000_bot_execution_ledger.sql` only to the confirmed TradeBridge Supabase project. Do not apply it to Azimart. Before enabling the UI in a deployed environment, run the Python test suite and controlled Demo end-to-end tests for proposal/buy, uncertain acknowledgement, disconnect, settlement, stop, and worker restart. Real-account automated execution remains disabled.
