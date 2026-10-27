# TradeBridge AI

A Python research API and authenticated Deriv session gateway for Demo and Real Options trading.
Supports authenticated Deriv WebSocket sessions, OTP verification, and server-side risk limits for both demo and real accounts.

## Requirements

- Python 3.11 or newer
- An always-on ASGI host with outbound HTTPS and WebSocket access to `api.derivws.com`
  (not a serverless function runtime — active WebSocket connections close on process restart)
- A Supabase project with the canonical migrations from the [TradeBridge repository](https://github.com/joel-mwangi/tradebridge/tree/main/supabase/migrations) applied

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
SUPABASE_ANON_KEY=YOUR_SUPABASE_PUBLISHABLE_OR_ANON_KEY
SUPABASE_SERVICE_ROLE_KEY=YOUR_SERVER_ONLY_SERVICE_ROLE_KEY
DERIV_OAUTH_CLIENT_ID=YOUR_DERIV_OAUTH_CLIENT_ID
DERIV_OAUTH_REDIRECT_URI=https://YOUR_FRONTEND_HOST/api/auth/deriv/callback
TRADEBRIDGE_SESSION_BRIDGE_TOKEN=LONG_RANDOM_SHARED_SECRET
TRADEBRIDGE_CREDENTIAL_ENCRYPTION_KEY=FERNET_KEY_GENERATED_LOCALLY
TRADEBRIDGE_ALLOWED_ORIGINS=https://YOUR_FRONTEND_HOST,http://localhost:3000
TRADEBRIDGE_DEMO_MAX_STAKE=1000
# Real trading defaults to disabled; configure a cap before enabling it.
# TRADEBRIDGE_REAL_TRADING_ENABLED=true
# TRADEBRIDGE_REAL_MAX_STAKE=YOUR_CONSERVATIVE_MAX_STAKE
DERIV_SYMBOL=1HZ100V
```

Generate a strong token: `python -c "import secrets; print(secrets.token_urlsafe(48))"`.

Generate a Fernet key: `python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"`.

The `TRADEBRIDGE_SESSION_BRIDGE_TOKEN` must match the server-only value in Next.js. Never
expose it through `NEXT_PUBLIC_*`. The service-role key bypasses Supabase RLS — keep it
only in server-side environment variables.

## Supabase migrations

**TradeBridge is the single source of truth for shared Supabase schema migrations.**
Do not create or apply duplicate schema migrations from this backend repository.

Review and apply the required migrations from
[TradeBridge `supabase/migrations`](https://github.com/joel-mwangi/tradebridge/tree/main/supabase/migrations)
to the intended Supabase project using the project's established migration workflow.
Confirm the target project and migration status before applying changes. The backend
expects the canonical `public.deriv_backend_credentials` and
`public.deriv_account_links` tables to exist with the schema defined by TradeBridge.

## Run and test

```powershell
python -m pytest -q
uvicorn app.main:app --host 127.0.0.1 --port 8000
```

Open `/docs` for interactive API documentation. Supply the bearer token in the Authorize
dialog to call analysis endpoints.

## Endpoints

### Public

- `GET /health` — liveness check; does not reveal secret configuration.

### Analysis (server-to-server bearer auth via `TRADEBRIDGE_API_TOKEN`)

- `POST /analysis/digits` — descriptive analysis of a supplied digit array.
- `GET /analysis/digits/latest?symbol=1HZ100V&limit=1000` — analysis of stored ticks
  from `public.market_ticks` (populated externally).

### Deriv session gateway (Supabase JWT auth)

- `POST /deriv/internal/oauth/exchange` — trusted Next.js callback: exchanges OAuth
  code + PKCE verifier; FastAPI stores the encrypted token and writes `deriv_account_links`.
- `POST /deriv/internal/credentials` — legacy credential transfer (token only; does not
  write account links — use the OAuth exchange endpoint instead).
- `GET /deriv/session` — safe session status; never returns credentials.
- `DELETE /deriv/session` — disconnect; removes credential and account links.
- `POST /deriv/accounts` — ownership-filtered account list.
- `POST /deriv/ws-ticket` — issue a 30-second single-use WebSocket ticket.
- `WS /deriv/ws?account_id=...` — authenticated Demo and Real trading proxy.
- `WS /deriv/live/market` — public market-data proxy; no persistence.

## Architecture

```
Browser / Next.js
  ├─ OAuth callback → POST /deriv/internal/oauth/exchange
  │    └─ FastAPI exchanges PKCE code, encrypts token, stores credential/account links
  ├─ POST /deriv/ws-ticket → short-lived, single-use WebSocket ticket
  ├─ WS /deriv/ws?account_id=... → authenticated Demo/Real trading and order execution
  └─ WS /deriv/live/market → public market-data proxy

FastAPI also exposes authenticated descriptive digit-analysis endpoints. Orders are executed
through the authenticated WebSocket gateway according to Deriv Options API specifications;
the public market WebSocket remains observation-only.
```

Live market ticks and history are forwarded in memory only. Both Demo (`/trading/v1/options/ws/demo`)
and Real (`/trading/v1/options/ws/real`) accounts are authenticated via Deriv's single-use OTP
service and protected by server-side risk and stake validations.

## Account modes and product scope

Authenticated Real Options accounts are a supported gateway mode, alongside Demo accounts.
Products supported by a Deriv API integration must support both account modes; Real access must
be selected explicitly and remain subject to separate server-side limits and user confirmation.
`TRADEBRIDGE_REAL_MAX_STAKE` limits stake size but is not an enable/disable switch for Real trading.

**Real Options execution gate:** Real proposals, buys, and automated strategy starts are disabled
unless `TRADEBRIDGE_REAL_TRADING_ENABLED=true`, a positive finite `TRADEBRIDGE_REAL_MAX_STAKE` is
configured, and the user confirms Real trading on the current authenticated WebSocket connection.
The confirmation message is:

```json
{"real_trading":{"action":"confirm","confirmation":"ENABLE REAL TRADING"}}
```

Confirmation is connection-scoped and must be repeated after reconnect. The server-enforced stake
cap still applies. These controls do not establish that a strategy is profitable or eliminate
market risk; automated Real execution should remain disabled in production until account-specific
risk limits, operational monitoring, and Real execution tests have been reviewed.

**Multipliers and CFDs:** Deriv documents Multipliers (`MULTUP`/`MULTDOWN`) through its WebSocket
proposal/buy/contract lifecycle, but this repo's gateway and validators currently implement
Options digit contracts only. Verify Multipliers against a Demo account and the intended Deriv
session API before adding the product. Deriv's published MT5 API documentation says MT5 trading
is not supported through its APIs; this repo therefore does not promise MT5 CFD order execution.

## WebSocket reliability

Both WebSocket proxies implement automatic reconnection with exponential backoff
(1s, 2s, 4s, 8s, capped at 30s with jitter) for up to 5 attempts:

- `deriv_websocket_proxy` re-fetches a fresh OTP on each reconnect (OTPs are single-use),
  then resubscribes the active tick stream and replays tick history.
- `live_market_proxy` resubscribes all previously active market symbols.

When the OAuth credential approaches expiry, a `session_warning` message is sent to the
client 5 minutes before the token expires so the frontend can prompt a reconnect.

`proposal_open_contract` subscriptions are cleaned up with a `forget` call on
settlement to avoid leaking subscription slots across trade cycles.

Rate-limit (`RateLimit`) and temporary (`TemporaryError`) errors from Deriv trigger up
to 3 retries with exponential backoff instead of permanently blocking the strategy.
Non-retriable errors block execution and surface the specific Deriv error code to the
client.

## Deployment constraints

`_ws_tickets` is a process-local dict. In multi-worker deployments (e.g. `gunicorn -w N`
or multiple replicas), a ticket issued by one worker cannot be consumed by another.
Either run a single worker per instance with sticky WebSocket routing, or migrate
`_ws_tickets` to a shared store (Redis or another external KV).

## Interpretation

Frequency summaries and chi-square statistics are descriptive diagnostics, not trading
signals. They do not prove future outcomes are predictable. Strategy claims require
pre-registered hypotheses, adequate sample sizes, chronological train/validation/test
splits, and realistic payout/cost modelling.


## Backend digit frequency

The public `/deriv/live/market` WebSocket enriches Deriv history/tick responses with a `digit_frequency` object. The backend computes counts and one-decimal percentages over the last 50, 100, 250, 500 and 1,000 observed tick points. Each window counts only points for which last-digit extraction is valid. The snapshot identifies its source as `tradebridge_backend_tick_frequency` and includes the market symbol, calculation timestamp, latest tick timestamp, sample size, counts and percentages.

The frontend must render these backend values rather than independently recomputing the displayed digit percentages. They are TradeBridge-calculated observed-tick frequencies, **not official Deriv Digit Analytics percentages**, and are not evidence that any digit predicts the next outcome.

The strategy adapter `app.strategy.market_frequency_adapter.to_percentage_snapshot` converts the backend's selected tick window into the existing fail-closed strategy feed format. A strategy started on the authenticated Demo WebSocket requests the selected history window, observes live ticks, and can execute one confirmed signal as up to three sequential Demo buys. Each buy waits for settlement; two consecutive losses stop the sequence before a third buy. The public market WebSocket remains observation-only.
