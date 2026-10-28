---
name: supervised-learning
display_name: Supervised Learning
description: >
  Use when the user wants to pick and fit a regression or classification model.
  Trigger phrases: "which model should I use", "train a classifier", "fit a
  regression model", "baseline vs. a real model", "compare a few algorithms",
  "predict a numeric/categorical target". NOT for building the input features
  (see feature-engineering), scoring/comparing models rigorously (see
  model-evaluation), or tuning the winning model's hyperparameters (see
  hyperparameter-tuning).
type: workflow
domain: classical-ml
level: intermediate
lifecycle: stable
risk_level: low
evidence_level: established-practice
last_verified: 2026-10-27
capabilities:
  - baseline-model-comparison
  - classifier-model-selection
  - regression-model-selection
  - cross-validated-model-comparison
  - class-imbalance-handling
requires:
conflicts:
related:
  - feature-engineering
  - model-evaluation
  - hyperparameter-tuning
  - data-preprocessing
inputs: A feature-engineered train/test split (see feature-engineering) with a numeric or categorical target.
outputs: A fitted candidate model, or a ranked shortlist of candidates with cross-validated scores, ready for tuning or rigorous evaluation.
version_constraints:
  - "scikit-learn>=1.4: DummyClassifier, LogisticRegression, HistGradientBoostingClassifier, SVC, StratifiedKFold, cross_val_score(scoring=roc_auc) as used in steps 1-6, with SVC scored via decision_function (no probability=True) (executed against scikit-learn 1.9.0 on 2026-10-27; lower bound not verified)"
---

## Overview
Goes from a clean, feature-engineered matrix to a fitted model, via a disciplined
spot-check: always start with a trivial baseline, then compare a linear model
against a tree ensemble under the same cross-validation splits, and pick a winner
by a defensible margin — not by eyeballing one fold. Model *scoring* mechanics
(metrics, calibration, CV design) live in `model-evaluation`; this skill is about
which estimator to fit and how to compare candidates fairly.

## Workflow
1. **Always fit a dumb baseline first.** Without one, a 0.75 R² or 80% accuracy
   means nothing — you need a floor to beat.
   ```python
   from sklearn.dummy import DummyClassifier, DummyRegressor
   from sklearn.model_selection import cross_val_score

   # Binary target assumed ("roc_auc"). A constant predictor scores ROC-AUC 0.5 by
   # construction; the baseline is most informative for accuracy, F1 or R^2.
   baseline = DummyClassifier(strategy="most_frequent")
   baseline_scores = cross_val_score(baseline, X_train, y_train, cv=5, scoring="roc_auc")
   print("baseline:", baseline_scores.mean())
   ```
2. **Fit a linear model as the second baseline.** Fast, interpretable, and a strong
   floor for anything roughly linearly separable — it also tells you whether feature
   scaling/engineering is doing its job.
   ```python
   from sklearn.linear_model import LogisticRegression
   from sklearn.pipeline import Pipeline
   from sklearn.preprocessing import StandardScaler

   linear = Pipeline([
       ("scale", StandardScaler()),
       ("model", LogisticRegression(max_iter=1000, class_weight="balanced")),
   ])
   linear_scores = cross_val_score(linear, X_train, y_train, cv=5, scoring="roc_auc")
   print("linear:", linear_scores.mean())
   ```
3. **Fit a tree ensemble.** Handles non-linearities and interactions without manual
   feature engineering, is scale-invariant, and is a strong default for tabular data.
   ```python
   from sklearn.ensemble import HistGradientBoostingClassifier

   trees = HistGradientBoostingClassifier(random_state=42)
   tree_scores = cross_val_score(trees, X_train, y_train, cv=5, scoring="roc_auc")
   print("trees:", tree_scores.mean())
   ```
4. **Compare candidates under identical folds, not separate ones.** Reuse a single
   `cv` splitter object across every `cross_val_score` call so the exact same rows
   are held out for every model — otherwise differences may just be fold luck.
   ```python
   from sklearn.model_selection import StratifiedKFold
   from sklearn.svm import SVC

   cv = StratifiedKFold(n_splits=5, shuffle=True, random_state=42)
   candidates = {
       "baseline": baseline,
       "logistic": linear,
       "hist_gbm": trees,
       # The roc_auc scorer falls back to decision_function, so SVC needs no
       # probability=True (deprecated in scikit-learn 1.9; it also ran an extra
       # internal 5-fold CV on every fit).
       "svm_rbf": Pipeline([("scale", StandardScaler()), ("model", SVC())]),
   }
   fold_scores = {
       name: cross_val_score(est, X_train, y_train, cv=cv, scoring="roc_auc")
       for name, est in candidates.items()
   }
   results = {name: scores.mean() for name, scores in fold_scores.items()}
   for name, scores in sorted(fold_scores.items(), key=lambda kv: -kv[1].mean()):
       print(f"{name}: {scores.mean():.3f} +/- {scores.std():.3f}")
   ```
