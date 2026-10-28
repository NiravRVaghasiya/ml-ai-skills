---
name: hyperparameter-tuning
display_name: Hyperparameter Tuning
description: >
  Use when the user wants to search for the best hyperparameters of an already-
  chosen model. Trigger phrases: "tune this model", "grid search over these
  params", "random search hyperparameters", "use Optuna to tune", "find the best
  n_estimators/learning_rate", "avoid overfitting my hyperparameter search". NOT
  for choosing which metric to optimize (see model-evaluation) or which
  algorithm/model family to use (see supervised-learning).
type: workflow
domain: classical-ml
level: advanced
lifecycle: stable
risk_level: medium
evidence_level: established-practice
last_verified: 2026-10-27
capabilities:
  - grid-search-tuning
  - randomized-search-tuning
  - bayesian-optimization-optuna
  - nested-cross-validation
  - search-overfitting-prevention
requires:
conflicts:
related:
  - model-evaluation
  - supervised-learning
  - feature-engineering
inputs: A chosen model family (see supervised-learning), a decided optimization metric (see model-evaluation), and a train set.
outputs: A best hyperparameter configuration, an unbiased nested-CV performance estimate, and a model refit on the full training set.
version_constraints:
  - "scikit-learn>=1.4: GridSearchCV, RandomizedSearchCV, StratifiedKFold, cross_val_score as used in steps 1-5 (executed against scikit-learn 1.9.0 on 2026-10-27; lower bound not verified)"
  - "scipy>=1.10: scipy.stats randint and uniform(loc, scale) distributions in step 2 (executed against scipy 1.18.1 on 2026-10-27; lower bound is the scipy minimum declared by scikit-learn 1.9.0)"
  - "optuna>=3.0: create_study, TPESampler(seed=), MedianPruner(n_startup_trials=, n_warmup_steps=), trial.report, trial.should_prune, TrialPruned (executed against optuna 5.0.0 on 2026-10-27; lower bound not verified)"
---

## Overview
Covers searching a model's hyperparameter space efficiently and getting an
*unbiased* estimate of how the tuned model will perform — the two things naive
tuning gets wrong. Walks through exhaustive grid search, randomized search over
distributions, Bayesian optimization with Optuna, and nested cross-validation to
avoid reporting an optimistic score. Assumes the metric to optimize is already
decided (see `model-evaluation`) and the model family is already chosen (see
`supervised-learning`).

## Workflow
1. **Start with `GridSearchCV` only for small, discrete spaces.** Exhaustive and
   simple, but combinations grow multiplicatively — as a rule of thumb, keep it to
   2-3 params with a handful of values each.
   ```python
   from sklearn.model_selection import GridSearchCV
   from sklearn.ensemble import RandomForestClassifier

   # Binary target assumed: on multiclass y, "roc_auc" fails inside every fit, each
   # score becomes nan (error_score default; only a UserWarning) and the search still
   # returns an arbitrary best_params_ with best_score_=nan — use "roc_auc_ovr" there.
   param_grid = {
       "n_estimators": [100, 300, 500],
       "max_depth": [None, 5, 10],
       "min_samples_leaf": [1, 2, 5],
   }
   grid = GridSearchCV(
       RandomForestClassifier(random_state=42), param_grid,
       cv=5, scoring="roc_auc", n_jobs=-1,
   )
   grid.fit(X_train, y_train)
   print(grid.best_params_, grid.best_score_)
   ```
2. **Switch to `RandomizedSearchCV` for larger or continuous spaces.** Sampling a
   fixed budget of random combinations from distributions scales far better than
   an exhaustive grid as dimensionality grows.
   ```python
   from sklearn.model_selection import RandomizedSearchCV
   from sklearn.ensemble import RandomForestClassifier
   from scipy.stats import randint, uniform

   param_distributions = {
       "n_estimators": randint(100, 800),
       "max_depth": randint(2, 30),
       "min_samples_leaf": randint(1, 10),
       "max_features": uniform(0.3, 0.7),   # uniform(loc, scale) -> [0.3, 1.0]
   }
   random_search = RandomizedSearchCV(
       RandomForestClassifier(random_state=42), param_distributions,
       n_iter=50, cv=5, scoring="roc_auc", random_state=42, n_jobs=-1,
   )
   random_search.fit(X_train, y_train)
   print(random_search.best_params_, random_search.best_score_)
   ```
