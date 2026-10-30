---
name: ci-cd-for-ml
display_name: CI/CD for ML
description: >
  Use when the user wants to automate testing, reproducibility, or release
  gating for an ML codebase or model — CI pipelines that must pass before a
  model or pipeline change ships. Trigger phrases: "set up CI for my ML repo",
  "add tests for my training pipeline", "automate model retraining and
  release", "gate deployment on model quality", "make my training run
  reproducible". NOT for the runtime serving infrastructure itself (see
  model-deployment) or logging individual training runs (see
  experiment-tracking).
type: workflow
domain: mlops
level: intermediate
lifecycle: stable
risk_level: medium
evidence_level: established-practice
last_verified: 2026-10-28
capabilities:
  - ml-ci-pipeline-setup
  - data-contract-testing
  - model-quality-gating
  - reproducible-environment-pinning
  - release-promotion-automation
requires:
conflicts:
related:
  - experiment-tracking
  - model-deployment
  - data-pipelines
inputs: An ML codebase (training pipeline and model code) in a version-controlled repo with a CI runner (e.g. GitHub Actions) available.
outputs: A CI workflow that blocks merges/releases on data-contract and model-quality test failures, plus a gated release job that promotes only passing models.
version_constraints:
  - "pytest>=7: autouse fixtures in conftest.py (executed against pytest 9.1.1 on 2026-10-27; lower bound not verified)"
  - "scikit-learn>=1.0: roc_auc_score, f1_score(average='macro') (executed against scikit-learn 1.9.0 on 2026-10-27; lower bound not verified)"
  - "mlflow>=2.9: registered-model aliases replace deprecated stages; step 5 uses MlflowClient.search_model_versions with a tags.git_sha filter, get_model_version_by_alias, set_registered_model_alias and pyfunc.load_model (mlflow-skinny 2.16.2 source read on 2026-10-27 marks transition_model_version_stage @deprecated since 2.9.0; step 5 executed against mlflow 3.16.1 (local example venv) on a SQLite store on 2026-10-28 in an opt-in slow test, 2.x not tested; docs/KNOWN-ISSUES.md #8 reports test_ci_cd_for_ml passing against the 3.17.2 pin in a separate venv on 2026-10-28; 2.9 lower bound not verified)"
  - "pip-audit==2.10.1: pip-audit --strict --disable-pip -r requirements.txt on a hashed lockfile (executed on 2026-10-28: exit 1 on mlflow==2.16.2, exit 0 on mlflow==3.17.2)"
---

## Overview
Applies standard software CI/CD discipline to an ML codebase, plus the ML-specific
pieces vanilla CI/CD doesn't cover: pinned/reproducible environments, data-contract
tests, and a model-quality gate that can actually fail a build. Covers pytest-based
tests for data and model quality, a GitHub Actions workflow that runs them on every PR,
and a release job that only promotes a model when the quality gate is green. The output
is a pipeline where a bad data change or a regressed model can't merge, not just a repo
that has tests somewhere.

## Workflow
1. **Pin the environment.** CI reproducibility starts with exact versions — of *every*
   package, not just the ones you import — and fixing every source of randomness so tests
   aren't flaky. Keep direct dependencies in `requirements.in` and compile a fully pinned,
   hash-checked `requirements.txt` from it; exact pins on direct dependencies alone still
   let transitive dependencies float.
   A lockfile also freezes known vulnerabilities in place, so audit it in CI (step 4)
   and treat a new advisory as a reason to bump.
   ```txt
   # requirements.in — direct dependencies, exact versions (no OSV advisories on 2026-10-28;
   # the previous mlflow==2.16.2 and pytest==8.3.3 pins have published ones)
   scikit-learn==1.5.2
   pandas==2.2.3
   numpy==2.2.3
   mlflow==3.17.2
   pytest==9.1.1
   pip-audit==2.10.1
   ```
   ```bash
   # pip install pip-tools — resolves and pins transitive dependencies, with hashes
   pip-compile --generate-hashes --output-file requirements.txt requirements.in
   ```
   ```python
   # conftest.py
   import random

   import numpy as np
   import pytest

   SEED = 42

   @pytest.fixture(autouse=True)
   def _seed_everything():
       # Seeds the *global* generators only. np.random.default_rng() and estimators'
       # random_state are separate: pass SEED to them explicitly in the pipeline.
       random.seed(SEED)
       np.random.seed(SEED)
       try:
           import torch
       except ImportError:
           return
       torch.manual_seed(SEED)
   ```
