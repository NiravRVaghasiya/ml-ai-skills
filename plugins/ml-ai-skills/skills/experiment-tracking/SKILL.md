---
name: experiment-tracking
display_name: Experiment Tracking
description: >
  Use when the user wants to log, compare, or reproduce ML experiments —
  tracking hyperparameters, metrics, and artifacts across training runs.
  Trigger phrases: "track my experiments", "log metrics with MLflow",
  "compare training runs", "set up W&B logging", "which run had the best
  accuracy". NOT for searching the hyperparameter space itself (see
  hyperparameter-tuning) or watching a deployed model's live performance
  (see ml-monitoring).
type: workflow
domain: mlops
level: beginner
lifecycle: stable
risk_level: low
evidence_level: official-documentation
last_verified: 2026-10-28
capabilities:
  - experiment-run-logging
  - hyperparameter-metric-tracking
  - run-comparison-querying
  - model-registry-staging
  - run-reproducibility
requires:
conflicts:
related:
  - hyperparameter-tuning
  - model-evaluation
  - ci-cd-for-ml
inputs: A training script or notebook whose hyperparameters, metrics, and artifacts need to be logged, plus a tracking backend (a local MLflow store or a shared MLflow/W&B server).
outputs: Logged, queryable runs grouped by experiment, a comparison table across runs, and a registered model version with an alias (e.g. @staging).
version_constraints:
  - "mlflow>=2.9: `set_registered_model_alias`, `search_runs(filter_string=attributes.status ...)`, `mlflow.sklearn.autolog(log_input_examples=...)`, `runs:/<id>/model` registration, and the `runs describe` / `artifacts download -d` CLI (executed against mlflow 3.16.1 with a SQLite store on 2026-10-27; `transition_model_version_stage` carries a deprecated-since-2.9.0 decorator there; behaviour on 2.x not tested)"
  - "mlflow==3.16.1: sklearn serialization defaults are `autolog(serialization_format='cloudpickle')` but `log_model`/`save_model(serialization_format='skops')` (read with inspect.signature and the mlflow/sklearn/__init__.py source on 2026-10-28; other releases not checked, including the one that switched log_model to skops); with skops 0.16.0, autolog(serialization_format='skops') on a RandomForestClassifier left a FAILED logged model (untrusted sklearn.tree._tree.Tree), and log_model(serialization_format='skops', skops_trusted_types=['sklearn.tree._tree.Tree']) logged and loaded one (executed 2026-10-28)"
  - "wandb>=0.13: `wandb.init(...)` as a context manager and `run.log` (read from the wandb 0.31.0 wheel source on 2026-10-27, where Run.__exit__ finishes with exit_code=1 on an exception; executed only against a fake module; the 0.13 minimum is not verified)"
---

## Overview
Turns ad-hoc training scripts into a searchable history of runs: every hyperparameter,
metric, and artifact gets logged against a run ID, so "what changed between the good
run and the bad run" is a query instead of a guess. This skill covers MLflow (with a W&B
equivalent) end to end — instrument a run, compare runs, and promote the best one to a
registry. The output is a reproducible, comparable record, not just a training script
that prints to stdout.

## Workflow
1. **Point at a tracking store and name the experiment.** Everything logged after this
   groups under one experiment so runs are comparable.
   ```python
   # pip install mlflow
   import os
   import mlflow

   # Local SQLite store for one person; for a team, export MLFLOW_TRACKING_URI pointing at the
   # shared tracking server (e.g. http://mlflow.internal:5000) so scripts *and* the CLI use it.
   mlflow.set_tracking_uri(os.environ.get("MLFLOW_TRACKING_URI", "sqlite:///mlflow.db"))
   mlflow.set_experiment("churn-prediction")
   ```
2. **Instrument the run.** Use autologging for the framework's own metrics, plus manual
   `log_params`/`log_metrics`/`log_artifact` for anything autolog can't see (custom
   preprocessing, business metrics, plots).
   ```python
   import matplotlib.pyplot as plt
   import mlflow
   from sklearn.ensemble import RandomForestClassifier
   from sklearn.metrics import ConfusionMatrixDisplay, accuracy_score, f1_score

   # X_train, X_test, y_train, y_test: your existing train/test split.
   # log_input_examples stores a few raw training rows next to the model; turn it off for PII.
   # autolog stores the model as cloudpickle: loading it runs code (see Gotchas)
   mlflow.sklearn.autolog(log_models=True, log_input_examples=True)

   with mlflow.start_run(run_name="rf-baseline") as run:
       # placeholder values: record the real `git rev-parse HEAD` and your dataset version
       mlflow.set_tags({"git_commit": "a1b2c3d", "dataset_version": "v3"})
       params = {"n_estimators": 200, "max_depth": 8}
       mlflow.log_params(params)

       clf = RandomForestClassifier(random_state=42, **params)
       clf.fit(X_train, y_train)
       preds = clf.predict(X_test)

       mlflow.log_metrics({
           "accuracy": accuracy_score(y_test, preds),
           "f1_macro": f1_score(y_test, preds, average="macro"),
       })
       fig = ConfusionMatrixDisplay.from_predictions(y_test, preds).figure_
       fig.savefig("confusion_matrix.png")   # log_artifact needs a file that exists
       plt.close(fig)
       mlflow.log_artifact("confusion_matrix.png")
   ```
