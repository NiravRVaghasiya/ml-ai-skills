---
name: model-evaluation
display_name: Model Evaluation
description: >
  Use when the user wants to score, cross-validate, or calibrate a fitted model.
  Trigger phrases: "which metric should I use", "cross-validate this model",
  "is my model overfitting", "calibrate predicted probabilities", "build a
  confusion matrix", "compare two models rigorously". NOT for choosing which
  algorithm to fit (see supervised-learning) or searching hyperparameters (see
  hyperparameter-tuning).
type: workflow
domain: classical-ml
level: intermediate
lifecycle: stable
risk_level: medium
evidence_level: established-practice
last_verified: 2026-10-27
capabilities:
  - cross-validation-strategy-selection
  - classification-metric-selection
  - probability-calibration
  - imbalanced-classification-metrics
  - overfitting-diagnosis
  - confusion-matrix-analysis
requires:
conflicts:
related:
  - supervised-learning
  - hyperparameter-tuning
  - feature-engineering
  - data-preprocessing
inputs: A fitted model plus a held-out test set (or a train set and CV strategy) and a defined target/metric.
outputs: A metric report with fold-to-fold spread, confusion matrix, and calibration diagnostics establishing whether the model's reported performance and probabilities can be trusted.
version_constraints:
  - "scikit-learn>=1.4: cross_validate, learning_curve(shuffle=, random_state=), calibration_curve, CalibratedClassifierCV(method=, cv=), cross_val_predict(method=predict_proba), brier_score_loss, ConfusionMatrixDisplay.from_predictions as used in steps 1-7 (executed against scikit-learn 1.9.0 on 2026-10-27; lower bound not verified)"
  - "matplotlib>=3.6.1: ConfusionMatrixDisplay plotting in step 5 (executed against matplotlib 3.11.1 with the Agg backend on 2026-10-27; lower bound is the matplotlib minimum declared by scikit-learn 1.9.0)"
---

## Overview
Covers how to *score* a model honestly: picking a metric that matches the business
problem, choosing a cross-validation strategy that matches the data's structure,
reading confusion-matrix-level detail beyond a single accuracy number, checking
whether predicted probabilities are trustworthy, and diagnosing over/underfitting
with learning curves. This skill assumes the model itself is already chosen (see
`supervised-learning`) — it's about producing a number (or curve) you can actually
trust and act on.

## Workflow
1. **Pick the metric before looking at any score.** Accuracy is misleading on
   imbalanced classes; pick based on what an error actually costs.
   ```python
   from sklearn.metrics import classification_report, roc_auc_score, average_precision_score

   # Fraud detection: false negatives are expensive -> prioritize recall / PR-AUC
   # over plain accuracy on an imbalanced positive class.
   # Binary target assumed: [:, 1] is the positive class. If y_test holds only one
   # class, ROC-AUC is undefined: scikit-learn 1.9 returns nan with an
   # UndefinedMetricWarning (older releases raised ValueError) — check class counts.
   y_pred = model.predict(X_test)
   y_proba = model.predict_proba(X_test)[:, 1]
   print(classification_report(y_test, y_pred))
   print("ROC-AUC:", roc_auc_score(y_test, y_proba))
   print("PR-AUC:", average_precision_score(y_test, y_proba))
   ```
2. **Match the CV splitter to the data's structure.** Unstratified random k-fold
   can leave a minority class out of a fold entirely on imbalanced data (use
   stratification), and leaks the future on time-ordered data (use a
   forward-chaining split; `TimeSeriesSplit(gap=...)` also drops rows between train
   and validation when features use look-back windows). Grouped rows (several per
   patient/user) need `GroupKFold`/`StratifiedGroupKFold`.
   ```python
   from sklearn.model_selection import StratifiedKFold, TimeSeriesSplit

   cv_classification = StratifiedKFold(n_splits=5, shuffle=True, random_state=42)
   cv_timeseries = TimeSeriesSplit(n_splits=5)   # never shuffle time-ordered rows
   ```
3. **Use `cross_validate` for multiple metrics at once, and report spread.** A
   single mean hides how stable the model is fold-to-fold.
   ```python
   from sklearn.model_selection import cross_validate

   scoring = ["roc_auc", "average_precision", "f1"]
   cv_results = cross_validate(model, X_train, y_train, cv=cv_classification,
                                scoring=scoring, return_train_score=True)
   for metric in scoring:
       test_scores = cv_results[f"test_{metric}"]
       print(f"{metric}: {test_scores.mean():.3f} +/- {test_scores.std():.3f}")
   ```
4. **Diagnose over/underfitting with train vs. validation score.** A large gap
   between `train_<metric>` and `test_<metric>` from step 3 means overfitting;
   both being low means underfitting.
   ```python
   for metric in scoring:
       train_mean = cv_results[f"train_{metric}"].mean()
       test_mean = cv_results[f"test_{metric}"].mean()
       print(f"{metric}: train={train_mean:.3f} test={test_mean:.3f} gap={train_mean - test_mean:.3f}")
   ```