2. **Write data-contract tests**, not just code tests — schema and dtypes, value ranges,
   nulls, and leakage. Test the feature list the pipeline *actually trains on*
   (`FEATURE_COLUMNS`), not one the test derives itself: a test that builds its own
   feature list by excluding the target and then asserts the target is excluded passes on
   every input and catches nothing.
   ```python
   # tests/test_data_contract.py
   import pandas as pd
   from sklearn.metrics import roc_auc_score

   from src.pipeline import FEATURE_COLUMNS, load_training_data

   TARGET = "churned"
   CONTRACT = {"customer_id": "int64", "orders_30d": "int64", "spend_30d": "float64", "churned": "int64"}
   # Identifiers and columns only known after the outcome must never be model inputs.
   FORBIDDEN_FEATURES = {TARGET, "customer_id", "churn_date", "cancellation_reason"}
   # Rule of thumb: one raw feature separating the target almost perfectly on its own is far
   # more often leakage than signal; raise this if your problem is genuinely that easy.
   MAX_SINGLE_FEATURE_AUC = 0.98

   def test_schema_matches_contract():
       df = load_training_data()
       missing = set(CONTRACT) - set(df.columns)
       assert not missing, f"missing columns: {sorted(missing)}"
       wrong = {c: str(df[c].dtype) for c, t in CONTRACT.items() if str(df[c].dtype) != t}
       assert not wrong, f"dtype drift vs contract: {wrong}"

   def test_values_within_contract():
       df = load_training_data()
       assert len(df) > 0, "training data is empty"
       assert df["customer_id"].is_unique, "duplicate customer rows"
       assert (df[["orders_30d", "spend_30d"]] >= 0).all().all(), "negative counts/spend"
       assert set(df[TARGET].unique()) <= {0, 1}, "target is not binary 0/1"

   def test_no_nulls_in_required_columns():
       df = load_training_data()
       assert df[["customer_id", TARGET]].isna().sum().sum() == 0

   def test_no_target_leakage_into_features():
       leaked = FORBIDDEN_FEATURES & set(FEATURE_COLUMNS)
       assert not leaked, f"forbidden columns used as features: {sorted(leaked)}"
       df = load_training_data()
       if df[TARGET].nunique() < 2:
           return   # single-class data: AUC undefined; the quality gate will fail loudly instead
       for col in FEATURE_COLUMNS:
           if not pd.api.types.is_numeric_dtype(df[col]):
               continue
           rows = df[[col, TARGET]].dropna()
           auc = roc_auc_score(rows[TARGET], rows[col])
           assert max(auc, 1 - auc) < MAX_SINGLE_FEATURE_AUC, (
               f"{col} alone separates {TARGET} (AUC {auc:.3f}); check it for leakage"
           )
   ```
3. **Write a model-quality gate test.** This is what makes CI able to reject a model
   regression, not just a syntax error.
   ```python
   # tests/test_model_quality.py
   from sklearn.metrics import f1_score

   from src.pipeline import load_training_data, split, train_model

   MIN_ACCEPTABLE_F1 = 0.80   # baseline captured via experiment-tracking

   def test_model_beats_quality_bar():
       df = load_training_data()
       X_train, X_test, y_train, y_test = split(df)
       model = train_model(X_train, y_train)
       preds = model.predict(X_test)
       score = f1_score(y_test, preds, average="macro")
       assert score >= MIN_ACCEPTABLE_F1, f"F1 {score:.3f} below bar {MIN_ACCEPTABLE_F1}"
   ```
