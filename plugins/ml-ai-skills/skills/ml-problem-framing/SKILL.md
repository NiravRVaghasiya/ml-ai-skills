---
name: ml-problem-framing
display_name: ML Problem Framing
description: >
  Use when the user wants to turn a vague business ask into a well-posed ML
  problem before any modeling starts. Trigger phrases: "how should I frame
  this as an ML problem", "is this even a good fit for ML", "what should my
  target variable be", "what metric should I optimize for", "should this be
  classification or regression". NOT for feature construction once the
  problem is framed (see feature-engineering), and NOT for choosing/tuning a
  specific model family (see supervised-learning).
type: workflow
domain: foundations
level: intermediate
lifecycle: stable
risk_level: low
evidence_level: established-practice
last_verified: 2026-10-27
capabilities:
  - problem-spec-definition
  - label-leakage-audit
  - ml-task-formulation
  - baseline-establishment
  - offline-vs-business-metric-alignment
  - class-imbalance-check
requires:
conflicts:
related:
  - feature-engineering
  - supervised-learning
  - model-evaluation
inputs: A vague business ask or request to apply ML, before any data exploration or modeling has begun.
outputs: A structured problem spec (unit, horizon, label, metric, baseline), a leakage-audited label definition, and a non-ML baseline score to beat.
version_constraints:
  - "pandas>=2.0: Series.between(inclusive='right'), DatetimeIndex.where, pd.to_timedelta(unit='D') (executed against pandas 2.2.3 on 2026-10-27; lower bound not verified)"
  - "scikit-learn>=1.0: DummyClassifier(strategy='prior'), train_test_split(stratify=), average_precision_score (executed against scikit-learn 1.9.0 on 2026-10-27; lower bound not verified)"
---

