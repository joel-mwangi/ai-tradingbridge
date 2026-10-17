# Demo bot worker deployment

The worker is a separate, long-lived Python process. Do not run it as a request handler or rely on a Vercel serverless function to keep it alive.

## Required infrastructure

- An always-on ASGI/worker host with outbound HTTPS and WebSocket access to `api.derivws.com`.
- The same Supabase project as the FastAPI backend.
- `SUPABASE_URL` and `SUPABASE_SERVICE_ROLE_KEY` configured only in the backend/worker environment.
- `TRADEBRIDGE_CREDENTIAL_ENCRYPTION_KEY`, OAuth settings, and allowed-origin configuration required by the existing Deriv gateway.
- A database migration applied to the correct Supabase project before starting the worker.
- Process supervision and restart policy, structured logs without tokens, and monitoring for runs stuck in `running` or `reconciliation_required`.

## Run control API

The authenticated `/bots/runs` API persists validated Demo run requests and status. It restricts the first worker contract to fixed stake, digit contracts, tick duration 1–10, at least one stop condition, maximum 20 trades, and a maximum 30-minute run. API acceptance means the run request was persisted; it does not mean trading has started.

## Rollout gate

Do not expose a Start button for backend runs until all of the following are true:

1. The worker implementation is deployed as a long-lived process and its health/heartbeat is monitored.
2. The Supabase migration has been applied and checked against the actual project.
3. A Demo-only end-to-end test verifies proposal, buy acknowledgement, contract settlement, persisted ledger values, stop request, worker restart, and uncertain-outcome reconciliation.
4. The frontend displays server-confirmed run state and does not present browser-local counters as authoritative P&L.
5. Real-account execution remains disabled.

A run stuck in `reconciliation_required` must be reconciled with Deriv's account/contract history before any subsequent purchase. Never automatically resubmit an uncertain buy.
