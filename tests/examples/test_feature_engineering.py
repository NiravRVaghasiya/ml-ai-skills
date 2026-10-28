"""Executes feature-engineering/SKILL.md's python blocks against tiny synthetic data.

Nothing is stubbed. The skill's documented inputs — a cleaned feature frame
``X`` and a binary target ``y`` (see data-preprocessing) — are injected via
``namespace=``; the frame carries the columns the skill's code names
(``price``, ``sqft``, ``order_ts``, ``zip_code``) plus integer-valued noise
columns so SelectKBest has more than ``k=20`` candidates and mutual
information sees tied values. ``replace=`` only shrinks the random forests'
``n_estimators=300`` to 10 so the file stays fast.

Regression tests pin the defects found in the audit (each failed against the
pre-fix skill code):

* ``X_train_numeric`` was used by steps 4 and 7 but never defined -> NameError.
* Domain features were added to ``X_train`` only, so ``X_test`` lacked the
  columns the selected feature set refers to.
* ``TargetEncoder(target_type="continuous", ...)`` in a classification workflow:
  string labels raise, and multiclass labels are silently collapsed
  into one ordinal "mean class id" column. Its ``random_state=`` argument is
  deprecated in scikit-learn 1.9 (FutureWarning; removal planned for 1.11).
* ``mutual_info_classif`` was unseeded: it jitters tied values with random
  noise, so scores (and potentially the selected set) changed between runs.
* Step 7's "honest" CV pipeline validated a different procedure than the one
  that produced the selected features (no target encoding, no univariate
  filter, SelectFromModel's default "mean" threshold instead of step 5's
  "median"), so its score did not estimate the procedure actually used.
* RFECV ranked features by raw LogisticRegression ``|coef_|`` on unscaled
  engineered features (price, price_per_sqft, ...), which ranks by unit choice.

Found in the review round (each regression test failed against the previous fix):

* ``sqft == 0`` became a NaN ratio that no step imputed -> SelectKBest raised
  "Input X contains NaN" at step 4.
* The polynomial interaction arrays were computed but never joined, so the
  product feature never reached selection or the step-7 pipeline.
* The RFECV units test used data where rescaling never changed the unscaled
  result, so it passed even with the scaling fix reverted.
"""
from __future__ import annotations

import contextlib
import io
import unittest
import warnings

from examples import harness

SLUG = "feature-engineering"
FAST = {"n_estimators=300": "n_estimators=10"}


def make_xy(n: int = 200, seed: int = 0, labels: str = "binary"):
    import numpy as np
    import pandas as pd

    rng = np.random.default_rng(seed)
    sqft = rng.uniform(500, 3500, n)
    price = sqft * rng.uniform(150, 450, n)
    zips = np.array([f"z{i:02d}" for i in range(30)])
    zip_code = rng.choice(zips, n)
    zip_effect = dict(zip(zips, rng.normal(0, 1, 30)))
    X = pd.DataFrame({
        "price": price,
        "sqft": sqft,
        "order_ts": pd.Timestamp("2026-01-01") + pd.to_timedelta(rng.integers(0, 90 * 24, n), unit="h"),
        "zip_code": zip_code,
    })
    for i in range(24):                                     # tied integer noise features
        X[f"n{i}"] = rng.integers(0, 5, n)
    logit = 0.004 * (price / sqft - 300) + np.vectorize(zip_effect.get)(zip_code)
    y01 = (logit + rng.normal(0, 0.5, n) > 0).astype(int)
    if labels == "binary":
        y = pd.Series(y01)
    elif labels == "strings":
        y = pd.Series(np.where(y01 == 1, "churn", "stay"))
    else:                                                   # "multiclass"
        y = pd.Series(y01 + (rng.random(n) < 0.3).astype(int))
    return X, y


def run_skill(indices=None, labels="binary", n_zero_sqft=0):
    X, y = make_xy(labels=labels)
    X.loc[X.index[:n_zero_sqft], "sqft"] = 0.0              # set after y: label has no 0/0
    replace = {i: FAST for i in (4, 6) if indices is None or i in indices}
    with contextlib.redirect_stdout(io.StringIO()), warnings.catch_warnings():
        warnings.simplefilter("error", FutureWarning)       # deprecated APIs must not ship
        return harness.run_blocks(SLUG, indices, namespace={"X": X, "y": y}, replace=replace)


