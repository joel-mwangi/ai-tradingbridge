# TradeBridge AI

A Python research API and authenticated Deriv session gateway with an inline Demo bot executor.
It does not support real-account trading.

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
- `WS /deriv/ws?account_id=...` — authenticated Demo-only manual trading proxy.
- `WS /deriv/live/market` — public market-data proxy; no persistence.

### Bot executor (Supabase JWT auth, ticket-based WS auth)

- `WS /bots/run?account_id=...` — inline Demo bot executor.
  - Authenticate first with `POST /deriv/ws-ticket`.
  - Connect with the ticket in the `sec-websocket-protocol` header.
  - Send a `DemoBotConfig` JSON object as the first message after connection.
  - Receive structured JSON events: `started`, `trade_opened`, `trade_settled`, `error`, `done`.
  - Send `{"action": "stop"}` to stop gracefully after the current trade.

## Architecture

```
Browser / Next.js
  │
  ├─ POST /deriv/internal/oauth/exchange
  │    └─ FastAPI: PKCE code exchange → encrypt token → store credential + account links
  │
  ├─ POST /deriv/ws-ticket  →  30-second one-use ticket
  │
  └─ WS /bots/run?account_id=VRTC123
       └─ FastAPI inline bot executor:
            ├─ Validate ticket + Deriv credential + account ownership
            ├─ Open Deriv Demo WebSocket via OTP
            ├─ asyncio bot loop (bot_state TradeState machine):
            │    CREATED → REQUESTING_PROPOSAL → BUY_PENDING → OPEN → SETTLING → SETTLED
            │    Stop conditions checked after every settled trade
            │    Cooldown between trades (exits immediately on stop signal)
            ├─ Stream JSON events to frontend
            └─ {"action":"stop"} → graceful stop after current trade
```

Live market ticks and history are forwarded in memory only. Bot execution is Demo-only;
real-account execution is explicitly blocked at every validation layer.

Active bot runs are tracked in-memory per process. A run stops if the server restarts.
For multi-worker deployments, keep a single ASGI worker until WebSocket tickets and active
run tracking are backed by a shared store (e.g. Redis or Supabase).

## Interpretation

Frequency summaries and chi-square statistics are descriptive diagnostics, not trading
signals. They do not prove future outcomes are predictable. Strategy claims require
pre-registered hypotheses, adequate sample sizes, chronological train/validation/test
splits, and realistic payout/cost modelling.
