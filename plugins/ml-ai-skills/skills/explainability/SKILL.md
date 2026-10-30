---
name: explainability
display_name: Model Explainability
description: >
  Use when the user wants to explain what a trained model is doing — globally
  (which features matter overall) or locally (why it made a specific
  prediction). Trigger phrases: "explain this prediction", "why did the model
  predict this", "compute SHAP values", "use LIME to explain my model",
  "feature importance for this model". NOT for measuring bias/fairness across
  groups (see ai-ethics-fairness) or removing/anonymizing PII (see
  data-privacy).
type: workflow
domain: responsible-ai
level: intermediate
lifecycle: stable
risk_level: low
evidence_level: official-documentation
last_verified: 2026-10-27
capabilities:
  - shap-value-computation
  - lime-local-explanation
  - permutation-importance
  - global-feature-importance
  - explanation-cross-checking
requires:
conflicts:
related:
  - ai-ethics-fairness
  - data-privacy
  - model-evaluation
inputs: A fitted, sklearn-compatible model plus held-out feature data (and optionally training data as a background sample) to explain.
outputs: Global feature-importance rankings and per-instance attributions (SHAP/LIME) with a cross-method agreement check.
version_constraints:
  - "scikit-learn>=0.22: sklearn.inspection.permutation_importance(scoring='roc_auc', n_repeats=, n_jobs=) (0.22 lower bound documented: the scikit-learn 0.22 release notes add inspection.permutation_importance, read 2026-10-29; 0.22 itself not executed; executed against scikit-learn 1.9.0 on 2026-10-27)"
  - "shap>=0.45: TreeExplainer(model)(X) Explanation with (n, features, classes) values for sklearn forests, Explanation slicing [:, :, 1], shap.plots.bar/waterfall(show=False), KernelExplainer.shap_values(nsamples=), shap.sample(X, n, random_state=) (lower bound is a model-knowledge estimate of when multi-output values became one ndarray instead of a list; executed against shap 0.54.1 on 2026-10-27)"
  - "lime>=0.2: lime.lime_tabular.LimeTabularExplainer(random_state=), explain_instance(...).as_list() (executed against lime 0.2.0.1 on 2026-10-27)"
---

## Overview
Turns an opaque trained model into explanations a human can act on: global
feature-importance rankings and per-instance attributions. Covers the three
workhorse techniques — permutation importance, SHAP, and LIME — and how to
sanity-check that the explanations agree with each other before trusting
them. Output is a reusable explanation pipeline, not a one-off plot.

## Workflow
1. **Pick the explanation type before picking a tool.** Decide model-specific
   vs. model-agnostic, and global vs. local — this determines which method
   below to reach for.
   ```python
   # Model-specific + fast (tree models only)   -> SHAP TreeExplainer
   # Model-agnostic + global                     -> permutation importance
   # Model-agnostic + local (single prediction)  -> SHAP KernelExplainer or LIME
   # Already have a fitted sklearn-compatible estimator `model` and
   # held-out data (X_test, y_test) for the rest of this workflow.
   ```
2. **Compute global importance with permutation importance.** Model-agnostic,
   uses only predict/score, and directly measures the metric drop the model
   actually cares about.
   ```python
   import numpy as np
   import pandas as pd
   from sklearn.inspection import permutation_importance

   # scoring="roc_auc" assumes a binary classifier; use e.g. "roc_auc_ovr" or
   # "neg_log_loss" for multiclass and an error metric for regression.
   result = permutation_importance(
       model, X_test, y_test,
       n_repeats=30, random_state=42, scoring="roc_auc", n_jobs=-1
   )
   importances = (
       pd.Series(result.importances_mean, index=X_test.columns)
       .sort_values(ascending=False)
   )
   print(importances.head(10))
   ```
3. **Compute SHAP values for consistent local + global attribution.** For
   tree ensembles, `TreeExplainer` is exact and fast; for anything else, fall
   back to `KernelExplainer` on a background sample.
   ```python
   import matplotlib.pyplot as plt
   import shap

   # Tree models (XGBoost, LightGBM, RandomForest, ...): exact and fast.
   # TreeExplainer rejects a Pipeline. Pass the tree step and its transformed input:
   #   explainer = shap.TreeExplainer(pipe[-1])
   #   X_explain = pd.DataFrame(pipe[:-1].transform(X_test),
   #                            columns=pipe[:-1].get_feature_names_out())
   # (the attributions are then for the transformed features), or use KernelExplainer below.
   explainer = shap.TreeExplainer(model)
   shap_values = explainer(X_test)
   # Some classifiers (e.g. sklearn RandomForestClassifier) return one attribution
   # set per class: values shape (n_samples, n_features, n_classes). The plots
   # and step 5 need 2-D values, so pick the class being explained.
   class_index = 1   # binary: the positive class; for multiclass, set the class you explain
   if shap_values.values.ndim == 3:
       shap_values = shap_values[:, :, class_index]
   shap_matrix = shap_values.values   # (n_samples, n_features), used in step 5

   # Global view: mean |SHAP value| per feature. Save and close each figure:
   # with show=False the next plot would otherwise draw onto the same one.
   shap.plots.bar(shap_values, show=False)
   plt.savefig("shap_global_bar.png", bbox_inches="tight")
   plt.close()

   # Local view: explain one prediction
   shap.plots.waterfall(shap_values[0], show=False)
   plt.savefig("shap_local_waterfall_0.png", bbox_inches="tight")
   plt.close()
   ```
   ```python
   import pandas as pd
   import shap

   # Non-tree / black-box models: model-agnostic but much slower.
   # Pass a plain function: shap hands the model NumPy arrays (which breaks
   # pipelines that select columns by name), and in shap 0.54.1 a Pipeline's
   # bound predict_proba fails with "property 'feature_names_in_' of 'Pipeline'
   # object has no setter". Explaining P(positive) gives 2-D (n, features) output.
   def predict_positive(data):
       return model.predict_proba(pd.DataFrame(data, columns=X_train.columns))[:, 1]

   background = shap.sample(X_train, 100, random_state=42)
   kernel_explainer = shap.KernelExplainer(predict_positive, background)
   kernel_shap_values = kernel_explainer.shap_values(X_test.iloc[:50], nsamples=200)
   # Distinct names keep the TreeExplainer `shap_values` Explanation intact; step 5
   # reads `shap_matrix`, which is set by whichever SHAP block ran last.
   shap_matrix = kernel_shap_values   # (50, n_features)
   ```