5. **Inspect the confusion matrix, not just one summary number.** Precision/recall
   trade-offs are invisible in a single F1 or accuracy figure.
   ```python
   from sklearn.metrics import ConfusionMatrixDisplay   # plotting needs matplotlib

   # normalize="true": each row sums to 1, so the diagonal is per-class recall.
   ConfusionMatrixDisplay.from_predictions(y_test, y_pred, normalize="true")
   ```
6. **Check calibration before trusting predicted probabilities.** A model with
   great ROC-AUC can still have systematically wrong probability estimates —
   critical if downstream logic thresholds or sums probabilities.
   ```python
   from sklearn.calibration import calibration_curve, CalibratedClassifierCV
   from sklearn.metrics import brier_score_loss
   from sklearn.model_selection import cross_val_predict

   prob_true, prob_pred = calibration_curve(y_test, y_proba, n_bins=10)
   # plot prob_pred vs prob_true; points on the diagonal mean well-calibrated.
   # Empty bins are dropped, so len(prob_true) can be < n_bins on small test sets.

   # If miscalibrated: with cv=5, CalibratedClassifierCV clones `model`, refits it on
   # 4/5 of X_train and fits the calibrator on the held-out 1/5, per fold — the
   # calibrator never sees predictions on rows the base model was trained on.
   # Isotonic overfits small calibration sets (scikit-learn advises against it well
   # below ~1,000 calibration samples), so fall back to sigmoid there.
   method = "isotonic" if len(y_train) // 5 >= 1000 else "sigmoid"
   calibrated = CalibratedClassifierCV(model, method=method, cv=5)

   # Decide calibrate-or-not on out-of-fold TRAIN predictions (cv_classification from
   # step 2), not on y_test: choosing on the test set and then reporting it is optimistic.
   oof = {
       "raw": cross_val_predict(model, X_train, y_train, cv=cv_classification,
                                method="predict_proba")[:, 1],
       "calibrated": cross_val_predict(calibrated, X_train, y_train, cv=cv_classification,
                                       method="predict_proba")[:, 1],
   }
   oof_brier = {name: brier_score_loss(y_train, p) for name, p in oof.items()}
   use_calibration = oof_brier["calibrated"] < oof_brier["raw"]
   proba_model = calibrated.fit(X_train, y_train) if use_calibration else model
   print(f"OOF Brier (lower is better): {oof_brier} -> calibrate={use_calibration}")
   # Report the chosen model once on the untouched test set — no further choices after this.
   test_brier = brier_score_loss(y_test, proba_model.predict_proba(X_test)[:, 1])
   print(f"test Brier: {test_brier:.4f}")
   ```
7. **Plot a learning curve for a clearer over/underfitting picture across
   training-set sizes.** `learning_curve` takes each size as a *prefix* of the
   training fold, and does not shuffle by default — on rows sorted by label (or by
   time) the small prefixes can be single-class, every such fit fails, and those
   points come back as `nan`. Pass `shuffle=True` for i.i.d. data.
   ```python
   from sklearn.model_selection import learning_curve
   import numpy as np

   train_sizes, train_scores, val_scores = learning_curve(
       model, X_train, y_train, cv=cv_classification, scoring="roc_auc",
       train_sizes=np.linspace(0.1, 1.0, 5), shuffle=True, random_state=42,
   )
   print("train:", train_scores.mean(axis=1))
   print("val:  ", val_scores.mean(axis=1))
   ```

## Gotchas
- **Accuracy on imbalanced classes is nearly meaningless.** A 95%-negative dataset
  gets 95% accuracy by predicting the majority class every time — use PR-AUC,
  recall/precision at a chosen threshold, or F1 instead.
- **Random k-fold on time-ordered data leaks the future into training.** Any row
  from "next month" ending up in a training fold whose validation fold is "last
  month" inflates the score unrealistically — use `TimeSeriesSplit`.
- **Tuning and final evaluation on the same fold(s).** If the same CV split was
  used to pick a model/threshold, the resulting score is optimistic — hold out a
  final untouched test set, or nest CV (see `hyperparameter-tuning`).
- **A high ROC-AUC does not imply well-calibrated probabilities.** ROC-AUC only
  cares about rank ordering; if the application needs actual probability values
  (e.g., expected-value calculations), check `calibration_curve` explicitly.
- **Calibrating on the same data the model was trained on overfits the
  calibration too.** `CalibratedClassifierCV` needs its own CV or held-out split —
  don't calibrate against training predictions. To calibrate an *already-fitted*
  model without refitting it, wrap it in `sklearn.frozen.FrozenEstimator` (added in
  scikit-learn 1.6, which deprecated `cv="prefit"`; 1.8+ removed it, and 1.9 rejects it with
  `InvalidParameterError` — on releases before 1.6, `cv="prefit"` is the equivalent) and fit
  the calibrator on a separate calibration split, never on the test set you report.

## References
- [scikit-learn: Model evaluation](https://scikit-learn.org/stable/modules/model_evaluation.html) — full metric catalog and scoring-string reference.
- [scikit-learn: Cross-validation](https://scikit-learn.org/stable/modules/cross_validation.html) — splitter strategies including `TimeSeriesSplit` and `StratifiedKFold`.
- [scikit-learn: Probability calibration](https://scikit-learn.org/stable/modules/calibration.html) — calibration curves and `CalibratedClassifierCV` usage.
