"""Executes python-for-ml/SKILL.md's single python block and checks its claims.

python-for-ml is a reference skill: one illustrative-but-runnable block plus
Key Concepts/Gotchas prose. The block runs verbatim (it builds its own data);
the remaining tests pin the library behaviours the prose asserts, against the
installed numpy/pandas, so a wrong claim fails here instead of misleading users.

Defects found in the audit. The constant-row test failed against the pre-fix
block; the remaining defects were wrong *prose*, so their tests pin the actual
library behaviour that the corrected text now states:

* Row normalisation divided by a zero standard deviation for a constant row ->
  NaN row plus a RuntimeWarning (the replace= below injects such rows). The
  first fix (``np.where(std == 0, 1.0, std)``) used exact float equality, so a
  constant row whose std rounds to ~1e-14 instead of 0 normalised to +/-1.
* "check ``arr.base is not None`` to see if something is a view": a fancy-indexed
  *copy* can have a non-None ``.base`` (``a[:, [0, 1]]``), so the check reports a
  copy as a view; ``np.shares_memory`` is the reliable test.
* "Mixed types force dtype=object": ``np.array([1, "a"])`` is a ``<U21`` string
  array and ``[1, 2.5]`` upcasts to float64; only e.g. ``None``/arbitrary
  objects give ``object``.
* "on Windows, numpy's default int dtype is int32": true only before NumPy 2.0;
  and ``int32.sum()`` accumulates in the (int64) platform integer, so the
  overflow example given did not overflow. Elementwise int32 arithmetic does wrap.
"""
from __future__ import annotations

import contextlib
import sys
import unittest
import warnings

from examples import harness

SLUG = "python-for-ml"
CONSTANT_ROW = {0: {
    "X = np.random.default_rng(0).normal(size=(1000, 5))":
    # 3.0 gives an exact zero std; 123.456 (5 columns) leaves a ~1.4e-14 rounding
    # residue, which an exact ``std == 0`` guard misses (that row became 1, not 0).
    "X = np.random.default_rng(0).normal(size=(1000, 5)); X[0] = 3.0; X[1] = 123.456",
}}


@harness.requires("numpy", "pandas")
class PythonForMlExampleTest(unittest.TestCase):
    def test_block_runs_verbatim(self):
        import numpy as np

        ns = harness.run_blocks(SLUG)
        X_norm = ns["X_norm"]
        self.assertEqual(X_norm.shape, (1000, 5))
        np.testing.assert_allclose(X_norm.mean(axis=1), 0, atol=1e-12)
        np.testing.assert_allclose(X_norm.std(axis=1), 1, atol=1e-12)
        self.assertEqual(ns["totals"].to_dict(), {"a": 40, "b": 20, "c": 40})
        self.assertEqual(ns["df"]["tier"].tolist(), ["low", "mid", "mid", "high"])

    def test_constant_row_normalizes_to_zero_not_nan(self):
        import numpy as np

        with warnings.catch_warnings():
            warnings.simplefilter("error", RuntimeWarning)   # no divide-by-zero warning either
            ns = harness.run_blocks(SLUG, replace=CONSTANT_ROW)
        self.assertTrue(np.all(np.isfinite(ns["X_norm"])))
        np.testing.assert_array_equal(ns["X_norm"][:2], 0.0)
        self.assertNotEqual(ns["std"][1, 0], 0.0)    # the residue case is actually exercised

    # --- claims in Key Concepts / Gotchas -------------------------------------
    def test_views_vs_copies(self):
        import numpy as np

        a = np.arange(12).reshape(3, 4)
        self.assertTrue(np.shares_memory(a, a[1:2]))         # basic slice: view
        fancy = a[:, [0, 1]]
        self.assertFalse(np.shares_memory(a, fancy))          # fancy index: copy ...
        self.assertIsNotNone(fancy.base)                      # ... yet .base is set
        self.assertFalse(np.shares_memory(a, a[a > 3]))       # boolean mask: copy

    def test_mixed_type_dtypes(self):
        import numpy as np

        self.assertEqual(np.array([1, "a"]).dtype.kind, "U")
        self.assertEqual(np.array([1, 2.5]).dtype, np.float64)
        self.assertEqual(np.array([1, None]).dtype, object)

    def test_integer_overflow_claims(self):
        import numpy as np

        big = np.array([2**31 - 1, 1], dtype=np.int32)
        self.assertEqual((big + np.int32(1))[0], -(2**31))   # elementwise: wraps silently
        if int(np.__version__.split(".")[0]) >= 2 and sys.maxsize > 2**32:
            self.assertEqual(np.array([1, 2, 3]).dtype, np.int64)    # incl. Windows on NumPy 2
            self.assertEqual(big.sum(), 2**31)                       # reduction upcasts

    def test_broadcasting_n_vs_n1_silently_makes_matrix(self):
        import numpy as np

        self.assertEqual((np.ones(3) + np.ones((3, 1))).shape, (3, 3))

    def test_chained_assignment_never_writes_under_copy_on_write(self):
        import pandas as pd

        major = int(pd.__version__.split(".")[0])
        ctx = pd.option_context("mode.copy_on_write", True) if major < 3 else contextlib.nullcontext()
        with ctx, warnings.catch_warnings():
            warnings.simplefilter("ignore")                   # ChainedAssignmentError warning
            df = pd.DataFrame({"x": [1, -1, 2], "y": [0, 0, 0]})
            df[df.x > 0]["y"] = 1
            self.assertEqual(df["y"].tolist(), [0, 0, 0])
            df.loc[df.x > 0, "y"] = 1
            self.assertEqual(df["y"].tolist(), [1, 0, 1])
            sub = df.iloc[:2]
            sub.iloc[0, 0] = 99
            self.assertEqual(df["x"].tolist(), [1, -1, 2])    # subset write never leaks back

    def test_iloc_subset_write_leaks_without_copy_on_write(self):
        import pandas as pd

        if int(pd.__version__.split(".")[0]) >= 3:
            self.skipTest("pandas >= 3 always uses Copy-on-Write")
        with pd.option_context("mode.copy_on_write", False), warnings.catch_warnings():
            warnings.simplefilter("ignore")                   # SettingWithCopyWarning
            df = pd.DataFrame({"x": [1.0, 2.0, 3.0]})
            sub = df.iloc[:2]
            sub.iloc[0, 0] = 99.0
            self.assertEqual(df["x"].tolist(), [99.0, 2.0, 3.0])


if __name__ == "__main__":
    unittest.main()