3. **Use Optuna for Bayesian/sequential search when each fit is expensive.** Optuna
   picks the next trial's params based on past trial results, and can prune bad
   trials early — but only if the objective *reports* intermediate values with
   `trial.report()` and checks `trial.should_prune()`. A pruner attached to an
   objective that only returns a final score never prunes anything. Here each CV
   fold is one step: the running mean is reported after every fold, and because
   every trial uses the same splitter, step *k* means the same fold across trials.
   ```python
   import numpy as np
   import optuna
   from sklearn.base import clone
   from sklearn.ensemble import GradientBoostingClassifier
   from sklearn.metrics import roc_auc_score
   from sklearn.model_selection import StratifiedKFold

   tune_cv = StratifiedKFold(n_splits=5, shuffle=True, random_state=42)
   X_arr, y_arr = np.asarray(X_train), np.asarray(y_train)   # numeric features assumed

   def objective(trial):
       params = {
           "n_estimators": trial.suggest_int("n_estimators", 100, 600),
           "learning_rate": trial.suggest_float("learning_rate", 1e-3, 0.3, log=True),
           "max_depth": trial.suggest_int("max_depth", 2, 8),
           "subsample": trial.suggest_float("subsample", 0.5, 1.0),
       }
       model = GradientBoostingClassifier(random_state=42, **params)
       fold_scores = []
       for step, (tr, va) in enumerate(tune_cv.split(X_arr, y_arr)):
           fitted = clone(model).fit(X_arr[tr], y_arr[tr])
           fold_scores.append(roc_auc_score(y_arr[va], fitted.predict_proba(X_arr[va])[:, 1]))
           trial.report(float(np.mean(fold_scores)), step)
           if trial.should_prune():          # worse than the median of earlier trials at this fold
               raise optuna.TrialPruned()
       return float(np.mean(fold_scores))

   study = optuna.create_study(
       direction="maximize",
       sampler=optuna.samplers.TPESampler(seed=42),
       # n_warmup_steps must be < n_splits, or no step is ever eligible for pruning.
       pruner=optuna.pruners.MedianPruner(n_startup_trials=5, n_warmup_steps=1),
   )
   study.optimize(objective, n_trials=100, timeout=1800)
   print(study.best_params, study.best_value)
   ```
4. **Get an unbiased performance estimate with nested cross-validation.** Reporting
   `best_score_` from step 1/2 as "final performance" is optimistic — it was
   selected on those exact folds. Nest an outer CV loop around the whole search —
   ideally the same search configuration you will ship. The result estimates the
   *tuning procedure*, not one specific `best_params_` set. Use stratified folds for
   classification: plain `KFold` on an imbalanced target can produce a fold with no
   positives, where ROC-AUC is undefined (`nan`) and the mean becomes `nan`.
   ```python
   import numpy as np
   from sklearn.model_selection import cross_val_score, StratifiedKFold

   # Reuses RandomizedSearchCV, RandomForestClassifier and param_distributions from step 2.
   inner_cv = StratifiedKFold(n_splits=5, shuffle=True, random_state=1)
   outer_cv = StratifiedKFold(n_splits=5, shuffle=True, random_state=2)

   search = RandomizedSearchCV(
       RandomForestClassifier(random_state=42), param_distributions,
       n_iter=30, cv=inner_cv, scoring="roc_auc", random_state=42,
   )
   nested_scores = cross_val_score(search, X_train, y_train, cv=outer_cv, scoring="roc_auc")
   if np.isnan(nested_scores).any():
       raise ValueError("a fold scored nan — check per-fold class counts before trusting the estimate")
   print("unbiased estimate:", nested_scores.mean(), "+/-", nested_scores.std())
   ```
5. **Refit the winning configuration on the full training set for shipping.**
   `GridSearchCV`/`RandomizedSearchCV` already refit `best_estimator_` on all of
   train by default (`refit=True`) — reuse it directly rather than re-instantiating.
   ```python
   final_model = random_search.best_estimator_   # already refit on full X_train
   ```

## Gotchas
- **Reporting the search's own CV score as the final number.** `best_score_` is
  the best of many trials evaluated on the same folds — it's biased upward. Use
  nested CV (step 4) or a held-out test set that was never touched during search.
- **Grid search combinatorial explosion.** Three params with 5 values each times a
  5-fold CV is 625 fits — adding a fourth 5-value param jumps to 3,125. Once the
  grid gets past a few dimensions, randomized/Bayesian search usually covers the
  space for a fraction of the fits.
- **Too many trials on a small dataset overfits the *search* to the validation
  folds**, even though each individual model looks fine — this is "CV leakage via
  the search process." Keep `n_iter`/`n_trials` proportional to dataset size and
  space complexity, and prefer nested CV to catch it.
- **Forgetting `random_state` on the search and the estimator.** Without it,
  `RandomizedSearchCV`/Optuna results aren't reproducible between runs, making it
  impossible to tell whether a "better" config was signal or noise. A seeded
  `TPESampler` is only reproducible for sequential runs — `study.optimize(n_jobs>1)`
  changes the trial sequence, and a `timeout` can stop at a different trial count
  on a slower or busier machine.
- **Attaching a pruner without reporting.** `MedianPruner` only acts on values
  passed to `trial.report(value, step)`; an objective that just returns
  `cross_val_score(...).mean()` runs every trial to completion. Likewise,
  `n_warmup_steps` at or above the number of reported steps disables pruning.
- **Ignoring compute budget.** Running full grid search with expensive estimators
  (deep trees, large ensembles) without `n_jobs=-1`, early stopping, or Optuna
  pruning wastes hours reproducing results a pruned/randomized search would reach
  in minutes.

## References
- [scikit-learn: Tuning hyperparameters](https://scikit-learn.org/stable/modules/grid_search.html) — `GridSearchCV`/`RandomizedSearchCV` API and nested CV example.
- [Optuna documentation](https://optuna.readthedocs.io/en/stable/) — samplers, pruners, and the `study`/`trial` API used in step 3.
- [scikit-learn: Nested versus non-nested CV example](https://scikit-learn.org/stable/auto_examples/model_selection/plot_nested_cross_validation_iris.html) — worked demonstration of the bias nested CV corrects for.
