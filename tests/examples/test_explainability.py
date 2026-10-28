"""Executable-example tests for explainability/SKILL.md.

The skill assumes the user already has a fitted binary classifier ``model`` and
``X_train``/``X_test``/``y_train``/``y_test`` DataFrames (step 1). Those are
injected via ``namespace=`` as a small synthetic make_classification problem;
nothing else is stubbed. Regression tests also pre-inject ``np``/``pd``/``shap``
(``preimport=True``) so that, run against the pre-fix skill, they fail on the
defect they target rather than on its missing-import NameError. matplotlib is forced onto the non-interactive Agg
backend so the plot calls run headless (the skill saves figures to the temp cwd).

``replace=`` is used only for:
* ``n_repeats=30`` -> 3 (iteration count, speed);
* ``n_jobs=-1`` -> 1: on Windows, joblib/loky worker processes keep the harness
  temp directory as their cwd, so its cleanup fails with PermissionError
  (WinError 32). This is a test-environment artifact, not a skill defect;
* ``class_index = 1`` -> 2 in one multiclass test, simulating the edit the
  skill's comment tells multiclass users to make.
"""
from __future__ import annotations

import unittest

from examples import harness

SLUG = "explainability"
PERM_FAST = {1: {"n_repeats=30": "n_repeats=3", "n_jobs=-1": "n_jobs=1"}}
FEATURES = ["f0", "f1", "f2", "f3", "f4"]


def _data():
    import pandas as pd
    from sklearn.datasets import make_classification
    from sklearn.model_selection import train_test_split

    X, y = make_classification(n_samples=300, n_features=5, n_informative=3,
                               n_redundant=0, random_state=0)
    X = pd.DataFrame(X, columns=FEATURES)
    X_train, X_test, y_train, y_test = train_test_split(X, y, stratify=y, random_state=0)
    return dict(X_train=X_train, X_test=X_test, y_train=y_train, y_test=y_test)


def _forest(data):
    from sklearn.ensemble import RandomForestClassifier

    return RandomForestClassifier(n_estimators=30, random_state=0).fit(data["X_train"], data["y_train"])


def _gbm(data):
    from sklearn.ensemble import GradientBoostingClassifier

    return GradientBoostingClassifier(n_estimators=30, random_state=0).fit(data["X_train"], data["y_train"])


def _name_based_pipeline(data):
    # Selects columns by *name*: fails if handed a bare NumPy array.
    from sklearn.compose import make_column_transformer
    from sklearn.linear_model import LogisticRegression
    from sklearn.pipeline import make_pipeline
    from sklearn.preprocessing import StandardScaler

    pipe = make_pipeline(make_column_transformer((StandardScaler(), FEATURES)), LogisticRegression())
    return pipe.fit(data["X_train"], data["y_train"])