4. **Explain a single prediction with LIME when you need a lightweight,
   surrogate-model explanation instead of SHAP's game-theoretic one.**
   ```python
   import pandas as pd
   from lime.lime_tabular import LimeTabularExplainer

   # LIME perturbs NumPy rows; rebuild the DataFrame so models/pipelines that
   # rely on column names still work.
   def predict_proba_df(data):
       return model.predict_proba(pd.DataFrame(data, columns=X_train.columns))

   lime_explainer = LimeTabularExplainer(
       X_train.values,
       feature_names=X_train.columns.tolist(),
       class_names=["negative", "positive"],
       mode="classification",
       discretize_continuous=True,
       random_state=42,
   )
   instance = X_test.iloc[0].values
   explanation = lime_explainer.explain_instance(
       instance, predict_proba_df, num_features=8
   )
   print(explanation.as_list())   # (condition, weight) pairs for the "positive" class
   # In Jupyter: from IPython.display import HTML, display; display(HTML(explanation.as_html()))
   # (lime 0.2.0.1's show_in_notebook imports `display` from IPython.core.display,
   # which IPython 9 no longer provides, so it raises ImportError even inside Jupyter.)
   ```
5. **Cross-check explanations before trusting them.** Different methods
   should broadly agree on the top drivers; large disagreement is a signal to
   dig deeper (correlated features, unstable local surrogate, or a
   mis-specified background dataset), not to average blindly.
   ```python
   # shap_matrix: 2-D attributions from step 3 (Tree or Kernel path), raw-feature columns.
   shap_rank = (
       pd.Series(np.abs(shap_matrix).mean(axis=0), index=X_test.columns)
       .sort_values(ascending=False)
   )
   agreement = pd.concat(
       [importances.rename("permutation"), shap_rank.rename("shap")], axis=1
   ).dropna()
   # The columns are in different units (metric drop vs. model-output units),
   # so compare ranks, not magnitudes.
   rank_corr = agreement["permutation"].corr(agreement["shap"], method="spearman")
   print(agreement.head(10))
   print(f"Spearman rank agreement: {rank_corr:.2f}")
   ```

## Gotchas
- **`KernelExplainer` is slow and approximate.** It's model-agnostic but
  samples feature coalitions, so results are noisy and expensive at scale —
  always prefer `TreeExplainer` (tree models) or `DeepExplainer`/`GradientExplainer`
  (neural nets) when applicable.
- **Correlated features distort SHAP attributions.** The attributions still sum
  to the prediction, but `KernelExplainer` (and interventional Tree SHAP)
  "turn off" a feature by sampling it independently of the others, which
  evaluates the model on unrealistic feature combinations; with strongly
  correlated features (e.g. `height_cm` and `height_in`), credit can be split
  between them somewhat arbitrarily rather than attributed to the underlying
  signal.
- **LIME explanations are unstable.** Because it fits a local linear surrogate
  on randomly perturbed samples, re-running `explain_instance` on the same row
  can produce visibly different top features. `random_state` makes a *fresh*
  explainer reproducible, but repeated calls on one explainer still draw new
  samples — consider averaging over multiple runs before reporting a result.
- **Multi-output SHAP values.** For classifiers, SHAP may return one attribution
  set per class (an extra trailing axis); feeding that 3-D array to
  `shap.plots.bar`/`waterfall` or a per-feature `pd.Series` fails — select the
  class you are explaining first.
- **Permutation importance is biased downward for correlated features.**
  Shuffling one of two correlated features barely hurts the model (the other
  still carries the signal), understating both features' true importance.
- **High feature importance is not causation.** All three methods describe
  what the model relies on statistically, not what actually drives the
  real-world outcome — don't present SHAP/LIME/permutation results as causal
  claims without a causal design to back them.

## References
- [SHAP documentation](https://shap.readthedocs.io/en/latest/) — API reference and explainer selection guide (Tree/Kernel/Deep/Linear).
- [Lundberg & Lee (2017), "A Unified Approach to Interpreting Model Predictions"](https://arxiv.org/abs/1705.07874) — the SHAP paper; defines the Shapley-value formulation.
- [Ribeiro, Singh & Guestrin (2016), "Why Should I Trust You?" (LIME)](https://arxiv.org/abs/1602.04938) — the original LIME paper.
- [scikit-learn: Permutation feature importance](https://scikit-learn.org/stable/modules/permutation_importance.html) — covers the correlated-feature bias gotcha in detail.
