"""Executable-example tests for ml-problem-framing/SKILL.md.

The skill generates its own synthetic churn table (step 2), so no data or
external service is stubbed and the blocks run verbatim. The only stub is
DATACLASS_STUB below, an empty sys.modules entry that works around a harness
artifact (see the comment there); it does not replace any skill code.
"""
from __future__ import annotations

import unittest

from examples import harness

SLUG = "ml-problem-framing"
# Workaround for a harness artifact, not a skill defect: harness.run_blocks calls
# compile() without dont_inherit=True, so the exec'd code inherits harness.py's
# `from __future__ import annotations`; @dataclass then resolves the string
# annotations via sys.modules[cls.__module__], which doesn't exist for the
# harness namespace. Registering an empty module under that name is enough.
_NS_MODULE = "skill_example_ml_problem_framing"
DATACLASS_STUB = {_NS_MODULE: harness.make_module(_NS_MODULE)}


@harness.requires("numpy", "pandas", "sklearn")
class MlProblemFramingExamples(unittest.TestCase):
    def test_full_workflow_runs_verbatim(self):
        ns = harness.run_blocks(SLUG, stubs=DATACLASS_STUB)
        df = ns["df"]
        # Label is rebuilt from cancel_date with the (cutoff, cutoff + horizon] window.
        cutoff, horizon = ns["scoring_date"], ns["horizon"]
        in_window = (df["cancel_date"] > cutoff) & (df["cancel_date"] <= cutoff + horizon)
        self.assertTrue((df["churn"] == in_window.astype(int)).all())
        self.assertEqual(list(ns["X"].columns), ns["feature_cols"])
        self.assertTrue(ns["ship_it"])

    def test_choose_formulation_rejects_unknown_label_type(self):
        ns = harness.run_blocks(SLUG, [2])
        with self.assertRaises(ValueError):
            ns["choose_formulation"](label_type="binary", needs_ranking=False, has_labels=True)

    # --- regression tests: each failed on the pre-2026-10-27 code -------------

    def _audit_ns(self):
        return harness.run_blocks(SLUG, [1, 3])

    def test_leakage_audit_survives_python_O(self):
        # The original check was an `assert`, which `python -O` removes: compiled
        # with optimize=2 a leaking feature passed silently. Re-run the fixed
        # block's audit function compiled the same way.
        ns = self._audit_ns()
        code = harness.python_blocks(SLUG)[3]
        exec(compile(code, "ml-problem-framing block #3 (-O)", "exec", optimize=2), ns)
        leaky = dict(ns["feature_asof"], monthly_spend=ns["scoring_date"] + ns["horizon"])
        with self.assertRaisesRegex(ValueError, "after"):
            ns["audit_feature_asof"](ns["feature_cols"], leaky, ns["scoring_date"])

    def test_leakage_audit_requires_every_feature_to_be_declared(self):
        # The original check only looked at signup_date, so any other leaky column
        # (e.g. days_until_cancel) passed. The audit now refuses undeclared features.
        ns = self._audit_ns()
        with self.assertRaisesRegex(ValueError, "days_until_cancel"):
            ns["audit_feature_asof"](ns["feature_cols"] + ["days_until_cancel"],
                                     ns["feature_asof"], ns["scoring_date"])

    def test_degenerate_label_is_rejected_before_splitting(self):
        # Original printed the class balance but continued; a constant label then
        # crashed later inside train_test_split(stratify=...).
        with self.assertRaisesRegex(ValueError, "degenerate"):
            harness.run_blocks(SLUG, [1], replace={1: {
                'df["churn"] = rng.binomial(1, churn_prob)': 'df["churn"] = 0',
            }})

    def test_degenerate_final_label_is_rejected(self):
        # Step 4 rebuilds the label from cancel_date; the step-2 check alone let a
        # window with no positives through to step 5, which then crashed with
        # "IndexError: index 1 is out of bounds" at dummy.predict_proba(...)[:, 1].
        with self.assertRaisesRegex(ValueError, "degenerate"):
            harness.run_blocks(SLUG, stubs=DATACLASS_STUB, replace={3: {
                "horizon = pd.Timedelta(days=30)": "horizon = pd.Timedelta(days=0)",
            }})

    def test_leakage_audit_rejects_null_asof(self):
        # `NaT > cutoff` is False, so a null as-of timestamp (e.g. .max() of an
        # empty column) used to pass the audit silently.
        import pandas as pd

        ns = self._audit_ns()
        with self.assertRaisesRegex(ValueError, "monthly_spend"):
            ns["audit_feature_asof"](ns["feature_cols"],
                                     dict(ns["feature_asof"], monthly_spend=pd.NaT),
                                     ns["scoring_date"])


if __name__ == "__main__":
    unittest.main()
