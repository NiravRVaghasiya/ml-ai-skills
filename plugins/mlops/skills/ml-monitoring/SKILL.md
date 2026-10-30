---
name: ml-monitoring
display_name: ML Monitoring
description: >
  Use when the user wants to detect data drift, model performance decay, or
  set up alerting for a model that is already running in production.
  Trigger phrases: "detect data drift", "is my model degrading in
  production", "monitor model performance over time", "set up alerts for
  prediction drift", "check for feature skew". NOT for logging training-time
  experiments (see experiment-tracking) or running pre-deploy test suites
  (see ci-cd-for-ml).
type: workflow
domain: mlops
level: intermediate
lifecycle: stable
risk_level: medium
evidence_level: established-practice
last_verified: 2026-10-28
capabilities:
  - data-drift-detection
  - concept-drift-detection
  - prediction-drift-detection
  - performance-degradation-tracking
  - drift-alerting-thresholds
  - scheduled-monitoring-jobs
requires:
conflicts:
related:
  - model-deployment
  - experiment-tracking
  - data-pipelines
inputs: A model already serving live traffic, a frozen reference dataset (held-out rows plus the model's scores on them, captured at training time), and (eventually) delayed ground-truth labels for served predictions.
outputs: A scheduled drift/performance check plus alerts that fire before customers notice a degraded model.
version_constraints:
  - "evidently>=0.7,<0.8: from evidently import Report; from evidently.presets import DataDriftPreset(num_method='psi', cat_method='psi'); Report.run(...) returning a Snapshot with .dict() and .save_html() (executed against evidently 0.7.23 on 2026-10-27; the 0.7.23 wheel has no evidently.report or evidently.metric_preset module)"
  - "pandas>=1.5,<3: read_json(lines=True), DataFrame.merge(validate='one_to_one') (executed against pandas 2.2.3 on 2026-10-27; lower bound not verified; <3 because evidently 0.7.23 failed to import on pandas 3.1.0 in CI on 2026-10-28)"
  - "apache-airflow>=2.4: DAG(schedule=...) (not executed: Airflow is stubbed in tests; apache-airflow-task-sdk 1.3.2 source read on 2026-10-27 has schedule but no schedule_interval; 2.4 lower bound documented: the Airflow 2.4.0 release notes add the schedule argument and deprecate schedule_interval, read 2026-10-29)"
---

## Overview
Watches a model *after* it's serving live traffic. This skill treats four distinct
failure modes as distinct, because they have different causes and different fixes:
**data drift** (the distribution of input features shifts away from training time),
**prediction drift** (the distribution of the model's *outputs* shifts — which can be
caused by data drift, by concept drift, or by a plain serving bug), **concept drift**
(the input-output relationship `P(y|x)` itself changes, so the same inputs now warrant a
different answer), and **performance degradation** (the ground-truth-measured metric
drops, for *any* reason — including causes that have nothing to do with drift, like a
broken feature pipeline). Covers logging production predictions, computing data drift and
prediction drift against a training-time reference, measuring delayed-label performance
decay, and wiring both into alerting. The output is a running check (scheduled job) that
tells you a model needs attention before customers notice, rather than a training-time
metric that goes stale the day the model ships.

## Workflow
1. **Log every prediction with its inputs and a request ID.** Without this, there's
   nothing to compare against later — and without the `request_id`, delayed labels can't
   be joined back to the prediction they grade (step 4). JSON Lines keeps each record
   self-describing, so a reordered or added feature can't silently shift values under the
   wrong CSV header the way appending dict rows to a CSV can.
   ```python
   import json
   import uuid
   from datetime import datetime, timezone

   LOG_PATH = "prediction_log.jsonl"
   RESERVED = {"request_id", "prediction", "score", "model_version", "logged_at"}

   def log_prediction(features: dict, prediction, score: float, model_version: str,
                      request_id: str | None = None) -> str:
       clash = RESERVED & features.keys()
       if clash:
           raise ValueError(f"feature names collide with log fields: {sorted(clash)}")
       request_id = request_id or str(uuid.uuid4())
       row = {**features, "request_id": request_id, "prediction": prediction,
              "score": float(score), "model_version": model_version,
              "logged_at": datetime.now(timezone.utc).isoformat()}
       with open(LOG_PATH, "a", encoding="utf-8") as fh:   # one line per write; use a log
           fh.write(json.dumps(row, default=str) + "\n")    # pipeline for multi-process servers
       return request_id
   ```
2. **Compute data drift against the training-time reference.** `evidently` compares
   *input feature* distributions and reports a share of drifted columns. This tells you
   the world the model sees has changed — it does not, by itself, tell you whether the
   model is still accurate. PSI is used per column so the decision is an effect size, not
   a p-value that becomes "significant" for any shift at production volumes. Validate the
   inputs first: evidently does not complain when the current window is missing a
   reference column.
   ```python
   # pip install "evidently>=0.7,<0.8" "pandas<3"   (0.7 changed the API; 0.7.23 breaks on pandas 3; see Gotchas)
   import pandas as pd
   from evidently import Report
   from evidently.presets import DataDriftPreset

   NON_FEATURES = RESERVED | {"y_true"}
   MIN_ROWS = 200   # rule of thumb: below a few hundred rows, PSI on binned data is noisy

   def drifted_share(snapshot) -> float:
       for metric in snapshot.dict()["metrics"]:
           if metric["metric_name"].startswith("DriftedColumnsCount"):
               return float(metric["value"]["share"])
       raise KeyError("DriftedColumnsCount missing from report; has the evidently API changed?")

   def drift_share_for(reference: pd.DataFrame, current: pd.DataFrame, cols: list[str],
                       html_path: str | None = None) -> float:
       missing = [c for c in cols if c not in current.columns]
       if missing:
           raise ValueError(f"current window lacks reference columns: {missing}")
       if len(current) < MIN_ROWS or len(reference) < MIN_ROWS:
           raise ValueError(f"need >= {MIN_ROWS} rows per window, got {len(reference)}/{len(current)}")
       preset = DataDriftPreset(num_method="psi", num_threshold=0.2, cat_method="psi", cat_threshold=0.2)
       snapshot = Report([preset]).run(current_data=current[cols], reference_data=reference[cols])
       if html_path:
           snapshot.save_html(html_path)
       return drifted_share(snapshot)

   # Frozen at training time from rows the model was NOT fit on (validation/holdout set):
   # their features plus the model's scores on them. Training rows' scores are sharper than
   # anything production will produce, so they make step 3 fire on undrifted traffic.
   reference = pd.read_csv("holdout_reference.csv")
   current = pd.read_json(LOG_PATH, lines=True).tail(5000)   # recent production window
   feature_cols = [c for c in reference.columns if c not in NON_FEATURES]

   drift_share = drift_share_for(reference, current, feature_cols, "data_drift_report.html")
   print(f"Share of drifted feature columns: {drift_share:.2%}")
   ```
3. **Separately, compute prediction drift on the model's output column.** Run the same
   drift test against the *score* distribution, not the features — which requires the
   reference file to contain the model's scores on the reference rows, and those rows must
   be held out from training (see Gotchas). Prediction drift
   can fire even when data drift doesn't — e.g. a serving-side bug (wrong feature order,
   a stale encoder) can shift outputs with no visible change in the raw input features.
   ```python
   pred_drift_detected = drift_share_for(reference, current, ["score"], "prediction_drift_report.html") > 0
   print(f"Prediction distribution drifted: {pred_drift_detected}")
   ```