4. **Wire it into CI**, running on every pull request with a read-only token, a timeout,
   and the cheap config check first so it really does fail fast.
   ```yaml
   # .github/workflows/ci.yml
   name: ml-ci
   on:
     pull_request:
     push:
       branches: [main]

   permissions:
     contents: read          # the default GITHUB_TOKEN may be read-write; tests need neither

   jobs:
     test:
       runs-on: ubuntu-latest
       timeout-minutes: 30
       steps:
         # For supply-chain safety pin third-party actions to a full commit SHA (with the
         # tag in a comment) instead of a movable tag like @v4.
         - uses: actions/checkout@v4
         - uses: actions/setup-python@v5
           with:
             python-version: "3.11"
         - run: pip install --require-hashes -r requirements.txt
         # audit the lockfile as written (--disable-pip: no re-resolution); an advisory fails CI
         - run: pip-audit --strict --disable-pip -r requirements.txt
         - run: python -m src.validate_pipeline_config   # fail fast on config errors
         - run: pytest tests/ -v --maxfail=1
   ```
5. **Gate the release on the artifact you ship, not on a model CI trained.** Step 3's
   test retrains a model on a small sample, so passing it says the *code* still learns;
   it says nothing about the registry version trained on the full data. The release job
   therefore loads the exact version registered for *this* commit, scores it on a fixed
   holdout the training job never saw, compares it with the current `champion`, and moves
   the alias to that same version only if it passes. It runs in a protected `environment`
   (required reviewers, and the only place the registry credentials are available). Use
   MLflow registry aliases: registry stages (`--stage Production`,
   `transition_model_version_stage`) are deprecated since MLflow 2.9.
   ```python
   # src/release.py — the training job registers each version with tags={"git_sha": <commit>}
   import argparse
   import re

   import mlflow
   from mlflow import MlflowClient
   from mlflow.exceptions import MlflowException
   from sklearn.metrics import f1_score

   from src.pipeline import load_holdout_data

   MODEL_NAME = "churn-model"
   MIN_ACCEPTABLE_F1 = 0.80        # the same bar as tests/test_model_quality.py
   MAX_DROP_VS_CHAMPION = 0.01     # set from the product's tolerance, not by default

   def holdout_f1(model_uri: str, X, y) -> float:
       # a pickled model runs code on load: only load from your own registry
       return f1_score(y, mlflow.pyfunc.load_model(model_uri).predict(X), average="macro")

   def release(git_sha: str, alias: str = "champion", client: MlflowClient | None = None) -> str:
       if not re.fullmatch(r"[0-9a-f]{40}", git_sha):   # it is interpolated into a filter
           raise ValueError(f"not a full git SHA: {git_sha!r}")
       client = client or MlflowClient()
       found = client.search_model_versions(f"name = '{MODEL_NAME}' AND tags.git_sha = '{git_sha}'")
       if len(found) != 1:
           raise SystemExit(f"expected 1 {MODEL_NAME} version tagged git_sha={git_sha}, found {len(found)}")
       candidate = str(found[0].version)
       X, y = load_holdout_data()   # fixed and versioned; never used for training
       score = holdout_f1(f"models:/{MODEL_NAME}/{candidate}", X, y)
       if score < MIN_ACCEPTABLE_F1:
           raise SystemExit(f"version {candidate}: holdout F1 {score:.3f} < bar {MIN_ACCEPTABLE_F1}")
       try:
           current = str(client.get_model_version_by_alias(MODEL_NAME, alias).version)
       except MlflowException:
           current = None   # first release: nothing to compare against
       if current not in (None, candidate):
           baseline = holdout_f1(f"models:/{MODEL_NAME}/{current}", X, y)
           if score < baseline - MAX_DROP_VS_CHAMPION:
               raise SystemExit(f"version {candidate}: F1 {score:.3f} vs {alias} v{current} {baseline:.3f}")
       client.set_registered_model_alias(MODEL_NAME, alias, candidate)   # the version scored above
       return candidate

   if __name__ == "__main__":
       parser = argparse.ArgumentParser()
       parser.add_argument("--git-sha", required=True)
       parser.add_argument("--alias", default="champion")
       args = parser.parse_args()
       print(f"{MODEL_NAME}@{args.alias} -> version {release(args.git_sha, args.alias)}")
   ```
   ```yaml
   # .github/workflows/release.yml
   name: ml-release
   on:
     push:
       branches: [main]

   permissions:
     contents: read

   concurrency:
     group: ml-release        # two merges in quick succession must not promote out of order
     cancel-in-progress: false

   jobs:
     quality-gate:
       runs-on: ubuntu-latest
       timeout-minutes: 30
       steps:
         - uses: actions/checkout@v4
         - uses: actions/setup-python@v5
           with:
             python-version: "3.11"
         - run: pip install --require-hashes -r requirements.txt
         - run: pytest tests/test_data_contract.py tests/test_model_quality.py -v   # code gate

     promote:
       needs: quality-gate
       runs-on: ubuntu-latest
       timeout-minutes: 15
       environment: production    # configure required reviewers on this environment
       steps:
         - uses: actions/checkout@v4
         - uses: actions/setup-python@v5
           with:
             python-version: "3.11"
         - run: pip install --require-hashes -r requirements.txt
         - name: Score this commit's registered version, then point "champion" at it
           env:
             MLFLOW_TRACKING_URI: ${{ secrets.MLFLOW_TRACKING_URI }}
             MLFLOW_TRACKING_TOKEN: ${{ secrets.MLFLOW_TRACKING_TOKEN }}
           run: python -m src.release --git-sha "${{ github.sha }}" --alias champion
   ```