@harness.requires("numpy", "pandas", "sklearn")
class FeatureEngineeringExampleTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.ns = run_skill()

    def test_full_workflow_runs(self):
        import numpy as np

        ns = self.ns
        self.assertEqual(len(ns["scores"]), 5)
        self.assertTrue(np.all(np.isfinite(ns["scores"])))
        self.assertGreater(ns["scores"].mean(), 0.6)       # the synthetic signal is learnable
        self.assertEqual(len(ns["kept_cols"]), 20)
        self.assertTrue(set(ns["final_cols"]) <= set(ns["kept_cols"]))
        self.assertTrue(set(ns["selected"]) <= set(ns["final_cols"]))
        # The fitted pipeline is the reusable artifact: it scores raw (un-encoded) test rows.
        proba = ns["full_pipe"].predict_proba(ns["X_test"])
        self.assertEqual(proba.shape, (len(ns["X_test"]), 2))

    def test_numeric_matrices_are_defined_for_both_splits(self):
        ns = self.ns
        self.assertIn("zip_te", ns["X_train_numeric"].columns)
        self.assertNotIn("zip_code", ns["X_train_numeric"].columns)
        self.assertEqual(list(ns["X_train_numeric"].columns), list(ns["X_test_numeric"].columns))

    def test_domain_features_reach_the_test_split(self):
        for col in ("price_per_sqft", "order_dow", "order_hour", "order_is_weekend"):
            self.assertIn(col, self.ns["X_test"].columns)
        self.assertNotIn("order_ts", self.ns["X_test"].columns)
        self.assertEqual(self.ns["X_test_inter"].shape[1], 3)   # price, sqft, price*sqft

    def test_zero_sqft_becomes_nan_not_inf(self):
        import numpy as np
        import pandas as pd

        frame = pd.DataFrame({"price": [1.0, 2.0], "sqft": [0.0, 4.0],
                              "order_ts": pd.to_datetime(["2026-01-03", "2026-01-05"])})
        out = self.ns["add_domain_features"](frame)
        self.assertTrue(np.isnan(out["price_per_sqft"].iloc[0]))
        self.assertEqual(out["price_per_sqft"].iloc[1], 0.5)
        self.assertEqual(out["order_is_weekend"].tolist(), [1, 0])   # Sat, Mon
        self.assertIn("order_ts", frame.columns)                     # caller's frame untouched

    def test_zero_sqft_rows_do_not_crash_the_workflow(self):
        # Pre-fix (review round): step 2 turned sqft == 0 into a NaN ratio and never
        # imputed it -> "Input X contains NaN" from SelectKBest in step 4.
        import numpy as np
        import pandas as pd

        ns = run_skill(n_zero_sqft=12)
        self.assertTrue(ns["X_train"]["sqft"].eq(0).any() and ns["X_test"]["sqft"].eq(0).any())
        for split in ("X_train_numeric", "X_test_numeric"):
            self.assertFalse(ns[split].isna().any().any(), split)
        # The reusable pipeline also imputes a NaN ratio arriving in new rows.
        new_row = ns["X_test"].head(1).copy()
        new_row["price_per_sqft"] = np.nan
        self.assertTrue(np.all(np.isfinite(ns["full_pipe"].predict_proba(pd.DataFrame(new_row)))))

    def test_interaction_features_reach_selection_and_the_pipeline(self):
        # Pre-fix (review round): X_train_inter / X_test_inter were computed but never
        # joined, so no later step (or the pipeline) ever saw the product feature.
        import numpy as np

        ns = self.ns
        self.assertEqual(ns["new_cols"], ["price sqft"])
        for split in ("X_train_numeric", "X_test_numeric"):
            self.assertIn("price sqft", ns[split].columns)
        self.assertIn("price sqft", ns["full_pipe"].feature_names_in_)
        np.testing.assert_allclose(ns["X_test"]["price sqft"], ns["X_test"]["price"] * ns["X_test"]["sqft"])

    def test_target_encoder_accepts_string_labels(self):
        ns = run_skill(indices=[0, 1, 2], labels="strings")
        self.assertEqual(ns["zip_encoded_train"].shape, (len(ns["X_train"]), 1))

    def test_target_encoder_type_matches_classification_task(self):
        self.assertEqual(self.ns["te"].target_type_, "binary")

    def test_mutual_information_selection_is_reproducible(self):
        import numpy as np

        again = run_skill(indices=[0, 1, 2, 3])
        np.testing.assert_array_equal(self.ns["selector"].scores_, again["selector"].scores_)

    def test_cv_pipeline_reruns_the_same_selection_procedure(self):
        steps = self.ns["full_pipe"].named_steps
        self.assertEqual(list(steps), ["encode", "impute", "filter", "embedded", "scale", "model"])
        self.assertEqual(steps["embedded"].threshold, self.ns["embedded"].threshold)
        self.assertEqual(steps["filter"].k, self.ns["selector"].k)

    def test_rfecv_selection_does_not_depend_on_feature_units(self):
        # Step 6 alone on a small well-conditioned frame (fast and deterministic):
        # 3 informative + 3 noise columns. Rescaling informative "a" by 1e3 is the same
        # information in other units but shrinks its raw |coef_| ~1e3x. Pre-fix
        # (unscaled LogisticRegression) that changed the result: ranking
        # [1,1,1,1,2,1] -> [1,1,1,1,1,1] (review round: the old version of this test
        # used workflow data where rescaling never changed anything, so it could not
        # catch the revert). With StandardScaler inside, the ranking is unchanged.
        import numpy as np
        import pandas as pd
        from sklearn.model_selection import StratifiedKFold
        from sklearn.pipeline import Pipeline
        from sklearn.preprocessing import StandardScaler

        rng = np.random.default_rng(7)
        frame = pd.DataFrame(rng.normal(size=(300, 6)), columns=list("abcxyz"))
        target = pd.Series((frame.a + frame.b + frame.c + rng.normal(0, 0.5, 300) > 0).astype(int))
        inputs = {"final_cols": frame.columns, "y_train": target,
                  "cv5": StratifiedKFold(5, shuffle=True, random_state=42)}
        base = harness.run_blocks(SLUG, [5], namespace={**inputs, "X_train_numeric": frame})
        rescaled = frame.assign(a=frame.a * 1e3)
        again = harness.run_blocks(SLUG, [5], namespace={**inputs, "X_train_numeric": rescaled})
        self.assertIn("a", base["selected"])
        self.assertEqual(again["rfecv"].ranking_.tolist(), base["rfecv"].ranking_.tolist())
        # And the wiring itself: coefficients come from a scaled LogisticRegression.
        est = self.ns["rfecv"].estimator
        self.assertIsInstance(est, Pipeline)
        self.assertIsInstance(est.steps[0][1], StandardScaler)

    def test_target_encoder_gotcha_fit_then_transform_is_not_cross_fitted(self):
        import numpy as np
        from sklearn.preprocessing import TargetEncoder

        rng = np.random.default_rng(0)
        Xc, yc = rng.choice(list("abcdefghij"), (200, 1)), rng.integers(0, 2, 200)
        te = TargetEncoder(target_type="binary")
        self.assertFalse(np.allclose(te.fit_transform(Xc, yc), te.fit(Xc, yc).transform(Xc)))

    def test_polynomial_gotcha_feature_count(self):
        # Gotcha claims degree=2 on 50 numeric columns produces >1,200 features.
        import numpy as np
        from sklearn.preprocessing import PolynomialFeatures

        for kwargs in ({}, {"include_bias": False}, {"interaction_only": True, "include_bias": False}):
            n_out = PolynomialFeatures(degree=2, **kwargs).fit(np.zeros((2, 50))).n_output_features_
            self.assertGreater(n_out, 1200, kwargs)


if __name__ == "__main__":
    unittest.main()
