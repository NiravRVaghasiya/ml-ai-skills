"""Executes the python blocks of model-evaluation/SKILL.md.

The skill evaluates an already-fitted ``model`` on an existing split, so the test
injects (via ``namespace=``) a small synthetic, mildly imbalanced binary dataset and
a ``LogisticRegression`` fitted on its train part. Nothing is stubbed and no block
is rewritten; matplotlib is forced onto the non-interactive Agg backend so the
confusion-matrix plot in step 5 renders headlessly.
"""
from __future__ import annotations

import contextlib
import io
import unittest
import warnings

from examples import harness

SLUG = "model-evaluation"
# Block indices: 0 metrics, 1 splitters, 2 cross_validate, 3 train/test gap,
# 4 confusion matrix, 5 calibration, 6 learning curve.


def _split(n_samples=300, seed=0):
    from sklearn.datasets import make_classification
    from sklearn.model_selection import train_test_split

    X, y = make_classification(
        n_samples=n_samples, n_features=8, n_informative=4, weights=[0.8], random_state=seed
    )
    return train_test_split(X, y, test_size=0.3, stratify=y, random_state=seed)


def _run(indices, ns):
    with contextlib.redirect_stdout(io.StringIO()):
        return harness.run_blocks(SLUG, indices, namespace=ns)


@harness.requires("numpy", "sklearn", "matplotlib")
class TestModelEvaluationExamples(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import matplotlib

        matplotlib.use("Agg")
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

    def tearDown(self):
        import matplotlib.pyplot as plt

        plt.close("all")

    def _namespace(self):
        from sklearn.linear_model import LogisticRegression

        X_train, X_test, y_train, y_test = _split()
        model = LogisticRegression(max_iter=1000).fit(X_train, y_train)
        return {"model": model, "X_train": X_train, "X_test": X_test, "y_train": y_train, "y_test": y_test}

    def test_workflow_runs_end_to_end(self):
        import numpy as np

        ns = _run(None, self._namespace())
        for metric in ns["scoring"]:
            self.assertEqual(ns["cv_results"][f"test_{metric}"].shape, (5,))
            self.assertIn(f"train_{metric}", ns["cv_results"])
        self.assertTrue(np.isfinite(ns["val_scores"]).all())
        self.assertGreater(len(ns["prob_true"]), 0)
        self.assertEqual(set(ns["oof_brier"]), {"raw", "calibrated"})
        self.assertTrue(np.isfinite(list(ns["oof_brier"].values()) + [ns["test_brier"]]).all())
        self.assertIs(ns["proba_model"], ns["calibrated"] if ns["use_calibration"] else ns["model"])

    def test_learning_curve_on_label_sorted_rows_has_no_nan_points(self):
        # Regression: learning_curve takes prefixes of each training fold and defaulted to
        # shuffle=False. With rows sorted by label, the 10% and 32.5% prefixes were single-class,
        # LogisticRegression fits failed, and val_scores came back nan for those sizes
        # (observed: "10 fits failed out of a total of 25"; val means [nan, nan, ...]).
        import numpy as np
        from sklearn.exceptions import FitFailedWarning

        ns = self._namespace()
        order = np.argsort(ns["y_train"], kind="stable")
        ns["X_train"], ns["y_train"] = ns["X_train"][order], ns["y_train"][order]
        with warnings.catch_warnings():
            warnings.simplefilter("error", category=FitFailedWarning)
            out = _run([1, 6], ns)
        self.assertTrue(np.isfinite(out["train_scores"]).all(), out["train_scores"])
        self.assertTrue(np.isfinite(out["val_scores"]).all(), out["val_scores"])

    def test_calibration_uses_sigmoid_on_small_calibration_folds(self):
        # Regression: the original step hard-coded method="isotonic", which scikit-learn's own
        # CalibratedClassifierCV docstring says tends to overfit with << 1000 calibration samples.
        ns = _run([0, 1, 5], self._namespace())
        self.assertLess(len(ns["y_train"]) // 5, 1000)
        self.assertEqual(ns["calibrated"].method, "sigmoid")

    def test_calibration_picks_isotonic_when_folds_are_large(self):
        from sklearn.linear_model import LogisticRegression

        X_train, X_test, y_train, y_test = _split(n_samples=7500, seed=1)
        model = LogisticRegression(max_iter=1000).fit(X_train, y_train)
        ns = _run([0, 1, 5], {"model": model, "X_train": X_train, "X_test": X_test,
                           "y_train": y_train, "y_test": y_test})
        self.assertEqual(ns["calibrated"].method, "isotonic")

    def test_calibrator_is_fit_on_clones_not_the_passed_model(self):
        # Library-behavior check backing the step-6 comment (passes on the original skill too):
        # with cv=5 the calibrator refits clones per fold and leaves the caller's model untouched.
        ns = self._namespace()
        coef_before = ns["model"].coef_.copy()
        out = _run([0, 1, 5], ns)
        calibrated = out["calibrated"].fit(ns["X_train"], ns["y_train"])  # fitted only if chosen
        self.assertEqual(len(calibrated.calibrated_classifiers_), 5)
        for cc in calibrated.calibrated_classifiers_:
            self.assertIsNot(cc.estimator, ns["model"])
        self.assertTrue((ns["model"].coef_ == coef_before).all())

    def test_calibration_decision_never_looks_at_the_test_labels(self):
        # Regression: the step used to pick calibrated-vs-raw by comparing Brier on y_test and
        # then report on that same y_test (test set used for selection). The decision must now
        # come from out-of-fold train predictions, so inverting y_test cannot change it.
        ns = self._namespace()
        a = _run([0, 1, 5], dict(ns))
        b = _run([0, 1, 5], {**ns, "y_test": 1 - ns["y_test"]})
        self.assertEqual(a["oof_brier"], b["oof_brier"])
        self.assertEqual(a["use_calibration"], b["use_calibration"])
        self.assertNotEqual(a["test_brier"], b["test_brier"])  # y_test only feeds the final report

    def test_calibration_is_kept_when_it_helps_out_of_fold(self):
        # GaussianNB on redundant (correlated) features is over-confident; out-of-fold Brier
        # improves with calibration (observed 0.157 raw vs 0.128 calibrated), so it is kept,
        # and the kept calibrator is fitted.
        from sklearn.datasets import make_classification
        from sklearn.model_selection import train_test_split
        from sklearn.naive_bayes import GaussianNB

        X, y = make_classification(n_samples=300, n_features=10, n_informative=3, n_redundant=6,
                                   weights=[0.8], random_state=0)
        X_train, X_test, y_train, y_test = train_test_split(X, y, test_size=0.3, stratify=y,
                                                            random_state=0)
        ns = _run([0, 1, 5], {"model": GaussianNB().fit(X_train, y_train), "X_train": X_train,
                              "X_test": X_test, "y_train": y_train, "y_test": y_test})
        self.assertLess(ns["oof_brier"]["calibrated"], ns["oof_brier"]["raw"])
        self.assertTrue(ns["use_calibration"])
        self.assertIs(ns["proba_model"], ns["calibrated"])
        self.assertTrue(hasattr(ns["proba_model"], "calibrated_classifiers_"))

    def test_confusion_matrix_rows_are_normalized(self):
        import matplotlib.pyplot as plt
        import numpy as np

        _run([0, 4], self._namespace())
        disp_ax = plt.gcf().axes[0]
        self.assertTrue(disp_ax.images, "ConfusionMatrixDisplay drew no image")
        cm = disp_ax.images[0].get_array()
        np.testing.assert_allclose(np.asarray(cm).sum(axis=1), 1.0)


if __name__ == "__main__":
    unittest.main()