4. **Measure performance decay once ground truth catches up.** This is the lagging,
   ground-truth-confirmed check, and it is the only one of the four signals here that can
   directly reveal **concept drift** — a real shift in `P(y|x)` where the same feature
   values now warrant a different label. If this check shows decay *without* meaningful
   data or prediction drift, that pattern (stable inputs and outputs, but worse ground-truth
   accuracy) is itself evidence of concept drift rather than a data problem. If it shows
   decay *with* no drift signal at all and no plausible real-world explanation, suspect a
   pipeline bug (label join, feature computation, or a serving defect) before assuming the
   model itself needs retraining — drift detection is not a substitute for checking the
   pipeline. F1 needs the logged *class label* (`prediction`), not the score.
   ```python
   from sklearn.metrics import f1_score

   MIN_LABELED = 200   # rule of thumb: don't alert on a metric computed from a handful of labels

   def compute_delayed_performance(pred_log: pd.DataFrame, labels: pd.DataFrame) -> float | None:
       # one_to_one: a duplicated request_id (client retry, double label load) raises
       # instead of silently double-counting rows
       joined = pred_log.merge(labels[["request_id", "y_true"]], on="request_id",
                               how="inner", validate="one_to_one")
       if len(joined) < MIN_LABELED:
           return None   # not enough ground truth yet
       return f1_score(joined["y_true"], joined["prediction"], average="macro")

   pred_log = pd.read_json(LOG_PATH, lines=True)
   ground_truth = pd.read_csv("ground_truth.csv")   # request_id, y_true — arrives days/weeks later
   current_f1 = compute_delayed_performance(pred_log, ground_truth)
   baseline_f1 = 0.87   # captured at model registration time via experiment-tracking
   MAX_F1_DROP = 0.05   # set from the product's tolerance, not by default
   if current_f1 is not None and current_f1 < baseline_f1 - MAX_F1_DROP:
       print(f"Performance decay detected: {current_f1:.3f} vs baseline {baseline_f1:.3f}")
   ```
