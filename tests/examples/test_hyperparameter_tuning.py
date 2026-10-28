"""Executes the python blocks of hyperparameter-tuning/SKILL.md.

Nothing is stubbed: the skill needs only scikit-learn, scipy and optuna, and the
training data is a tiny synthetic ``make_classification`` set injected as
``X_train``/``y_train`` (the skill assumes the caller already has a train split).

``replace=`` is used only to shrink iteration budgets / ensemble sizes, and to
swap ``n_jobs=-1`` for ``n_jobs=1``: on Windows, loky worker processes inherit the
harness's temporary cwd and keep it locked, so ``TemporaryDirectory`` cleanup fails
(observed: PermissionError WinError 32). Parallelism is not what is under test.
"""
from __future__ import annotations

import contextlib
import io
import unittest
import warnings

from examples import harness

SLUG = "hyperparameter-tuning"

# Block indices (document order): 0 grid, 1 randomized, 2 optuna, 3 nested CV, 4 refit.
FAST = {
    0: {
        '"n_estimators": [100, 300, 500]': '"n_estimators": [5]',
        '"max_depth": [None, 5, 10]': '"max_depth": [None, 3]',
        '"min_samples_leaf": [1, 2, 5]': '"min_samples_leaf": [1, 5]',
        "n_jobs=-1": "n_jobs=1",
    },
    1: {
        '"n_estimators": randint(100, 800)': '"n_estimators": randint(5, 15)',
        "n_iter=50": "n_iter=3",
        "n_jobs=-1": "n_jobs=1",
    },
    2: {
        '"n_estimators", 100, 600': '"n_estimators", 5, 20',
        "n_trials=100": "n_trials=3",
    },
    3: {"n_iter=30": "n_iter=1"},
}


def _data(n_samples=120, weights=None, seed=0):
    from sklearn.datasets import make_classification

    return make_classification(
        n_samples=n_samples, n_features=6, n_informative=4, weights=weights,
        flip_y=0 if weights else 0.01, random_state=seed,
    )


def _run_blocks(*args, **kwargs):
    # The blocks print best params/scores; keep the test log readable.
    with contextlib.redirect_stdout(io.StringIO()):
        return harness.run_blocks(*args, **kwargs)