5. **Handle class imbalance at the model level, not just via metrics.** Reweight
   or resample before assuming a fancier model is needed.
   ```python
   from sklearn.ensemble import RandomForestClassifier

   balanced_rf = RandomForestClassifier(
       n_estimators=300, class_weight="balanced_subsample", random_state=42
   )
   ```
6. **Fit the chosen model on the full training set for handoff.** Once a winner is
   picked (by a margin that survives `model-evaluation`'s scrutiny — see that skill
   for how to judge "better"), fit it once on all of train before tuning or shipping.
   ```python
   import math
   from sklearn.base import clone

   # cross_val_score records a failed fit or score as nan (error_score=np.nan, only a
   # warning), and sorting keys that contain nan gives an arbitrary order — refuse to
   # rank rather than silently ship the wrong model.
   non_finite = [name for name, score in results.items() if not math.isfinite(score)]
   if non_finite:
       raise ValueError(f"non-finite CV score for {non_finite}: fix or drop them before ranking")
   ranked = sorted(results, key=results.get, reverse=True)
   best_name, runner_up = ranked[0], ranked[1]
   if best_name == "baseline":
       raise ValueError("no candidate beat the dummy baseline; revisit features before shipping")
   margin = results[best_name] - results[runner_up]
   print(f"winner={best_name} margin over {runner_up}={margin:.3f} "
         f"(winner fold std={fold_scores[best_name].std():.3f})")
   # clone(): fit a fresh copy so the candidate objects used in CV stay unfitted.
   final_model = clone(candidates[best_name]).fit(X_train, y_train)
   ```

## Gotchas
- **Comparing models on different CV folds.** An int `cv=5` is deterministic (an
  unshuffled `StratifiedKFold` for classifiers, `KFold` otherwise), so repeated calls
  do share folds — but the folds follow row order, which is biased when rows are
  sorted by label or time, and they differ from any shuffled splitter used elsewhere
  (steps 1-3 vs. step 4 here). A `KFold(shuffle=True)` without `random_state`
  reshuffles on every call. Pass one shared, seeded splitter *object* to every
  candidate so they all see the same rows.
- **Skipping the baseline.** A model that "achieves 92% accuracy" is meaningless if
  the majority class is 91% of the data — always compare against `DummyClassifier`/
  `DummyRegressor` first.
- **Scaling trees, or not scaling linear/distance models.** Unscaled features
  distort the penalty of regularized `LogisticRegression`, the RBF kernel of `SVC`,
  and kNN distances (and often slow solver convergence), while scaling is wasted
  effort (and harmless) for tree ensembles — know which family you're fitting.
- **Ignoring class imbalance until the model already looks "good."** On a heavily
  skewed split (say 95/5) a model fit without `class_weight` often predicts the
  majority class almost everywhere at the default 0.5 threshold. Reweighting at fit
  time and tuning the decision threshold (`TunedThresholdClassifierCV`, added in
  scikit-learn 1.5) are both
  legitimate; note that reweighting shifts predicted probabilities, so re-check
  calibration (see `model-evaluation`) if downstream code consumes them.
- **Picking a winner from a single train/test split.** A model that wins by 0.3
  points on one split can lose on another. As a rule of thumb, be skeptical of a
  margin smaller than the fold-to-fold standard deviation; because every candidate
  shares the same folds, comparing per-fold differences (paired) is more sensitive
  than comparing two means.

## References
- [scikit-learn: Supervised learning](https://scikit-learn.org/stable/supervised_learning.html) — full catalog of estimators by family.
- [scikit-learn: Choosing the right estimator](https://scikit-learn.org/stable/machine_learning_map.html) — flowchart for narrowing model families fast.
- [scikit-learn: HistGradientBoosting](https://scikit-learn.org/stable/modules/ensemble.html#histogram-based-gradient-boosting) — the strong tabular-data default used in step 3.