5. **Set a threshold and page someone, don't just log a number.** The webhook URL is a
   credential — read it from the environment/secret store, never hardcode or log it. A
   missing URL, a timeout, or a non-2xx response raises, so the scheduler marks the run
   failed instead of the alert silently going nowhere.
   ```python
   import math
   import os

   import requests

   def send_alert(text: str, webhook_url: str | None = None, timeout_s: float = 10.0) -> None:
       url = webhook_url or os.environ.get("DRIFT_ALERT_WEBHOOK_URL")
       if not url:
           raise RuntimeError("drift alert fired but DRIFT_ALERT_WEBHOOK_URL is not configured")
       resp = requests.post(url, json={"text": text}, timeout=timeout_s)
       resp.raise_for_status()

   def alert_if_drifted(drift_share: float, threshold: float = 0.3, webhook_url: str | None = None) -> bool:
       if math.isnan(drift_share):
           raise ValueError("drift_share is NaN; the drift check itself is broken")
       if drift_share <= threshold:
           return False
       send_alert(f":rotating_light: Data drift alert: {drift_share:.0%} of features drifted "
                  f"(threshold {threshold:.0%}).", webhook_url)
       return True
   ```
6. **Run it on a schedule** instead of remembering to check manually. The DAG file
   imports the helpers from a `monitoring.py` module that holds the step 1-5 constants and
   functions (`LOG_PATH`, `NON_FEATURES`, `drift_share_for`, `send_alert`,
   `alert_if_drifted`) but *not* their ad-hoc script lines — Airflow imports every DAG file
   at parse time, so module-level `read_csv`/report calls would run on every parse.
   ```python
   # monitoring_dag.py — Airflow DAG, runs the checks above daily.
   # `schedule=` works on Airflow >= 2.4 and 3.x (`schedule_interval=` is gone in 3.x). On
   # Airflow 3 the operator's home is airflow.providers.standard.operators.python; the
   # import below still works there as a deprecated alias.
   from datetime import datetime, timedelta

   import pandas as pd
   from airflow import DAG
   from airflow.operators.python import PythonOperator
   from monitoring import LOG_PATH, NON_FEATURES, alert_if_drifted, drift_share_for, send_alert

   def run_drift_check():
       reference = pd.read_csv("holdout_reference.csv")
       current = pd.read_json(LOG_PATH, lines=True).tail(5000)
       cols = [c for c in reference.columns if c not in NON_FEATURES]
       alert_if_drifted(drift_share_for(reference, current, cols))
       if drift_share_for(reference, current, ["score"]) > 0:
           send_alert(":rotating_light: Prediction (score) distribution drifted.")

   with DAG(
       dag_id="model_drift_monitoring",
       schedule="@daily",
       start_date=datetime(2026, 1, 1),
       catchup=False,
       default_args={"retries": 2, "retry_delay": timedelta(minutes=10)},
   ) as dag:
       PythonOperator(task_id="check_drift", python_callable=run_drift_check)
   ```

