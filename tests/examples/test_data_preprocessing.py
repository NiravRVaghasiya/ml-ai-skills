"""Executes data-preprocessing/SKILL.md's python blocks against tiny synthetic data.

Nothing is stubbed: the only input the skill expects from the user is a raw
DataFrame ``df`` with a label column, which is injected via ``namespace=``
(column ``"target"``, the name the skill's split step uses). The blocks write
``model.joblib`` into the harness's temporary working directory.

Regression tests pin the defects found in the audit (each failed against the
pre-fix skill code):

* Step 2 split ``X, y`` that no block ever derived from ``df`` -> NameError
  when the Workflow is followed top to bottom.
* Missing categoricals stored as ``None`` (what ``pd.DataFrame`` keeps for
  object columns built from Python lists / JSON) were never imputed:
  ``SimpleImputer``'s default ``missing_values=np.nan`` does not match ``None``,
  so they reached ``OneHotEncoder`` as a literal ``"None"`` category even
  though step 1's ``df.isna()`` profile counted them as missing.
* The first fix for that (``df.fillna(np.nan)``) downcast object columns of
  numeric codes to float64 (pandas 2.2 FutureWarning), silently moving a
  categorical into ``num_cols``; ``mask(isna, np.nan)`` keeps the dtype.
* That ``mask`` then ran in step 2, outside the persisted ``clf``: at serving
  time a raw ``None`` skipped imputation and was one-hot encoded as an
  all-zero "unknown" row, so ``None`` and ``NaN`` got different predictions.
  The normalisation is now a pipeline step, so the joblib artifact applies it.
* Step 3's dtype-based column groups silently omit bool and pandas
  ``"string"``-dtype columns, and ``ColumnTransformer``'s default
  ``remainder="drop"`` then throws those features away without any error.
"""
from __future__ import annotations

import contextlib
import io
import os
import unittest

from examples import harness

SLUG = "data-preprocessing"


def make_df(n: int = 120, seed: int = 0, **extra_columns):
    import numpy as np
    import pandas as pd

    rng = np.random.default_rng(seed)
    age = rng.normal(40, 10, n)
    age[rng.random(n) < 0.1] = np.nan                     # numeric missingness
    city = rng.choice(["nyc", "sf", "austin"], n).astype(object)
    city[rng.random(n) < 0.1] = None                      # categorical missingness
    df = pd.DataFrame({
        "age": age,
        "income": rng.lognormal(10, 0.5, n),
        "city": city,
        "plan": pd.Categorical(rng.choice(["free", "pro"], n)),
    })
    df["target"] = ((df["income"] > df["income"].median()) ^ (rng.random(n) < 0.1)).astype(int)
    for name, values in extra_columns.items():
        df[name] = values
    return df


def run_skill(df, indices=None):
    with contextlib.redirect_stdout(io.StringIO()):     # step 1 prints a profile
        return harness.run_blocks(SLUG, indices, namespace={"df": df})