## Overview
Converts an ambiguous business request ("reduce churn," "flag bad
transactions") into a precisely specified, checkable ML problem: what unit is
being predicted, over what horizon, using what label, judged against what
metric, and beating what baseline. Most failed ML projects fail here, not at
the modeling step — this workflow front-loads the questions that prevent
building the wrong thing or leaking the answer into the features.

## Workflow
1. **Write down a structured problem spec before touching data.** Forcing
   every field to be filled in surfaces ambiguity (undefined horizon, no
   metric, no decision) immediately instead of after a model is built.
   ```python
   from dataclasses import dataclass

   @dataclass
   class ProblemSpec:
       business_question: str
       decision_made_from_prediction: str
       unit_of_prediction: str      # e.g. "customer", "transaction"
       prediction_horizon: str      # e.g. "next 30 days"
       ml_task: str                 # classification | regression | ranking | clustering
       label_definition: str
       primary_offline_metric: str
       business_metric: str
       baseline_to_beat: str

   spec = ProblemSpec(
       business_question="Which customers are likely to cancel their subscription?",
       decision_made_from_prediction="Trigger a retention offer for top-risk customers",
       unit_of_prediction="customer",
       prediction_horizon="next 30 days",
       ml_task="binary classification",
       label_definition="churn=1 if the customer cancels within 30 days of the scoring date",
       primary_offline_metric="PR-AUC (positive class is rare)",
       business_metric="monthly churn rate reduction among treated customers",
       baseline_to_beat="existing rule: flag if support_tickets_30d >= 3",
   )
   print(spec)
   ```
2. **Check feasibility on real data before committing.** Confirm the label
   actually exists, the class balance isn't degenerate, and features aren't
   mostly missing.
   ```python
   import numpy as np
   import pandas as pd

   # Synthetic stand-in so the example runs end to end — load your real table here.
   rng = np.random.default_rng(42)
   n = 5000
   df = pd.DataFrame({
       "customer_id": np.arange(n),
       "tenure_months": rng.integers(1, 60, n),
       "support_tickets_30d": rng.poisson(0.5, n),
       "monthly_spend": rng.normal(50, 15, n).clip(5, None),
   })
   churn_prob = 1 / (1 + np.exp(-(0.8 * df["support_tickets_30d"]
                                  - 0.03 * df["tenure_months"] - 1.5)))
   df["churn"] = rng.binomial(1, churn_prob)

   print(f"rows: {len(df):,}")
   print(df["churn"].value_counts(normalize=True))       # class balance check
   print(df.isna().mean().sort_values(ascending=False))   # feature availability

   def require_both_classes(label: pd.Series) -> None:
       """A stratified split and PR-AUC both need at least 2 examples of each class."""
       class_counts = label.value_counts()
       if len(class_counts) < 2 or class_counts.min() < 2:
           raise ValueError(f"label is degenerate: {class_counts.to_dict()} — revisit the label or horizon")

   require_both_classes(df["churn"])   # re-run on the final label in step 4
   ```
3. **Pick the ML formulation with an explicit decision rule, not by default.**
   The task type determines everything downstream (loss, metric, model
   family) — decide it deliberately.
   ```python
   def choose_formulation(label_type: str, needs_ranking: bool, has_labels: bool) -> str:
       if not has_labels:
           return "unsupervised (clustering / anomaly detection) — no ground truth"
       if needs_ranking:
           return "ranking / recommendation — relative order matters more than the raw score"
       if label_type == "continuous":
           return "regression"
       if label_type == "categorical":
           return "classification"
       raise ValueError(f"Unrecognized label_type: {label_type}")

   print(choose_formulation(label_type="categorical", needs_ranking=False, has_labels=True))
   ```
4. **Build the label with an explicit time cutoff and audit for leakage.**
   The label must come only from events *after* the scoring moment, and every
   feature only from information available *as of* it — getting this wrong is
   one of the most common ways ML problems are mis-framed.
   ```python
   scoring_date = pd.Timestamp("2026-01-01")
   horizon = pd.Timedelta(days=30)

   # Synthetic raw timestamps, consistent with the simulated churn flag above.
   df["signup_date"] = scoring_date - pd.to_timedelta(df["tenure_months"] * 30, unit="D")
   cancel_offset = pd.to_timedelta(rng.integers(1, 31, len(df)), unit="D")
   df["cancel_date"] = (scoring_date + cancel_offset).where(df["churn"].eq(1).to_numpy())

   # Label: cancellation strictly after the cutoff and within the horizon.
   df["churn"] = df["cancel_date"].between(
       scoring_date, scoring_date + horizon, inclusive="right").astype(int)
   require_both_classes(df["churn"])   # this is the label step 5 trains on

   def audit_feature_asof(feature_cols, feature_asof, cutoff):
       """Fail unless every feature declares the latest timestamp of the data it
       was computed from, and none is later than the cutoff. Raises instead of
       `assert`, which `python -O` strips. A null timestamp (e.g. .max() of an
       empty or all-NaT column) counts as undeclared: `NaT > cutoff` is False."""
       missing = sorted(set(feature_cols) - {f for f, ts in feature_asof.items()
                                             if not pd.isna(ts)})
       if missing:
           raise ValueError(f"no as-of timestamp recorded for features: {missing}")
       late = {f: str(ts) for f, ts in feature_asof.items() if ts > cutoff}
       if late:
           raise ValueError(f"features use data from after {cutoff}: {late}")

   feature_cols = ["tenure_months", "support_tickets_30d", "monthly_spend"]
   # In production, take these from your feature pipeline's metadata.
   feature_asof = {
       "tenure_months": df["signup_date"].max(),
       "support_tickets_30d": scoring_date,          # window (scoring_date - 30d, scoring_date]
       "monthly_spend": scoring_date - pd.Timedelta(days=1),
   }
   audit_feature_asof(feature_cols, feature_asof, scoring_date)
   # cancel_date feeds the label only — it (or anything derived from it) is never a feature.
   ```
5. **Establish a non-ML baseline before building any model.** If a simple
   rule or the prior class distribution already scores well, that's the bar
   any model must clear to justify its complexity.
   ```python
   from sklearn.dummy import DummyClassifier
   from sklearn.model_selection import train_test_split
   from sklearn.metrics import average_precision_score

   X = df[feature_cols]   # exactly the features audited in step 4
   y = df["churn"]
   X_train, X_test, y_train, y_test = train_test_split(
       X, y, test_size=0.2, stratify=y, random_state=42)

   # Existing non-ML rule
   rule_based_score = (X_test["support_tickets_30d"] >= 3).astype(int)
   print("Rule-based PR-AUC:", average_precision_score(y_test, rule_based_score))

   # Trivial ML baseline (predicts the prior class distribution)
   dummy = DummyClassifier(strategy="prior").fit(X_train, y_train)
   dummy_score = dummy.predict_proba(X_test)[:, 1]
   print("Dummy-classifier PR-AUC:", average_precision_score(y_test, dummy_score))
   ```
6. **Separate the offline metric from the business metric, and gate launch
   on both.** A model can win on the offline metric and still be unsafe or
   useless in production if it violates a guardrail.
   ```python
   def passes_launch_guardrails(offline_pr_auc: float, min_pr_auc: float,
                                 false_positive_rate: float, max_fpr: float) -> bool:
       """A metric win offline isn't sufficient on its own — guardrails
       protect the business metric (e.g. don't spam retention offers)."""
       return offline_pr_auc >= min_pr_auc and false_positive_rate <= max_fpr

   ship_it = passes_launch_guardrails(
       offline_pr_auc=0.42, min_pr_auc=0.35,
       false_positive_rate=0.08, max_fpr=0.10,
   )
   print("Ready to ship:", ship_it)
   ```

## Gotchas
- **Jumping to model choice before defining the decision.** If nobody can say
  what action changes based on the prediction, the problem isn't framed yet —
  "predict churn" is not a spec; "flag the top 5% risk customers for a
  retention call" is.
- **Temporal leakage in the label definition.** Using a feature that is only
  known *after* the outcome occurred (e.g. "days until cancellation" as a
  churn-prediction feature) produces a model that looks great offline and is
  useless in production — always enforce an explicit as-of cutoff.
- **Optimizing an offline metric that doesn't track the business metric.**
  Accuracy on a 98%-negative-class churn dataset can hit 98% by predicting
  "never churns" — that's why the offline metric must be chosen deliberately
  (e.g. PR-AUC, recall@k) to match the real decision cost structure.
- **Skipping the non-ML baseline.** Without a baseline, there is no way to
  tell whether a complex model is actually earning its complexity, or whether
  a one-line heuristic would already have captured most of the value — the
  actual gap depends on the problem, so measure it rather than assuming a
  model will beat the baseline by any particular margin.
- **Ignoring the asymmetry between error types.** A false negative (missed
  churner) and a false positive (wasted retention offer) rarely cost the same
  — that asymmetry should drive the metric and decision threshold, not be
  bolted on after the model is trained.

## References
- [Google — Introduction to ML Problem Framing](https://developers.google.com/machine-learning/problem-framing) — a full course specifically on this step, from the team that also wrote Rules of ML.
- [Zinkevich — Rules of Machine Learning](https://developers.google.com/machine-learning/guides/rules-of-ml) — practical, battle-tested guidance on when/how to introduce ML and what to check before scaling complexity.