## Gotchas
- **Unpinned dependencies** (`scikit-learn>=1.5`) make CI green today and red in three
  months when a dependency ships a breaking change on an unrelated PR. Exact pins on your
  direct dependencies are not enough — transitive ones still float — so install from a
  compiled, hash-checked lockfile and bump it deliberately, in its own PR.
- **A "model quality" test that retrains on the full dataset in CI** can exceed CI
  timeouts and burns compute on every PR — train on a small, fixed, checked-in sample for
  the gate, and run full retraining as a separate scheduled job (see data-pipelines).
- **Not seeding every source of randomness** (Python's `random`, NumPy, and
  framework-specific seeds like `torch.manual_seed`) makes the quality-gate test flaky —
  it passes on some runs and fails on others with no code change, and teams learn to
  ignore it.
- **Testing only code, never the data contract**, lets a broken upstream data change ship
  silently — the pipeline runs fine and all code tests pass; it just trains on garbage.
  The same goes for contract tests that check a list the test built itself (e.g. "the
  target is not in `[c for c in df.columns if c != target]`") — they can never fail.
- **CI credentials and mutable references are attack surface.** Without a `permissions:`
  block the job's `GITHUB_TOKEN` gets the repository's default scope, which can be
  read-write; movable action tags (`@v4`) can be repointed upstream; and registry
  credentials exposed to PR jobs can be exfiltrated by a malicious PR. Keep secrets in a
  protected `environment` used only by the release job, and pin actions to commit SHAs.
- **A gate that scores a different model from the one it promotes.** If CI trains its
  own model for the quality test and the release job then moves an alias to a version
  trained elsewhere, a registry artifact with a data bug, another seed, or other
  hyperparameters ships whenever the CI model clears the bar. Score the exact version
  you are about to promote, as step 5's `release` does, and promote that version.
- **Gating release on a single aggregate metric** without a slice-level regression check
  against the current production model can let a model that's meaningfully worse on a
  minority class or critical segment pass, because the aggregate score still clears the
  bar.

## References
- [GitHub Actions docs](https://docs.github.com/en/actions) — workflow syntax, jobs, and triggers.
- [pytest docs](https://docs.pytest.org/en/stable/) — fixtures, parametrization, and test organization.
- [Google: Rules of ML](https://developers.google.com/machine-learning/guides/rules-of-ml) — practical guidance that informs the testing/gating practices above.