3. **Compare runs to find the best one.** Query programmatically instead of eyeballing
   the UI once you have more than a handful of runs.
   ```python
   import mlflow

   runs = mlflow.search_runs(
       experiment_names=["churn-prediction"],
       # a run that crashed mid-training can still have logged a good-looking metric
       filter_string="attributes.status = 'FINISHED'",
       order_by=["metrics.f1_macro DESC"],
       max_results=5,
   )
   if runs.empty:
       raise RuntimeError("no FINISHED runs in experiment 'churn-prediction'")
   print(runs[["run_id", "params.n_estimators", "params.max_depth", "metrics.f1_macro"]])
   best_run_id = runs.iloc[0]["run_id"]
   ```
4. **Register the best model.** The registry gives the model a stable name and version
   history independent of the run that produced it. Point an alias at the version;
   consumers load `models:/churn-rf-classifier@staging`, so promoting or rolling back means
   moving the alias, not editing code.
   ```python
   from mlflow import MlflowClient

   client = MlflowClient()
   result = mlflow.register_model(
       model_uri=f"runs:/{best_run_id}/model",   # autolog names the model "model"; MLflow 3 resolves this to the run's logged model
       name="churn-rf-classifier",
   )
   # aliases replace the stage API (transition_model_version_stage, deprecated since MLflow 2.9)
   client.set_registered_model_alias(
       name="churn-rf-classifier", alias="staging", version=result.version,
   )
   ```
5. **(Alternative) Same idea in Weights & Biases**, if the team standardizes on W&B
   instead of MLflow.
   ```python
   # pip install wandb
   import wandb

   # reuses preds and confusion_matrix.png from step 2; the context manager finishes the run
   # (marked failed) even if logging raises, instead of leaving it dangling as "running"
   with wandb.init(project="churn-prediction", config={"n_estimators": 200, "max_depth": 8}) as run:
       run.log({"accuracy": accuracy_score(y_test, preds), "f1_macro": f1_score(y_test, preds, average="macro")})
       run.log({"confusion_matrix": wandb.Image("confusion_matrix.png")})
   ```
6. **Reproduce a past run.** Pull the exact params, environment, and artifacts back down
   instead of trying to remember what you ran. In MLflow 3 the model (with its
   `requirements.txt`/`python_env.yaml`) is a separate logged-model object, so the run's
   artifact download only returns plots and files you logged — fetch the model by URI.
   ```bash
   export MLFLOW_TRACKING_URI=sqlite:///mlflow.db   # the store the script used; the CLI can't see set_tracking_uri()
   mlflow runs describe --run-id <best_run_id>                                  # params, metrics, tags as JSON
   mlflow artifacts download --run-id <best_run_id> -d ./reproduced             # run artifacts (plots, ...)
   mlflow artifacts download --artifact-uri "models:/churn-rf-classifier@staging" -d ./reproduced/model
   ```

## Gotchas
- **Autolog doesn't see everything.** Custom preprocessing, feature engineering, or data
  versioning that happens outside the estimator's `fit()` call won't be captured — log
  those params/tags manually or the "best run" isn't actually reproducible.
- **The default tracking store is local to your machine** — a `./mlruns` folder in older
  MLflow releases, a `sqlite:///mlflow.db` file in the working directory in recent 3.x
  releases (observed on MLflow 3.16.1). If you forget to point at a shared server or
  artifact store, your teammates simply can't see your runs — they don't error, they just
  look empty on their machine. The same applies to the `mlflow` CLI, which reads
  `MLFLOW_TRACKING_URI`, not what a script passed to `set_tracking_uri()`.
- **Logging raw datasets or every checkpoint as an artifact bloats the backend store**
  fast. Log data *references* (path + version hash) and representative samples, not the
  full dataset, on every run. `log_input_examples=True` also copies raw rows into the
  artifact store — a data-exposure issue if the features contain PII.
- **Unnamed, untagged runs are indistinguishable** six weeks later. Always set `run_name`
  and tags like git commit and dataset version — "Run 47" tells you nothing.
- **Registry aliases and stage labels are just metadata** — pointing `@production` (or the
  legacy "Production" stage) at a version does not deploy anything or gate anything by itself;
  that enforcement is the job of ci-cd-for-ml and model-deployment. The stage API
  (`transition_model_version_stage`) is deprecated since MLflow 2.9 in favour of aliases
  (step 4) and tags — don't build new automation on stages.
- **Loading an sklearn model logged as (cloud)pickle unpickles it**, which can execute
  arbitrary code, and the default format depends on the entry point. On MLflow 3.16.1,
  `mlflow.sklearn.log_model`/`save_model` default to `serialization_format="skops"`, but
  `mlflow.sklearn.autolog` defaults to `"cloudpickle"`, so step 2's model is a pickle.
  Switching autolog to `"skops"` is not a drop-in fix for tree models: autolog has no
  `skops_trusted_types`, skops treats `sklearn.tree._tree.Tree` as untrusted, and the
  random forest's logged model is left `FAILED` with no usable artifact (one warning, no
  exception). For skops, set
  `autolog(log_models=False)` and call `mlflow.sklearn.log_model(clf, name="model",
  serialization_format="skops", skops_trusted_types=["sklearn.tree._tree.Tree"])`.
  The trusted list is stored in the model's `MLmodel` file, so either way, only load
  models from tracking servers and registries whose writers you trust.

## References
- [MLflow Tracking docs](https://mlflow.org/docs/latest/tracking.html) — canonical guide to runs, experiments, and autologging.
- [MLflow Model Registry docs](https://mlflow.org/docs/latest/model-registry.html) — versioning and stage transitions for registered models.
- [Weights & Biases docs — Experiments](https://docs.wandb.ai/guides/track/) — the W&B equivalent of steps 2–3.
