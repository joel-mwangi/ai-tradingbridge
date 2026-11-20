# Even/Odd Lab — ML Research Pipeline

## Research boundary

The pipeline estimates the probability that the next observed last digit is even.
It is research software only. Training completion, a high validation score, or
a calibrated probability does not by itself establish a tradable edge.

## Dataset construction

1. Load bounded history for one Deriv market from the dedicated database.
2. Run the structural quality audit before model preparation.
3. Preserve the source rows and report repeated observation fingerprints.
4. Build supervised rows in chronological order. Each feature row uses only
   ticks before its target tick; the target parity is stored separately.
5. Do not mix symbols or treat overlapping collection windows as independent
   observations without a separately justified deduplication policy.

## Chronological partitions

`chronological_research_split` reserves four consecutive periods:

- Training: fit model coefficients and feature scaling.
- Validation: choose the training epoch using validation log loss.
- Calibration: fit a Platt-style probability calibrator independently of model
  fitting and early stopping.
- Test: final untouched evaluation period.

The split is chronological and never shuffles. The test partition must not be
used to select features, tune hyperparameters, or calibrate probabilities.

## Current model

The initial dependency-light model is standardized logistic regression with
L2 regularization and batch gradient descent. Means and standard deviations
are fit on the training partition only. Early stopping monitors validation log
loss. The calibrator fits a logistic mapping on the separate calibration
partition.

The implementation intentionally returns `validated=false` and
`execution_authorized=false`. A model must not be promoted simply because
training completed.

## Evaluation

The evaluator reports:
- accuracy and balanced accuracy;
- log loss and Brier score;
- confusion matrix;
- expected calibration error and calibration bins;
- constant 50% baseline metrics.

Expanding walk-forward splits provide repeated chronological future test
blocks. Results are compared against the constant 50% baseline and simple
rolling-frequency baselines. A block bootstrap estimates the
baseline-minus-model log-loss improvement interval and an approximate
one-sided bootstrap tail probability. Statistical support is reported only
when at least five walk-forward folds are available, the lower interval bound
is positive, and every fold has positive mean improvement. This is a conservative research gate, not a
guarantee of future performance. Performance must also be stable across
multiple markets and time periods, not concentrated in one favourable segment.

## Statistical cautions

Tick parity frequencies can be serially dependent, non-stationary, and affected
by overlapping collection windows. Ordinary IID confidence intervals and
single-sample significance tests can overstate evidence. The quality audit is
structural, not a test of independence or stationarity.

An exploratory block bootstrap estimates a local calibration interval
from calibrated walk-forward forecasts near a requested probability. It
resamples fixed-length chronological blocks, requires sufficient local observations and blocks,
and checks stability across walk-forward folds and interval width. The
`validated` flag means only that these configured empirical gates passed; it
is not a distribution-free coverage guarantee and does not validate the model
itself. The interval selector refuses extrapolation outside the calibrated
probability band. Regime drift and long-memory dependence remain risks that
must be assessed before demo use.

## Candidate model comparison and payout replay

Each experiment fits two candidate families using only past observations:
- standardized multivariate logistic regression;
- a Laplace-smoothed first-order parity Markov transition model.

Both candidates use the same chronological train, validation, calibration and
untouched holdout partitions. Walk-forward folds report each model separately
against the constant 50% and rolling-frequency baselines. Candidate selection
and promotion are not based on the final test partition. Both artifacts remain
`candidate`; neither family is automatically marked validated.

Walk-forward uncertainty for a test fold is estimated only from earlier
out-of-sample folds. The first folds therefore have no validated uncertainty
interval and cannot produce replay entries. The hosted workflow defaults to
eight folds so later folds can accumulate prior calibration evidence. The
aggregate intervals retained in the model registry are for future live
research forecasts; they must not be retroactively applied to the same rows
that produced them in a backtest.

The read-only `GET /even-odd/payouts` endpoint records matched Even and Odd
proposal terms in `even_odd_payout_quote_snapshots` when Database 2 is
configured and the migration has been applied. A historical replay uses only
a quote snapshot at or before the target tick, within the configured age
limit, and matching the exact market, currency and stake. It computes
payout-adjusted conservative EV, entry coverage, win/loss counts, net P&L,
ROI on stake, profit factor and maximum drawdown from out-of-sample forecasts.
No current quote is substituted for a missing historical quote.

If quote snapshots are absent or sparse, replay reports unavailable or low
coverage rather than inventing returns. A replay with fewer than 200 entries
is explicitly marked as insufficient evidence. Replay metrics remain
unvalidated research evidence and never enable broker execution.

Apply the migrations
`20261120002608_even_odd_historical_payout_snapshots.sql` and
`20261120002811_even_odd_payout_snapshot_sequence_privileges.sql` to the
 dedicated Database 2 project only. Continue collecting payout snapshots over time; the
