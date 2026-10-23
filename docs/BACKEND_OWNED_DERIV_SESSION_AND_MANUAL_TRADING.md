# Backend-Owned Deriv Session and Manual Trading

## Target boundary

TradeBridge browser UI is a presentation and intent layer. FastAPI is the only owner of Deriv sessions and authenticated Deriv API operations.

The browser renders prices, account summary, connection state, open contracts, transactions and errors; lets the user authorize an account; select market, contract and stake; request a proposal; explicitly confirm a purchase; and request supported contract actions. It must never receive or persist a Deriv API token.

FastAPI owns Deriv WebSocket connections, authorization/session lifecycle, account identity, reconnect/re-authorize, subscriptions, proposals, server-side validation, purchases, open-contract monitoring, transaction reconciliation, logout, per-user isolation, idempotency and ephemeral market-data distribution. No tick persistence is required in this phase.

## Security requirements

1. Never put Deriv tokens in query strings, URLs, browser storage, logs, analytics or public frontend environment variables.
2. Use HTTPS/WSS in production. Authenticate browser-to-FastAPI HTTP and WebSocket sessions using the app's own secure session, not a client-supplied login ID.
3. Prefer Deriv OAuth where available. If direct token authorization is used, collect it only over TLS, validate required scopes, keep it server-side, encrypt at rest if persisted, and never log or return it.
4. Bind each Deriv session to the authenticated TradeBridge user. Never allow one user to access another user's session.
5. Validate symbol, contract type, stake bounds, currency, proposal ownership/age and account mode on the server.
6. Every real-money purchase requires an explicit user action, idempotency protection and a clear result. No auto-trading, bots or AI decisions in this migration.
7. On disconnect, mark state stale; reconnect with bounded backoff, restore subscriptions and reconcile uncertain purchases before allowing retries.
8. Research/AI outages must not block account access or manual trading.

## Target flow

- Browser authenticates to the TradeBridge application and opens an authenticated FastAPI session.
- FastAPI owns the Deriv connection and required market/account subscriptions.
- FastAPI sends sanitized tick, account, contract and status events to that user's authenticated browser WebSocket.
- Browser submits manual trade intent to FastAPI.
- FastAPI validates parameters and requests a proposal. UI presents the proposal and requires explicit confirmation.
- FastAPI revalidates and purchases once, then reports transaction/contract identifiers and monitors open-contract state and settlement.
- Live ticks are relayed in memory only. No database writes, persistent market history or replay in this phase.

## Migration sequence

### Phase 0 — inventory
Map current browser-side Deriv sockets, token handling, authorization, proposal, buy/sell, open-contract and settlement flows. Identify current OAuth/token model and establish regression tests. Do not delete existing paths until equivalent backend paths pass tests.

### Phase 1 — backend session and live feed
Deploy FastAPI to an always-on ASGI host that supports long-lived WebSockets. Implement authenticated app sessions, per-user Deriv session management, reconnect/resubscribe, bounded queues and live ticks without database writes. Do not assume a serverless deployment can maintain a persistent session.

### Phase 2 — authorization and account
Move authorization, account details, balance, currency, account mode and account subscriptions behind FastAPI. Remove browser access to Deriv credentials. Verify account switching and logout.

### Phase 3 — manual order lifecycle
Move proposal, purchase, supported sell/cancel actions, open-contract updates, transaction history and settlement behind FastAPI. Keep proposal and purchase separate; require explicit confirmation; add validation, idempotency and timeout reconciliation. Preserve all manual UI controls.

### Phase 4 — cutover
Compare backend read-only results with existing behavior. Switch one operation at a time behind a feature flag, starting with demo accounts. Remove browser-side Deriv operations only after parity and acceptance tests. Never run two purchase paths concurrently. Keep rollback capability.

## Acceptance criteria

- No Deriv token reaches browser JavaScript or API responses.
- One user cannot access another user's Deriv session.
- Live ticks arrive without database dependency or persistence.
- Manual purchase follows proposal -> explicit confirmation -> server validation -> one purchase attempt -> reconciliation.
- Duplicate submits and timeouts cannot silently create duplicate purchases.
- Balances, open contracts, transaction results and settlement reconcile with Deriv.
- Disconnected/stale status is clear; reconnection does not imply an order succeeded.
- Manual trading remains available if research/AI is unavailable.
- Automated tests cover auth, access isolation, invalid inputs, upstream errors, reconnects, duplicate purchase requests and demo-mode flow.

## Current status

This document defines the target architecture and safe migration sequence; it is not evidence that the migration is implemented. The initial live tick relay is only a first step: it currently opens an upstream Deriv connection per browser client and is not a shared, authenticated per-user session manager. Do not treat that relay as production-ready account/trading infrastructure.