@harness.requires("numpy", "pandas", "sklearn", "shap", "matplotlib")
class ExplainabilityShapExamples(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import matplotlib

        matplotlib.use("Agg")

    def _run(self, indices, model_factory, *, preimport=False, data=None, replace=None):
        data = data if data is not None else _data()
        ns = dict(data, model=model_factory(data))
        if preimport:
            import numpy as np
            import pandas as pd
            import shap

            ns.update(np=np, pd=pd, shap=shap)
        rep = {k: v for k, v in PERM_FAST.items() if k in indices}
        rep.update(replace or {})
        return harness.run_blocks(SLUG, indices, namespace=ns, replace=rep)

    def test_tree_workflow_with_gradient_boosting(self):
        ns = self._run([0, 1, 2, 5], _gbm)
        self.assertEqual(ns["shap_values"].values.shape, (len(ns["X_test"]), 5))
        self.assertEqual(sorted(ns["agreement"].index), FEATURES)
        self.assertGreater(ns["rank_corr"], 0)  # methods broadly agree on this easy problem

    # --- regression tests: each failed on the pre-2026-10-27 code -------------

    def test_permutation_block_imports_pandas(self):
        # Original used `pd.Series` without importing pandas -> NameError.
        ns = self._run([0, 1], _gbm)
        self.assertEqual(sorted(ns["importances"].index), FEATURES)

    def test_tree_workflow_with_random_forest_multi_output(self):
        # RandomForestClassifier gives (n, features, 2) SHAP values; the original
        # passed them straight to shap.plots.bar -> IndexError, and step 5's
        # per-feature pd.Series would fail on the 2-D mean.
        import numpy as np

        ns = self._run([0, 1, 2, 5], _forest, preimport=True)
        sv = ns["shap_values"]
        self.assertEqual(sv.values.ndim, 2)
        # Additivity (local accuracy) for the positive-class probability.
        np.testing.assert_allclose(sv.values.sum(axis=1) + sv.base_values,
                                   ns["model"].predict_proba(ns["X_test"])[:, 1], atol=1e-6)
        self.assertEqual(sorted(ns["agreement"].index), FEATURES)

    def test_kernel_block_does_not_clobber_tree_shap_values(self):
        # Original reassigned `shap_values` to a NumPy array in the Kernel block,
        # so step 5's `shap_values.values` raised AttributeError.
        ns = self._run([0, 1, 2, 3, 5], _gbm, preimport=True)
        self.assertTrue(hasattr(ns["shap_values"], "values"))
        self.assertEqual(ns["kernel_shap_values"].shape, (50, 5))
        self.assertEqual(sorted(ns["agreement"].index), FEATURES)

    def test_kernel_explainer_with_name_based_pipeline(self):
        # Original passed `model.predict_proba` of a Pipeline: shap 0.54.1 raised
        # "property 'feature_names_in_' of 'Pipeline' object has no setter".
        import numpy as np

        ns = self._run([3], _name_based_pipeline, preimport=True)
        kv = ns["kernel_shap_values"]
        self.assertEqual(kv.shape, (50, 5))
        # Kernel SHAP is constrained to sum to f(x) - E[f(background)].
        expected = ns["predict_positive"](ns["X_test"].iloc[:50])
        np.testing.assert_allclose(kv.sum(axis=1) + ns["kernel_explainer"].expected_value,
                                   expected, atol=1e-6)


    def test_kernel_only_path_reaches_cross_check(self):
        # Non-tree models skip the TreeExplainer block. Step 5 used to read
        # `shap_values`, which only that block defines -> NameError.
        ns = self._run([0, 1, 3, 5], _name_based_pipeline, preimport=True)
        self.assertNotIn("shap_values", ns)
        self.assertEqual(sorted(ns["agreement"].index), FEATURES)
        self.assertGreater(ns["rank_corr"], 0)

    def test_tree_block_with_pipeline_uses_documented_tree_step(self):
        # TreeExplainer rejects a Pipeline (InvalidModelError); run the tree block
        # verbatim with the inputs its comment prescribes: pipe[-1] and the
        # pipe[:-1]-transformed features.
        import numpy as np
        import pandas as pd
        from sklearn.ensemble import RandomForestClassifier
        from sklearn.pipeline import make_pipeline
        from sklearn.preprocessing import StandardScaler

        data = _data()
        pipe = make_pipeline(StandardScaler(), RandomForestClassifier(n_estimators=30, random_state=0))
        pipe.fit(data["X_train"], data["y_train"])
        X_explain = pd.DataFrame(pipe[:-1].transform(data["X_test"]),
                                 columns=pipe[:-1].get_feature_names_out())
        ns = harness.run_blocks(SLUG, [2], namespace={"model": pipe[-1], "X_test": X_explain})
        sv = ns["shap_values"]
        np.testing.assert_allclose(sv.values.sum(axis=1) + sv.base_values,
                                   pipe.predict_proba(data["X_test"])[:, 1], atol=1e-6)

    def test_multiclass_class_index_is_explicit(self):
        # The ndim == 3 branch used to hard-code class 1; for multiclass the
        # explained class must be the one the user picks via class_index.
        import numpy as np
        import pandas as pd
        from sklearn.datasets import make_classification
        from sklearn.model_selection import train_test_split

        X, y = make_classification(n_samples=300, n_features=5, n_informative=3, n_redundant=0,
                                   n_classes=3, n_clusters_per_class=1, random_state=0)
        X = pd.DataFrame(X, columns=FEATURES)
        X_train, X_test, y_train, y_test = train_test_split(X, y, stratify=y, random_state=0)
        data = dict(X_train=X_train, X_test=X_test, y_train=y_train, y_test=y_test)
        ns = self._run([2], _forest, data=data, replace={2: {"class_index = 1": "class_index = 2"}})
        sv = ns["shap_values"]
        np.testing.assert_allclose(sv.values.sum(axis=1) + sv.base_values,
                                   ns["model"].predict_proba(X_test)[:, 2], atol=1e-6)


@harness.requires("numpy", "pandas", "sklearn", "lime")
class ExplainabilityLimeExamples(unittest.TestCase):
    def test_lime_with_name_based_pipeline_outside_notebook(self):
        # Original passed NumPy rows to a name-based pipeline ("Specifying the
        # columns using strings is only supported for dataframes") and called
        # show_in_notebook, which raises ImportError with IPython >= 9 (lime 0.2.0.1
        # imports `display` from IPython.core.display) — inside Jupyter too.
        data = _data()
        ns = harness.run_blocks(SLUG, [4], namespace=dict(data, model=_name_based_pipeline(data)))
        pairs = ns["explanation"].as_list()
        self.assertEqual(len(pairs), 5)  # num_features=8 is capped at the 5 features
        self.assertTrue(all(isinstance(w, float) for _, w in pairs))


if __name__ == "__main__":
    unittest.main()
