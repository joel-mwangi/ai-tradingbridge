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
structural, not a test of independence or stationarity. Further dependence-aware
inference, drift analysis, and block-based uncertainty estimates are required
before a model can be considered for demo trading.

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
