# Digit Analytics Percentage Feed Adapter Contract

Status: **contract only — no verified Deriv Digit Analytics percentage endpoint has been established**.

This document defines what an eventual adapter must provide. It does not claim that Deriv currently exposes these fields through a supported API. Do not connect a tick histogram or locally calculated digit frequency as a substitute for the percentages shown in Deriv Digit Analytics unless the product owner explicitly approves that change.

## Required normalized snapshot

Every accepted source event must normalize to one immutable snapshot containing:

- `source_id`: stable identifier for the verified upstream product/feed.
- `source_event_id` or an equivalent monotonic sequence identifier, when the upstream source provides one.
- `observed_at`: timezone-aware timestamp supplied by the source, not generated at receipt time.
- `received_at`: timezone-aware timestamp recorded by our adapter when the event arrives.
- `market_id`: exact market identifier matching the configured strategy session.
- `percentages`: a complete mapping for digits 0 through 9, each a finite numeric value in the inclusive range 0–100.
- `source_metadata`: enough non-secret metadata to diagnose feed/version/schema changes.

Do not infer missing digits, fill gaps with the previous value, synthesize timestamps, or silently round values to make validation pass.

## Verification gate

Before enabling an adapter, document and review:

1. Official source documentation or written confirmation from Deriv that this feed represents the exact Digit Analytics percentages.
2. Authentication and authorization requirements, if any.
3. A real, redacted sample payload and field mapping.
4. Timestamp semantics, update cadence, rounding/precision, and market identifiers.
5. Reconnection, duplicate-event, out-of-order-event, and rate-limit behavior.
6. A test fixture derived from a genuine source payload.

A configuration flag or caller-provided boolean such as `is_verified=true` is not proof of source authenticity. Source verification must be an explicit deployment/review decision backed by evidence. Until then, the adapter must remain unavailable to strategy selection.

## Freshness and continuity rules

- Reject snapshots for the wrong market or with an incomplete digit map.
- Reject non-finite values, booleans, and percentages outside 0–100.
- Require timezone-aware source timestamps and reject timestamps that move backwards.
- Deduplicate events using the source event ID/sequence where available; never count a duplicate as a new stability observation.
- Track source event time separately from local receipt time.
- Configure a maximum age based on the confirmed feed cadence. If no cadence is confirmed, do not invent a timeout and claim it is safe; keep the feed disabled pending an explicit setting.
- On disconnect, stale data, schema mismatch, or suspected missing updates, mark the feed unhealthy and stop producing strategy-eligible observations. Never keep replaying the last percentage snapshot as if it were fresh.
- After reconnect, re-establish a complete, ordered observation window from fresh source events before allowing a new selection.

## Session integration

The strategy may evaluate a window only after feed health and source verification pass. The initial 10-second window must contain at least two real, distinct source updates with source timestamps inside that window. This minimum does not prove that every expected update arrived; continuity guarantees depend on what the upstream source actually exposes.

The post-window policy (rolling window versus fixed-window re-evaluation) remains a separate product decision and must not be inferred by the adapter.

## Fail-closed behavior

Any missing verification, stale or malformed snapshot, wrong market, timestamp regression, or feed-health uncertainty must produce a non-tradable state and a diagnostic reason. Feed failure must never trigger a purchase. Accepted trades, if later implemented, would still require separate reconciliation and settlement handling.

## Current implementation boundary

- Implemented: pure percentage rules, timestamp-window validation, and regression tests.
- Not implemented: a Deriv Digit Analytics percentage adapter, feed-health monitoring, session orchestration, or trade execution.
- Real-account execution remains disabled.