migration cannot reconstruct historical payout prices that were never stored.

## Promotion gate

Before any model can be marked validated:
- freeze the feature schema and model version;
- complete repeated walk-forward evaluation;
- demonstrate improvement over relevant baselines on untouched future data;
- assess calibration and uncertainty on independent periods;
- measure performance after payout, spread, and contract costs;
- document sample sizes, markets, time ranges, and failure cases;
- require a separate explicit approval before any demo execution integration.

No live trading authorization is implemented by this pipeline.


## Running a local experiment

The research runner deliberately executes outside the Vercel request lifecycle
so bounded serverless API requests are not used for model training. From the
backend repository root, configure the dedicated database credentials in the
local environment and run:

```powershell
$env:EVEN_ODD_SUPABASE_URL = "https://<your-dedicated-project>.supabase.co"
$env:EVEN_ODD_SUPABASE_SECRET_KEY = "<your-secret-key>"
python -m scripts.even_odd_research --symbol R_50 --limit 5000 --walk-forward
```

Do not paste the secret into chat or commit it. The runner loads only the
dedicated Even/Odd database, performs the quality audit, then writes a JSON
research report to stdout. The default 50/100/250/500/1000 feature windows
require a sufficiently large historical sample; a small sample should fail
rather than silently train on inadequate history.


## Hybrid entry confirmation

The entry detector is a separate, deterministic gate. It consumes:
- a model probability interval for Even (and the complementary lower bound for Odd), sourced from the local walk-forward block-bootstrap calibration report;
- the model ID/version and an explicit validated flag;
- current data age and sample size;
- side-specific payout profit and loss;
- multiple strictly chronological forecast snapshots.

It requires the configured number of consecutive confirmations to agree on
the same side, remain within the freshness and time-gap limits, and exceed
both the minimum conservative probability edge over the payout-derived
break-even probability and the minimum conservative expected value.

The detector returns either `wait` or a non-executable `candidate`. It never
submits an order. Thresholds are server-owned policy defaults; callers cannot weaken them. Walk-forward evaluation now produces
local calibration envelopes using the moving-block bootstrap described above.
The model itself is still stored as a candidate and is not validated by the
training runner. Until an independent review approves the model and the
relevant local interval passes its empirical gates, the entry API must return
`wait`, not a candidate.


## Forecast and entry-check API

- `GET /even-odd/forecast?symbol=R_50&model_id=<uuid>` loads the selected
  stored model, fetches the latest bounded history, verifies data quality and
  freshness, builds next-tick features without a target label, and returns a
  research-only point forecast plus the nearest validated local-calibration
  envelope when one exists. It exposes the persisted tick row ID so the
  confirmation flow can reference an auditable observation.
- `GET /even-odd/payouts?symbol=R_50&currency=USD&stake=1.00` obtains
  read-only one-tick `DIGITEVEN` and `DIGITODD` proposal quotes from Deriv.
  Both quotes must match the requested contract parameters and have fresh spot
  timestamps. The endpoint does not buy proposals.
- `POST /even-odd/entry-check` requires an active `session_id` and ordered
  persisted tick references (`tick_row_id`, `tick_epoch`, `model_id`,
  `model_version`). It loads the session's market, currency, fixed stake and
  remaining loss budget; obtains fresh Deriv payout proposals; recomputes each
  probability from persisted ticks strictly before its referenced target; and
  applies server-owned freshness, sample, confirmation, edge and expected-value
  thresholds. Caller-supplied probabilities, payouts, validation flags,
  confidence intervals, sample sizes, data ages and policy overrides are not
  accepted as decision evidence.
- A response of `candidate` means only that the configured entry gates passed.
  It is not an order instruction. The response always sets execution disabled.


## Running a hosted research experiment

A manually dispatched GitHub Actions workflow runs the same research CLI outside
the Vercel request lifecycle:

1. Add the dedicated database credentials as repository Actions secrets:
   - `EVEN_ODD_SUPABASE_URL`
   - `EVEN_ODD_SUPABASE_SECRET_KEY`
2. Open **Actions → Even/Odd research experiment → Run workflow**.
3. Select the market, historical row limit, initial training rows, test-block
   size, fold count and maximum training epochs.
4. Review the workflow output and download the immutable
   `even-odd-research-...` JSON artifact.

The workflow writes a research-run record and a candidate model to the isolated
Even/Odd database. It never promotes a model to `validated` and never enables
order execution. A failed data-quality check, duplicate-observation gate,
insufficient history, or model-training error fails the run rather than
silently relaxing the research criteria.

The hosted workflow is intentionally manual: each experiment should be an
explicit, reviewable research action. Do not add the database secret to Vercel
frontend variables or expose it to browser code.