@harness.requires("numpy", "pandas", "sklearn", "joblib")
class DataPreprocessingExampleTest(unittest.TestCase):
    def test_full_workflow_runs_and_generalizes_to_unseen_categories(self):
        import joblib
        import pandas as pd
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            with contextlib.redirect_stdout(io.StringIO()):
                ns = harness.run_blocks(SLUG, namespace={"df": make_df()}, workdir=tmp)
            self.assertTrue(os.path.exists(os.path.join(tmp, "model.joblib")))
            reloaded = joblib.load(os.path.join(tmp, "model.joblib"))   # our own artifact: trusted

        clf, X_test = ns["clf"], ns["X_test"]
        self.assertEqual(sorted(ns["num_cols"]), ["age", "income"])
        self.assertEqual(sorted(ns["cat_cols"]), ["city", "plan"])
        self.assertNotIn("target", ns["X"].columns)          # label never enters the features
        self.assertEqual(len(X_test), 24)                      # test_size=0.2 of 120 rows

        # handle_unknown="ignore": a category never seen in training must not crash inference.
        unseen = X_test.head(3).copy()
        unseen["city"] = "a-city-never-seen-in-training"
        self.assertEqual(len(clf.predict(unseen)), 3)
        pd.testing.assert_series_equal(
            pd.Series(reloaded.predict(X_test)), pd.Series(clf.predict(X_test)))

    def test_split_derives_X_y_from_df(self):
        # Pre-fix: block #1 referenced X/y that no block defined -> NameError.
        ns = run_skill(make_df(), indices=[0, 1])
        self.assertEqual(len(ns["X_train"]) + len(ns["X_test"]), 120)
        self.assertNotIn("target", ns["X_train"].columns)

    def test_none_categoricals_are_imputed_not_encoded_as_a_category(self):
        ns = run_skill(make_df(), indices=[0, 1, 2, 3, 4])
        self.assertGreater(ns["df"]["city"].isna().sum(), 0)    # make_df stores them as None
        names = ns["clf"].named_steps["prep"].get_feature_names_out().tolist()
        self.assertNotIn("cat__city_None", names)
        self.assertEqual(sorted(n for n in names if n.startswith("cat__city_")),
                         ["cat__city_austin", "cat__city_nyc", "cat__city_sf"])

    def test_persisted_pipeline_treats_none_like_nan_at_serving_time(self):
        # Pre-fix: None -> NaN ran in step 2, outside clf, so a raw serving row with
        # None (what pd.DataFrame([json_record]) holds) skipped the imputer and was
        # encoded as an all-zero unknown category, unlike the same row with NaN.
        import joblib
        import tempfile

        import numpy as np
        import pandas as pd

        with tempfile.TemporaryDirectory() as tmp:
            with contextlib.redirect_stdout(io.StringIO()):
                harness.run_blocks(SLUG, namespace={"df": make_df()}, workdir=tmp)
            reloaded = joblib.load(os.path.join(tmp, "model.joblib"))   # our own artifact: trusted

        raw = pd.DataFrame([{"age": 40.0, "income": 22000.0, "city": None, "plan": None}])
        as_nan = pd.DataFrame([{"age": 40.0, "income": 22000.0, "city": np.nan, "plan": np.nan}],
                              dtype=object).astype({"age": float, "income": float})
        self.assertIsNone(raw.loc[0, "city"])
        self.assertTrue(np.isnan(as_nan.loc[0, "city"]))

        prep = reloaded.named_steps["prep"]

        def encoded(frame):
            out = prep.transform(frame)
            return out.toarray() if hasattr(out, "toarray") else np.asarray(out)

        np.testing.assert_array_equal(encoded(raw), encoded(as_nan))
        np.testing.assert_array_equal(reloaded.predict_proba(raw), reloaded.predict_proba(as_nan))
        # ...and the missing city was imputed (one city column hot), not encoded as unknown.
        names = prep.get_feature_names_out().tolist()
        city_hot = [v for n, v in zip(names, encoded(raw)[0]) if n.startswith("cat__city_")]
        self.assertEqual(sum(city_hot), 1.0)

    def test_none_normalisation_keeps_object_code_columns_categorical(self):
        # Pre-fix (review round): step 2 used df.fillna(np.nan), which on pandas 2.2
        # emits a downcasting FutureWarning and turns an object column of numeric
        # codes with None into float64 -> it moved to num_cols and was median-imputed
        # and scaled instead of one-hot encoded.
        import warnings

        import numpy as np
        import pandas as pd

        codes = pd.Series(np.random.default_rng(4).choice([10, 20, 30], 120), dtype=object)
        codes[:6] = None
        with warnings.catch_warnings():
            warnings.simplefilter("error", FutureWarning)
            ns = run_skill(make_df(store_code=codes), indices=[0, 1, 2, 3, 4])
        self.assertIn("store_code", ns["cat_cols"])
        self.assertEqual(ns["X"]["store_code"].dtype, object)
        names = ns["clf"].named_steps["prep"].get_feature_names_out().tolist()
        self.assertEqual(sorted(n for n in names if n.startswith("cat__store_code_")),
                         ["cat__store_code_10", "cat__store_code_20", "cat__store_code_30"])

    def test_bool_column_is_not_silently_dropped(self):
        import numpy as np

        flags = np.random.default_rng(1).random(120) < 0.5
        with self.assertRaisesRegex(ValueError, "is_member"):
            run_skill(make_df(is_member=flags), indices=[0, 1, 2])

    def test_string_dtype_column_is_not_silently_dropped(self):
        import numpy as np
        import pandas as pd

        tier = pd.array(np.random.default_rng(2).choice(["a", "b"], 120), dtype="string")
        with self.assertRaisesRegex(ValueError, "tier"):
            run_skill(make_df(tier=tier), indices=[0, 1, 2])

    def test_cast_advice_in_guard_message_makes_columns_usable(self):
        # The guard's error message tells users how to cast; that advice must work
        # (string -> object *then* fillna; the other order leaves pd.NA, which
        # SimpleImputer rejects with "boolean value of NA is ambiguous").
        import numpy as np
        import pandas as pd

        rng = np.random.default_rng(3)
        tier = pd.array(rng.choice(["a", "b"], 120), dtype="string")
        tier[:5] = pd.NA
        df = make_df(is_member=rng.random(120) < 0.5, tier=tier)
        df["is_member"] = df["is_member"].astype(int)
        df["tier"] = df["tier"].astype(object).fillna(np.nan)
        ns = run_skill(df, indices=[0, 1, 2, 3, 4])
        names = ns["clf"].named_steps["prep"].get_feature_names_out().tolist()
        self.assertIn("num__is_member", names)
        self.assertEqual(sorted(n for n in names if n.startswith("cat__tier_")), ["cat__tier_a", "cat__tier_b"])

    def test_original_silent_drop_mechanism(self):
        # Documents WHY the guard exists: the library itself raises nothing.
        import pandas as pd
        from sklearn.compose import ColumnTransformer
        from sklearn.preprocessing import StandardScaler

        frame = pd.DataFrame({"n": [1.0, 2.0, 3.0], "flag": [True, False, True]})
        self.assertEqual(frame.select_dtypes("number").columns.tolist(), ["n"])
        self.assertEqual(frame.select_dtypes(["object", "category"]).columns.tolist(), [])
        out = ColumnTransformer([("num", StandardScaler(), ["n"])]).fit_transform(frame)
        self.assertEqual(out.shape, (3, 1))                    # "flag" vanished, no warning


if __name__ == "__main__":
    unittest.main()
