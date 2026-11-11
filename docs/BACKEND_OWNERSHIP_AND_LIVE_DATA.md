# Backend ownership and live-data architecture

## Decision

`ai-tradingbridge` is the sole owner of research logic: descriptive statistics, feature generation, model inference, evaluation, and research-result schemas. `tradebridge` is the trading UI and execution client. It must not reimplement research calculations.

Manual trade execution remains in the TradeBridge frontend through its existing Deriv trading connection. Research APIs must never submit, purchase, or modify contracts.

## Current verified baseline

- `app/main.py` exposes authenticated `POST /analysis/digits` and `GET /analysis/digits/latest`.
- `GET /analysis/digits/latest` reads stored `market_ticks` from Supabase and computes descriptive digit statistics.
- The API does not currently own a persistent Deriv WebSocket collector or a live analysis stream.
- The documented ingestion flow is Deriv WebSocket -> Cloudflare Worker -> TradeBridge ingestion endpoint -> Supabase -> FastAPI historical analysis.
- The deployed URL alone does not prove that the database migration, secrets, collector, or end-to-end ingestion are configured.

## Target responsibility boundary

### FastAPI research backend
- Own research calculations, models, validation and versioned response schemas.
- Provide authenticated historical analysis and model-evaluation APIs.
- Provide an internal, authenticated tick-ingestion interface only if the chosen collector sends ticks directly to this service.
- Publish live research updates through a supported streaming transport when the runtime supports persistent connections.
- Record timestamps, source, model/version, sample window and freshness metadata; report stale/disconnected state instead of implying data is live.
- Never place trades.

### TradeBridge frontend
- Render backend-produced research results; do not calculate duplicate statistics or model outputs.
- Keep market/contract selection, stake entry, manual trade submission, contract monitoring and settlement in the existing Deriv trading workflow.
- Keep AI-service failures isolated from the trading connection and order lifecycle.
- Keep server-to-server secrets out of browser code. If authenticated user access requires a Next.js proxy, that proxy is transport/authentication only—not a second research engine.

### Persistence
- Supabase is durable market history and audit storage, not a high-frequency UI message bus.
- Use bounded batches and idempotent writes. Avoid a database read per tick or multiple collectors persisting the same stream.

## Live-stream deployment constraint

Do not assume a serverless FastAPI deployment can keep a single Deriv WebSocket collector alive or preserve in-memory rolling state across requests/instances. Before implementing a persistent collector or SSE/WebSocket fan-out, confirm the actual hosting runtime. If the service runs as serverless functions, use a supported long-running worker/service for collection and shared state or event delivery; do not rely on a process-local singleton.

## Safe implementation sequence

1. Inventory the current deployment/runtime, collector source, Supabase migration state, and server-side secrets without exposing secret values.
2. Define one versioned research response/event schema shared by backend and frontend.
3. Consolidate all research calculations in this repository and add tests.
4. Establish exactly one authoritative research tick feed; retain existing trading socket and manual order flow.
5. Batch idempotent history persistence; make stale data and reconnect status explicit.
6. Add authenticated live delivery appropriate to the hosting runtime.
7. Change TradeBridge to a thin consumer and remove only proven duplicate research logic.
8. Run unit, integration, reconnect, duplicate-tick, stale-data, auth, and manual-trading regression tests before rollout.

## Acceptance criteria

- One authoritative research calculation implementation.
- No research service can place trades.
- No browser-visible server token or service-role key.
- Live status is backed by tick timestamps and connection health, not a UI timer.
- Reconnects and duplicate deliveries do not corrupt rolling statistics or history.
- Research backend outages do not break manual trading.
- Historical analysis remains available for comparison and testing.
