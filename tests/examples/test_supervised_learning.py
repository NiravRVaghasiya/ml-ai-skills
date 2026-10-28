"""Executes the python blocks of supervised-learning/SKILL.md.

The skill starts from a feature-engineered train split, so the test injects a tiny
synthetic binary dataset as ``X_train``/``y_train`` via ``namespace=``. Nothing is
stubbed and no block is rewritten (all estimators are cheap at this size).
"""
from __future__ import annotations

import contextlib
import io
import unittest
import warnings

from examples import harness

SLUG = "supervised-learning"
# Block indices: 0 dummy baseline, 1 logistic, 2 hist-GBM, 3 shared-fold comparison,
# 4 class-weighted RF, 5 winner selection + final fit.


def _nonlinear(seed=0):
    from sklearn.datasets import make_classification

    return make_classification(n_samples=150, n_features=6, n_informative=4, random_state=seed)


def _linear(seed=0):
    # A purely linear decision rule with 20 features and few rows: logistic regression
    # clearly beats the tree ensemble here (observed ~0.98 vs ~0.87 ROC-AUC).
    import numpy as np

    rng = np.random.default_rng(seed)
    X = rng.normal(size=(150, 20))
    y = (X @ rng.normal(size=20) + rng.normal(scale=0.5, size=150) > 0).astype(int)
    return X, y


def _run(X, y, indices=None):
    with contextlib.redirect_stdout(io.StringIO()):
        return harness.run_blocks(SLUG, indices, namespace={"X_train": X, "y_train": y})


@harness.requires("numpy", "sklearn")
class TestSupervisedLearningExamples(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # Cap OpenMP/BLAS threads: tiny fits are dominated by thread start-up, and on a loaded
        # machine HistGradientBoosting took ~6s per fit with default threads vs ~0.2s with 1.
        # threadpoolctl ships as a scikit-learn dependency; it only limits runtimes that are
        # already loaded, so import the estimators' compiled modules first.
        import sklearn.ensemble  # noqa: F401
        import sklearn.linear_model  # noqa: F401
        from threadpoolctl import threadpool_limits

        cls._thread_limits = threadpool_limits(limits=1)

    @classmethod
    def tearDownClass(cls):
        cls._thread_limits.unregister()

    def test_workflow_runs_end_to_end(self):
        import numpy as np

        X, y = _nonlinear()
        ns = _run(X, y)
        self.assertEqual(set(ns["results"]), {"baseline", "logistic", "hist_gbm", "svm_rbf"})
        self.assertAlmostEqual(ns["results"]["baseline"], 0.5)  # constant scores -> AUC 0.5
        for name, scores in ns["fold_scores"].items():
            self.assertEqual(scores.shape, (5,), name)
            self.assertTrue(np.isfinite(scores).all(), name)
        self.assertEqual(ns["final_model"].predict(X).shape, (len(X),))

    def test_comparison_emits_no_deprecation_warnings(self):
        # Regression: SVC(probability=True) raises FutureWarning in scikit-learn 1.9
        # ("The `probability` parameter was deprecated in 1.9 and will be removed in 1.11").
        X, y = _nonlinear()
        with warnings.catch_warnings():
            warnings.simplefilter("error", FutureWarning)
            warnings.simplefilter("error", DeprecationWarning)
            ns = _run(X, y, [0, 1, 2, 3])
        # roc_auc on SVC without predict_proba falls back to decision_function and still ranks well.
        self.assertGreater(ns["results"]["svm_rbf"], 0.5)

    def test_final_model_is_the_cross_validated_winner(self):
        # Regression: step 6 was hard-coded to `trees.fit(...)`, shipping the hist-GBM even
        # when another candidate won the comparison in step 4.
        X, y = _linear()
        ns = _run(X, y)
        winner = max(ns["results"], key=ns["results"].get)
        self.assertEqual(winner, "logistic")
        self.assertEqual(ns["best_name"], winner)
        self.assertEqual(type(ns["final_model"]), type(ns["candidates"][winner]))

    def test_final_model_is_a_fresh_clone(self):
        # The candidate objects stay unfitted so they can be re-used in further CV runs.
        from sklearn.exceptions import NotFittedError

        X, y = _linear()
        ns = _run(X, y)
        self.assertIsNot(ns["final_model"], ns["candidates"][ns["best_name"]])
        with self.assertRaises(NotFittedError):
            ns["candidates"][ns["best_name"]].predict(X)

    def _compared(self):
        # Blocks 0-3 (baselines + shared-fold comparison) on the nonlinear set, computed once.
        if not hasattr(type(self), "_compared_ns"):
            X, y = _nonlinear()
            type(self)._compared_ns = _run(X, y, [0, 1, 2, 3])
        return dict(self._compared_ns)

    def test_ranking_refuses_nan_scores(self):
        # Regression: cross_val_score turns a failed fit/score into nan (error_score=np.nan),
        # and sorted() with nan keys is order-dependent. With this exact dict, the original
        # `sorted(results, key=results.get, reverse=True)` ranked "baseline" first, so step 6
        # would have fitted and shipped the DummyClassifier.
        import math

        ns = self._compared()
        results = dict(ns["results"])
        results["logistic"] = results["svm_rbf"] = math.nan
        self.assertEqual(sorted(results, key=results.get, reverse=True)[0], "baseline")
        ns["results"] = results
        with self.assertRaisesRegex(ValueError, r"non-finite CV score for \['logistic', 'svm_rbf'\]"):
            harness.run_blocks(SLUG, [5], namespace=ns)

    def test_ranking_refuses_to_ship_the_baseline(self):
        ns = self._compared()
        ns["results"] = {name: (0.5 if name == "baseline" else 0.45) for name in ns["results"]}
        with self.assertRaisesRegex(ValueError, "dummy baseline"):
            harness.run_blocks(SLUG, [5], namespace=ns)

    def test_multiclass_target_with_binary_roc_auc_is_caught(self):
        # End-to-end version of the nan case: "roc_auc" on a 3-class target fails at scoring
        # time in every fold. scikit-learn only warns (UserWarning) and returns nan scores, so
        # without the step-6 guard the ranking would be arbitrary.
        from sklearn.datasets import make_classification

        X, y = make_classification(n_samples=150, n_features=6, n_informative=4, n_classes=3,
                                   random_state=0)
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", UserWarning)
            with self.assertRaisesRegex(ValueError, "non-finite CV score"):
                _run(X, y, [0, 1, 2, 3, 5])

    def test_int_cv_is_deterministic_unshuffled_stratified(self):
        # Library-behavior check backing the corrected Gotcha (exercises sklearn, not skill
        # code): an int cv is NOT reshuffled between calls.
        from sklearn.model_selection import StratifiedKFold, check_cv

        X, y = _nonlinear()
        cv = check_cv(5, y, classifier=True)
        self.assertIsInstance(cv, StratifiedKFold)
        self.assertFalse(cv.shuffle)
        a = [test.tolist() for _, test in check_cv(5, y, classifier=True).split(X, y)]
        b = [test.tolist() for _, test in check_cv(5, y, classifier=True).split(X, y)]
        self.assertEqual(a, b)


if __name__ == "__main__":
    unittest.main()
