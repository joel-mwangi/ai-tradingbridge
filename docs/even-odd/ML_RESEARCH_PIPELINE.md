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
blocks. Results should be compared against the constant 50% baseline and
simple rolling-frequency baselines. Performance must be stable across
multiple markets and time periods, not concentrated in one favourable segment.

## Statistical cautions

Tick parity frequencies can be serially dependent, non-stationary, and affected
by overlapping collection windows. Ordinary IID confidence intervals and
single-sample significance tests can overstate evidence. The quality audit is
structural, not a test of independence or stationarity.

An exploratory moving-block bootstrap estimates a local calibration interval
from calibrated walk-forward forecasts near a requested probability. It
resamples contiguous blocks, requires sufficient local observations and blocks,
and checks stability across walk-forward folds and interval width. The
`validated` flag means only that these configured empirical gates passed; it
is not a distribution-free coverage guarantee and does not validate the model
itself. The interval selector refuses extrapolation outside the calibrated
probability band. Regime drift and long-memory dependence remain risks that
must be assessed before demo use.

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
submits an order. Thresholds are explicit policy inputs rather than hidden
hard-coded claims of profitability. Walk-forward evaluation now produces
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
- `POST /even-odd/entry-check` accepts multiple forecast observations and
  side-specific payout terms. The backend re-reads the model's validation
  status and uncertainty intervals from the dedicated database, verifies each
  tick row ID/epoch against persisted market history, computes data age on the
  server, and then applies the hybrid confirmation gates. Client-supplied
  validation flags and data-age claims are not trusted.
- A response of `candidate` means only that the configured entry gates passed.
  It is not an order instruction. The response always sets execution disabled.