## Gotchas
- **Data drift, prediction drift, concept drift, and performance degradation are four
  different things** and treating any pair as synonyms leads to the wrong fix: data drift
  (input features shifted) and prediction drift (outputs shifted) are both detectable
  *without* labels but neither proves the model got worse; concept drift (`P(y|x)` itself
  changed) can only be inferred from ground-truth performance decay that shows up *without*
  a matching data/prediction drift signal; and performance degradation is the catch-all
  ground-truth symptom that can also be caused by something that isn't drift at all — a
  broken label join, a feature-pipeline bug, a schema change — so a degraded metric with no
  drift signal should send you to check the pipeline before you retrain.
- **Reference scores taken on the training rows** make prediction drift fire on
  undrifted traffic. A model is more confident on rows it was fit on than on new rows
  from the same distribution, and the gap is largest for high-capacity models: a random
  forest's in-sample scores pile up near 0 and 1. PSI on `score` then flags drift on
  day one and every day after. Score the reference on a validation/holdout split (or
  on early post-launch traffic once you trust it), never on the fit set.
- **A reference set frozen forever at training time** eventually flags nearly everything
  as "drifted" as the world naturally shifts — refresh the reference window periodically
  and re-baseline after every retrain, or you'll get alert fatigue and start ignoring it.
- **Evidently's API has changed across releases.** Code written for the pre-0.7 API
  (`from evidently.report import Report`, `from evidently.metric_preset import ...`,
  `report.as_dict()["metrics"][0]["result"]`) fails with `ModuleNotFoundError` on 0.7,
  where `Report.run()` returns a `Snapshot` and results are listed per metric by name
  (the old API lives on under `evidently.legacy`). Pin a range like `>=0.7,<0.8`, and look
  metrics up by name (step 2's `drifted_share`) rather than by list position.
- **evidently 0.7.23 does not import on pandas 3**, although its metadata allows any
  `pandas>=1.3.5`, so an unpinned install picks pandas 3. `from evidently import Report`
  then fails with `ImportError: cannot import name 'infer_dtype_from_object'` (a private
  pandas function removed in 3.0). Keep `pandas<3` next to the evidently pin until an
  evidently release supports pandas 3.
- **Ground truth often arrives late** (weeks, for churn/fraud/credit models) — you cannot
  wait on labels alone to detect a problem. Feature/prediction drift is your leading
  signal in the interim; treat label-based decay as confirmation, not the first alarm.
- **Alerting on raw statistical-test p-values at production data volumes** triggers
  constantly, since huge sample sizes make even trivial shifts "significant" — alert on
  effect-size thresholds (share of drifted columns, PSI > 0.2) instead of p < 0.05.
  Evidently's default per-column test depends on sample size (for small numeric samples it
  is a KS p-value), which is why step 2 sets `num_method="psi"` explicitly.
- **Logging raw, unredacted PII into the prediction log** for drift analysis creates a
  compliance problem of its own — hash or strip identifying fields before persisting.

## References
- [Evidently AI docs](https://docs.evidentlyai.com/) — drift and data-quality report reference.
- [Google: Rules of ML](https://developers.google.com/machine-learning/guides/rules-of-ml) — practical guidance on monitoring models in production.
- [Apache Airflow docs](https://airflow.apache.org/docs/apache-airflow/stable/index.html) — scheduling recurring monitoring jobs.
