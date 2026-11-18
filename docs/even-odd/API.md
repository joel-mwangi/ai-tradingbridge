# Even/Odd Lab API Contract

All endpoints are served by the FastAPI backend under the `/even-odd` prefix.
Every endpoint requires:

```http
Authorization: Bearer <TRADEBRIDGE_API_TOKEN>
Content-Type: application/json
```

The token and dedicated Supabase credentials are backend-only. Never expose
`EVEN_ODD_SUPABASE_SECRET_KEY` or the API token through a browser bundle or a
`NEXT_PUBLIC_*` variable.

## Service and market data

| Method | Endpoint | Purpose |
|---|---|---|
| GET | `/even-odd/status` | Module status and execution/model availability |
| POST | `/even-odd/analyze` | Descriptive analysis of submitted digits |
| POST | `/even-odd/collect` | Bounded Deriv public tick collection and persistence |
| GET | `/even-odd/payouts?symbol=R_50&currency=USD&stake=1.00` | Read-only one-tick DIGITEVEN/DIGITODD payout proposals |
| GET | `/even-odd/history` | Latest bounded persisted tick history, returned chronologically |
| GET | `/even-odd/quality` | Structural and duplicate-observation audit |

Collection accepts 10–300 ticks and a 5–80 second receive timeout. A timeout may
produce a persisted partial collection. The response reports the collection
ID, requested/received counts, status, and descriptive analysis. Collection
never places an order.

History returns at most 5,000 of the newest rows in chronological order. Its
snapshot boundary prevents concurrent inserts from shifting paged reads.

## Research and models

| Method | Endpoint | Purpose |
|---|---|---|
| GET | `/even-odd/models?symbol=R_50` | List model metadata and evaluation summaries |
| GET | `/even-odd/research-runs?symbol=R_50` | List experiment history |
| GET | `/even-odd/forecast?symbol=R_50&model_id=<uuid>` | Research-only next-parity forecast |
| GET | `/even-odd/payouts?symbol=R_50&currency=USD&stake=1` | Read-only current Even/Odd Deriv proposal terms |

The research runner is local, not a Vercel request:

```powershell
python -m scripts.even_odd_research --symbol R_50 --walk-forward
```

It requires sufficient persisted history, audits data quality, trains and
calibrates a candidate model, evaluates chronological holdout and expanding
walk-forward folds, and persists the experiment and candidate artifact.
Walk-forward output includes local calibration intervals and block-bootstrap
log-loss comparisons against constant and rolling-frequency baselines.

Models remain `candidate`. The runner does not promote them to `validated`.
A point forecast is research information, not an entry signal.

## Hybrid entry evaluation

`POST /even-odd/entry-check` accepts an active `session_id` and an ordered
list of observations. Each observation supplies only:

- `tick_row_id` and `tick_epoch` from persisted market history;
- `model_id` and `model_version`.

The backend loads the active session and derives the market, currency, and fixed
stake from its stored configuration. It rejects closed sessions, market/model
mismatches, exhausted trade limits, a missing monetary loss cap, and an
insufficient remaining loss budget.

For payout-aware expected value, the backend obtains fresh one-tick
`DIGITEVEN` and `DIGITODD` proposals from Deriv's public WebSocket API. It
validates the returned proposal identifiers and echoed contract parameters,
then derives net profit-if-win as `payout - ask_price` and loss-if-lose as
`ask_price`. These are price quotes only: the backend does not buy either
proposal. A quote failure blocks the entry check.

The client does not supply model probabilities, payout values, validation
flags, confidence intervals, sample sizes, or data-age claims. The backend
re-loads the model, recomputes each probability from persisted history strictly
before the referenced tick, derives uncertainty bounds from the stored
walk-forward calibration report, verifies tick IDs/epochs, and computes
freshness on the server.

Entry thresholds are server-owned and cannot be relaxed by request fields:
- minimum sample size is the model's largest trained feature window;
- maximum data age is 10 seconds;
- three consecutive confirmations are required;
- maximum confirmation gap is 10 seconds;
- minimum conservative edge over payout break-even is 5 percentage points;
- minimum conservative expected value is 0.10 in the session currency.

A `candidate` response means all configured gates passed. It is not an order
instruction. The session's fixed stake and remaining loss budget are checked
before the candidate is returned. Missing/invalid model or uncertainty
evidence, stale data, invalid tick references, unstable direction, inadequate
payout-adjusted EV, or failed confirmation returns `wait` or an HTTP error.

## Persistent trade management

| Method | Endpoint | Purpose |
|---|---|---|
| POST | `/even-odd/sessions` | Create a research/demo/real-account ledger session |
| GET | `/even-odd/sessions/{session_id}` | Read session and sequence history |
| POST | `/even-odd/sequence-plan` | Plan a 1–7 trade upper bound from recent persisted opportunities |
| POST | `/even-odd/sequences` | Start the approved sequence plan |
| GET | `/even-odd/sequences/{sequence_id}` | Read sequence and settled trade records |
| POST | `/even-odd/settlements` | Record a completed trade outcome |

The sequence planner recomputes forecasts from persisted tick history, applies
the current live payout quote and uncertainty bounds, and selects the longest
positive-conservative-EV prefix. Its current persistence input is a recent
historical opportunity proxy, not a trained forward sequence-survival model
or a historical payout replay. The result is only an upper bound; revalidate
after every completed trade.

Session creation requires an explicit fixed stake, currency, and maximum
session-loss budget. Database functions enforce the 7-trade sequence cap,
10-trade session cap, 2-consecutive-loss stop, fixed stake, validated-model
gate, one active sequence per session, and remaining loss budget.

The settlement endpoint records an outcome; it does not submit or close a
Deriv contract. No Even/Odd endpoint in this backend places orders.

## Environment configuration

Configure only in the backend deployment:

- `TRADEBRIDGE_API_TOKEN`
- `EVEN_ODD_SUPABASE_URL`
- `EVEN_ODD_SUPABASE_SECRET_KEY` (preferred), or
  `EVEN_ODD_SUPABASE_SERVICE_ROLE_KEY` for a legacy JWT key

The Even/Odd storage adapter never falls back to the primary TradeBridge
Supabase project. The dedicated project has RLS enabled and is accessible to
the backend service role only.
