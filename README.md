# TradeBridge AI

A Python research API and authenticated Deriv session gateway for manual Demo trading.
Automated bot execution has been removed. Real-account trading is not supported.

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
TRADEBRIDGE_API_TOKEN=
SUPABASE_URL=
SUPABASE_ANON_KEY=
SUPABASE_SERVICE_ROLE_KEY=
DERIV_OAUTH_CLIENT_ID=
DERIV_OAUTH_REDIRECT_URI=https://YOUR_FRONTEND_HOST/api/auth/deriv/callback
TRADEBRIDGE_SESSION_BRIDGE_TOKEN=
TRADEBRIDGE_CREDENTIAL_ENCRYPTION_KEY=
TRADEBRIDGE_ALLOWED_ORIGINS=https://YOUR_FRONTEND_HOST,http://localhost:3000
TRADEBRIDGE_DEMO_MAX_STAKE=1000
DERIV_SYMBOL=1HZ100V
# cTrader Open API — leave blank until Spotware approves the app
CTRADER_OPENAPI_CLIENT_ID=
CTRADER_OPENAPI_CLIENT_SECRET=
CTRADER_OAUTH_REDIRECT_URI=https://YOUR_FRONTEND_HOST/api/auth/ctrader/callback
APP_URL=https://YOUR_FRONTEND_HOST
CTRADER_DEMO_ORDERS_ENABLED=false
CTRADER_DEMO_MAX_VOLUME_UNITS=1000
```

Generate a strong token: `python -c "import secrets; print(secrets.token_urlsafe(48))"`.

Generate a Fernet key: `python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"`.

The `TRADEBRIDGE_SESSION_BRIDGE_TOKEN` must match the server-only value in Next.js. Never
expose it through `NEXT_PUBLIC_*`. The service-role key bypasses Supabase RLS — keep it
only in server-side environment variables.

## cTrader Open API gateway

Conventional Forex CFD access is provided separately from the Deriv Options gateway. The cTrader API app must first be registered and approved by Spotware, with the exact callback URI registered in that app. Keep `CTRADER_OPENAPI_CLIENT_ID`, `CTRADER_OPENAPI_CLIENT_SECRET`, and `CTRADER_OAUTH_REDIRECT_URI` unset until that approval is complete; then add their values in the backend and frontend platform environment settings, not in source control. The client secret, Supabase service-role key, encryption key, and bridge token must remain backend/server-only. The frontend onboarding panel links users to Deriv's account activation instructions when needed.

All cTrader order and position-changing actions are limited to Demo accounts by the backend in this rollout. Keep `CTRADER_DEMO_ORDERS_ENABLED=false` while validating OAuth, account ownership, migrations, and provider responses. There is no live-account order execution in this release.

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
- `WS /deriv/ws?account_id=...` — authenticated Demo-only manual trading proxy.
- `WS /deriv/live/market` — public market-data proxy; no persistence.

## Architecture

```
Browser / Next.js
  ├─ OAuth callback → POST /deriv/internal/oauth/exchange
  │    └─ FastAPI exchanges PKCE code, encrypts token, stores credential/account links
  ├─ POST /deriv/ws-ticket → short-lived, single-use WebSocket ticket
  ├─ WS /deriv/ws?account_id=... → authenticated Demo-only manual trading proxy
  └─ WS /deriv/live/market → public market-data proxy

FastAPI also exposes authenticated descriptive digit-analysis endpoints. No bot runner,
strategy executor, bot configuration API, or automated order loop is registered.
```

Live market ticks and history are forwarded in memory only. Real-account execution remains
explicitly blocked at the gateway validation layer.

## Interpretation

Frequency summaries and chi-square statistics are descriptive diagnostics, not trading
signals. They do not prove future outcomes are predictable. Strategy claims require
pre-registered hypotheses, adequate sample sizes, chronological train/validation/test
splits, and realistic payout/cost modelling.


## Backend digit frequency

The public `/deriv/live/market` WebSocket enriches Deriv history/tick responses with a `digit_frequency` object. The backend computes counts and one-decimal percentages over the last 50, 100, 250, 500 and 1,000 observed tick points. Each window counts only points for which last-digit extraction is valid. The snapshot identifies its source as `tradebridge_backend_tick_frequency` and includes the market symbol, calculation timestamp, latest tick timestamp, sample size, counts and percentages.

The frontend must render these backend values rather than independently recomputing the displayed digit percentages. They are TradeBridge-calculated observed-tick frequencies, **not official Deriv Digit Analytics percentages**, and are not evidence that any digit predicts the next outcome.

The strategy adapter `app.strategy.market_frequency_adapter.to_percentage_snapshot` converts the backend's 1,000-tick window into the existing fail-closed strategy feed format. The strategy still requires an explicitly configured `PercentageFeedHealth` monitor and a fresh observation window. This adapter does not start a strategy session, place orders, or enable trade execution. Live strategy orchestration remains separate.