@harness.requires("numpy", "sklearn", "scipy")
class TestHyperparameterTuningExamples(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # Only the Optuna-dependent tests need optuna; quiet its per-trial INFO logging if present.
        # (Optuna configures its own logger on import, so a plain setLevel before import is lost.)
        if not harness.missing_modules("optuna"):
            import optuna

            optuna.logging.set_verbosity(optuna.logging.WARNING)
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

    @harness.requires("optuna")
    def test_workflow_runs_end_to_end(self):
        import numpy as np

        X, y = _data()
        ns = _run_blocks(SLUG, namespace={"X_train": X, "y_train": y}, replace=FAST)
        self.assertIn("n_estimators", ns["grid"].best_params_)
        self.assertEqual(len(ns["study"].trials), 3)
        self.assertEqual(ns["nested_scores"].shape, (5,))
        self.assertTrue(np.isfinite(ns["nested_scores"]).all())
        # Step 5: the shipped model is the search's refit estimator, fitted on all of X_train.
        self.assertIs(ns["final_model"], ns["random_search"].best_estimator_)
        self.assertEqual(ns["final_model"].predict(X).shape, (len(X),))

    def test_randomized_search_block_is_self_contained(self):
        # Regression: step 2 used RandomForestClassifier imported only in step 1,
        # so running it on its own (as "switch to RandomizedSearchCV" invites) raised NameError.
        X, y = _data()
        ns = _run_blocks(SLUG, [1], namespace={"X_train": X, "y_train": y}, replace={1: FAST[1]})
        self.assertTrue(hasattr(ns["random_search"], "best_estimator_"))

    @harness.requires("optuna")
    def test_optuna_objective_feeds_the_pruner(self):
        # Regression: the original objective returned cross_val_score(...).mean() without
        # trial.report(), so MedianPruner had no intermediate values and could never prune
        # (observed: every trial's intermediate_values == {}). n_warmup_steps=5 with only
        # 5 folds (steps 0-4) would also have disabled pruning on its own.
        import optuna

        X, y = _data()
        # 7 trials: past MedianPruner's 5 startup trials, so later trials are prune-eligible.
        seven = {**FAST[2], "n_trials=100": "n_trials=7"}
        ns = _run_blocks(SLUG, [2], namespace={"X_train": X, "y_train": y}, replace={2: seven})
        study = ns["study"]
        n_folds = ns["tune_cv"].get_n_splits()
        for t in study.trials:
            self.assertTrue(t.intermediate_values, f"trial {t.number} reported nothing to the pruner")
            if t.state == optuna.trial.TrialState.COMPLETE:
                self.assertEqual(sorted(t.intermediate_values), list(range(n_folds)))
        # Whether warm-up leaves a prunable step is checked behaviorally in
        # test_pruner_prunes_a_trial_below_the_median (no private Optuna attributes).

    @harness.requires("optuna")
    def test_pruner_prunes_a_trial_below_the_median(self):
        # Behavioral check of the step-3 objective + pruner: seed a study with 5 completed
        # trials whose running fold mean is a perfect 1.0 at every step, then run the skill's
        # objective once. Any real trial is below that median after the 1-step warm-up, so it
        # must be pruned at step 1 instead of running all 5 folds.
        import optuna

        X, y = _data()
        # Only the objective and pruner are needed from the block, so run a single trial.
        one_trial = {**FAST[2], "n_trials=100": "n_trials=1"}
        ns = _run_blocks(SLUG, [2], namespace={"X_train": X, "y_train": y}, replace={2: one_trial})
        study = optuna.create_study(direction="maximize", pruner=ns["study"].pruner)
        for _ in range(5):
            study.add_trial(optuna.trial.create_trial(
                params={}, distributions={}, value=1.0,
                intermediate_values={step: 1.0 for step in range(5)},
            ))
        study.enqueue_trial({"n_estimators": 5, "learning_rate": 1e-3, "max_depth": 2, "subsample": 0.5})
        study.optimize(ns["objective"], n_trials=1)
        last = study.trials[-1]
        self.assertEqual(last.state, optuna.trial.TrialState.PRUNED)
        self.assertEqual(sorted(last.intermediate_values), [0, 1])

    def test_nested_cv_on_imbalanced_target_has_no_nan_folds(self):
        # Regression: plain KFold(shuffle=True, random_state=2) on this 7-positive / 80-row set
        # puts 0 positives in one outer fold -> ROC-AUC undefined -> nested_scores.mean() == nan
        # (observed with the original block). Stratified folds keep >=1 positive per fold.
        import numpy as np

        X, y = _data(n_samples=80, weights=[0.9], seed=1)
        self.assertEqual(int(y.sum()), 7)
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")  # tiny-forest / small-class warnings are expected here
            ns = _run_blocks(
                SLUG, [1, 3], namespace={"X_train": X, "y_train": y}, replace={1: FAST[1], 3: FAST[3]}
            )
        self.assertTrue(np.isfinite(ns["nested_scores"]).all(), ns["nested_scores"])

    def test_nested_cv_refuses_to_report_nan(self):
        # The step-4 guard turns a silent nan estimate into an explicit error.
        import numpy as np
        from sklearn.model_selection import KFold

        X, y = _data(n_samples=80, weights=[0.9], seed=1)
        replace = {1: FAST[1], 3: dict(FAST[3])}
        # Re-introduce the original non-stratified outer splitter to prove the guard fires.
        replace[3]["outer_cv = StratifiedKFold(n_splits=5, shuffle=True, random_state=2)"] = (
            "outer_cv = KFold(n_splits=5, shuffle=True, random_state=2)"
        )
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            with self.assertRaisesRegex(ValueError, "nan"):
                _run_blocks(
                    SLUG, [1, 3], namespace={"X_train": X, "y_train": y, "KFold": KFold, "np": np},
                    replace=replace,
                )


if __name__ == "__main__":
    unittest.main()
