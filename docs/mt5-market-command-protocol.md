# MT5 bridge: market-selection command protocol

## Status

Design contract only. This document does not add MT5 routes, pair a terminal, or enable order execution.

## Scope

Synchronize the selected instrument in the TradeBridge Forex Analyst with one explicitly paired MT5 desktop terminal. Market selection is a configuration command only: it must never create, modify, or close a trade. The existing Deriv digit Options and Multipliers execution paths remain separate.

## Components

- **Frontend:** submits the provider market symbol and displays requested, acknowledged, rejected, or stale status. It must not display 'synced' until an acknowledgement for the current command is received.
- **FastAPI backend:** authenticates the signed-in TradeBridge user, authorizes access to a paired terminal owned by that user, persists commands, and exposes authenticated polling and acknowledgement endpoints.
- **MT5 EA:** polls using a per-terminal credential, validates command identity and age, resolves the requested provider symbol through an explicit broker-symbol mapping, checks that the mapped symbol is selectable, applies the chart/active-symbol change, and acknowledges success or a safe rejection. It must not submit an order in response to this command.

## Persistence contract

Use durable Supabase tables, not a process-local Python list. Proposed tables:

### mt5_devices
- id UUID primary key
- user_id UUID, owner
- device_name text
- credential_hash text (hash only; never store the EA's raw device credential)
- status constrained to pending | paired | revoked
- last_seen_at nullable timestamptz
- created_at, updated_at timestamptz

### mt5_commands
- id UUID primary key
- device_id UUID foreign key
- user_id UUID owner (enforce owner/device consistency)
- command_type constrained to select_market
- provider_symbol text
- status constrained to pending | acknowledged | rejected | expired
- requested_at, expires_at timestamptz
- acknowledged_at nullable timestamptz
- result_code nullable text
- result_message nullable text
- unique idempotency key scoped to device

Use a migration that adds these tables and constraints; do not edit or delete already-pushed migrations. Enable RLS and deny direct client access to device credentials and command mutation. The service role is backend-only. Verify the actual migration history and current Supabase schema before writing the migration.

## Proposed HTTP contract

All user-facing endpoints require a valid, verified Supabase access token and derive user_id from the token; never trust a caller-supplied owner ID.

- POST /mt5/devices/pairing-codes: create a short-lived, single-use pairing code for the signed-in user.
- POST /mt5/devices/pair: consume the code and register an EA device. Return a raw device credential only once; store only its secure hash.
- GET /mt5/devices: list only the signed-in user's devices and safe status metadata.
- POST /mt5/devices/{device_id}/commands/select-market: validate symbol syntax, ownership, paired status, and idempotency key; persist a short-lived pending command.
- GET /mt5/ea/commands/next: authenticate the EA's device credential, lease the oldest valid command atomically, and return its ID, type, provider symbol, issue time, and expiry. Never return user tokens or broker credentials.
- POST /mt5/ea/commands/{command_id}/ack: authenticate the same device, verify command/device binding and expiry, then idempotently record acknowledged or rejected plus a bounded result code/message.

Exact route naming may change during implementation, but ownership, credential, persistence, expiry, and acknowledgement guarantees must not.

## Command lifecycle

1. Frontend asks the backend to select a provider symbol for one paired terminal.
2. Backend persists a command and returns its command ID with pending status.
3. EA polls and claims the command. Repeated polling must not execute it twice.
4. EA validates the symbol against broker availability and the explicit mapping; it does not guess suffixes.
5. EA applies the selection and acknowledges the command.
6. Frontend shows success only after the matching command ID is acknowledged. Rejection, timeout, revoked device, or stale command is visible as a failure.

Use server-generated IDs, UTC timestamps, bounded expiry, idempotency, and atomic claim/update semantics. If the database cannot provide an atomic claim operation, add a narrowly scoped Postgres function/RPC rather than relying on a read-then-write race.

## Security and safety requirements

- Require HTTPS in deployed environments.
- Hash device credentials using a suitable one-way password/secret hash; compare safely and support revocation/rotation.
- Pairing codes are short-lived, single-use, rate-limited, and shown only to the authenticated user.
- Do not log raw pairing codes, device credentials, access tokens, or full authorization headers.
- Device credential may authorize only that device's polling and acknowledgements, never user-facing endpoints or arbitrary commands.
- Validate command ID ownership and device binding on every request.
- Expired or duplicate commands must not be applied.
- A market-switch command must have no order side effects. No trade placement, SL/TP mutation, or position closure is allowed in this handler.
- Existing positions must continue to be managed regardless of the selected chart symbol.
- Never assume a provider symbol (for example, frxEURUSD) equals an MT5 broker symbol (for example, EURUSD). Maintain an explicit, broker-validated mapping and reject unknown mappings.
- The EA's trading input remains disabled for this phase. Market-selection sync is not authorization to trade.

## Acceptance tests

- A user cannot create, read, poll, or acknowledge commands for another user's device.
- Missing/malformed/revoked credentials are rejected.
- A pairing code expires and cannot be consumed twice.
- Concurrent polls cannot cause one command to be applied twice.
- A duplicate acknowledgement is idempotent; an acknowledgement for another device's command is rejected.
- Expired commands are never returned as executable.
- Unknown/unavailable broker symbols are rejected with a useful bounded result code.
- Frontend does not show synchronized state until the current command ID is acknowledged.
- Switching EUR/USD to GBP/USD changes only the active market selection and does not place, modify, or close a trade.
- Existing positions remain visible/managed after market switching.
- Deriv digit Options and Multipliers routes and tests remain unchanged.