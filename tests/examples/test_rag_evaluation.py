"""Executes rag-evaluation/SKILL.md's python blocks (pure stdlib metrics).

Every block runs verbatim; there is nothing to stub (the NLI model call in
step 4 lives in llm-evaluation and is not executed here). nDCG is
cross-checked against scikit-learn's independent ``ndcg_score``.

Regression tests (each failed against the pre-fix skill code):

* ndcg_at_k counted a duplicated relevant doc id once per occurrence, so the
  skill's own example ranking scored nDCG@5 = 1.018 (> 1).
* Negative k silently sliced from the end of the list (``retrieved[:-1]``)
  and precision_at_k(k=-1) returned a negative number.
* A plain mean over per-query recall is poisoned by the NaN returned for
  unlabeled queries (new ``mean_metric`` helper).
* precision_at_k / reciprocal_rank / context_precision returned 0.0 for an
  unlabeled query while recall/nDCG returned NaN, so MRR or mean precision
  via ``mean_metric`` was silently deflated (MRR 0.5 instead of 1.0).
* citation_correctness raised KeyError on an uncited claim.
* scan_failure_modes treated a doc id missing from ``context_tokens`` as
  0 tokens, hiding context overflow.
"""
from __future__ import annotations

import contextlib
import io
import math
import unittest

from examples import harness

SLUG = "rag-evaluation"


def run_skill():
    with contextlib.redirect_stdout(io.StringIO()):
        return harness.run_blocks(SLUG)


class RagEvaluationExampleTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.ns = run_skill()

    def test_skill_example_values(self):
        ns = self.ns
        retrieved, relevant = ns["retrieved"], ns["relevant"]
        self.assertEqual(ns["recall_at_k"](retrieved, relevant, 3), 1.0)
        self.assertAlmostEqual(ns["precision_at_k"](retrieved, relevant, 3), 1 / 3)
        self.assertEqual(ns["reciprocal_rank"](retrieved, relevant), 0.5)
        self.assertAlmostEqual(ns["ndcg_at_k"](retrieved, relevant, 3), 1 / math.log2(3))
        self.assertEqual(ns["context_precision"](ns["final_context_ids"], relevant), 0.5)

    def test_unlabeled_and_empty_inputs(self):
        ns = self.ns
        for fn in ("recall_at_k", "precision_at_k", "ndcg_at_k"):
            with self.subTest(fn=fn):
                self.assertTrue(math.isnan(ns[fn](["a"], set(), 3)))
        self.assertTrue(math.isnan(ns["reciprocal_rank"](["a"], set())))
        self.assertTrue(math.isnan(ns["context_precision"](["a"], set())))
        self.assertEqual(ns["recall_at_k"]([], {"a"}, 3), 0.0)
        self.assertEqual(ns["precision_at_k"]([], {"a"}, 3), 0.0)
        self.assertEqual(ns["reciprocal_rank"]([], {"a"}), 0.0)
        self.assertEqual(ns["ndcg_at_k"]([], {"a"}, 3), 0.0)
        self.assertEqual(ns["context_precision"]([], {"a"}), 0.0)

    def test_k_larger_than_list_uses_k_denominator_for_precision(self):
        self.assertAlmostEqual(self.ns["precision_at_k"](["a"], {"a"}, 10), 0.1)
        self.assertEqual(self.ns["recall_at_k"](["a"], {"a", "b"}, 10), 0.5)

    @harness.requires("numpy", "sklearn")
    def test_ndcg_matches_sklearn(self):
        import numpy as np
        from sklearn.metrics import ndcg_score

        cases = [
            (["d1", "d2", "d3", "d4", "d5"], {"d2", "d5", "d9"}, 3),
            (["d1", "d2", "d3", "d4", "d5"], {"d2", "d5", "d9"}, 5),
            (["d4", "d1", "d2"], {"d1"}, 2),
            (["d1", "d2"], {"d1", "d2"}, 5),
        ]
        for retrieved, relevant, k in cases:
            docs = list(dict.fromkeys(retrieved + sorted(relevant)))
            y_true = np.array([[1.0 if d in relevant else 0.0 for d in docs]])
            # rank order -> descending scores; unretrieved docs get scores below every retrieved one
            y_score = np.array([[len(docs) - retrieved.index(d) if d in retrieved else -1.0 - i
                                 for i, d in enumerate(docs)]])
            with self.subTest(retrieved=retrieved, relevant=relevant, k=k):
                self.assertAlmostEqual(self.ns["ndcg_at_k"](retrieved, relevant, k),
                                       float(ndcg_score(y_true, y_score, k=k)))

    def test_failure_mode_tags(self):
        scan = self.ns["scan_failure_modes"]
        self.assertEqual(scan("q", ["x"], {"a"}, ["x"], 100, {"x": 10}), ["retrieval_failure"])
        self.assertEqual(scan("q", ["x", "a"], {"a"}, ["x"], 100, {"x": 10}), ["reranking_failure"])
        self.assertEqual(scan("q", ["a"], {"a"}, ["a"], 5, {"a": 10}), ["context_overflow"])
        self.assertEqual(scan("q", ["a"], {"a"}, ["a"], 100, {"a": 10}), [])

    def test_faithfulness_pairs(self):
        self.assertEqual(self.ns["faithfulness_pairs"](["s1", "s2"], "ctx"), [("s1", "ctx"), ("s2", "ctx")])

    # --- regressions -------------------------------------------------------
    def test_ndcg_never_exceeds_one_with_duplicate_ids(self):
        # Pre-fix: the skill's own example ranking (policy_042 at ranks 2 and 5) gave 1.018.
        ns = self.ns
        self.assertLessEqual(ns["ndcg_at_k"](ns["retrieved"], ns["relevant"], 5), 1.0)
        self.assertAlmostEqual(ns["ndcg_at_k"](["a", "a", "a"], {"a"}, 3), 1.0)

    def test_non_positive_k_is_rejected(self):
        for fn in ("recall_at_k", "precision_at_k", "ndcg_at_k"):
            with self.subTest(fn=fn), self.assertRaises(ValueError):
                self.ns[fn](["a", "b"], {"b"}, -1)

    def test_mean_metric_skips_unlabeled_nan(self):
        mean_metric = self.ns["mean_metric"]
        self.assertEqual(mean_metric([1.0, float("nan"), 0.0]), 0.5)
        self.assertTrue(math.isnan(mean_metric([float("nan")])))
        self.assertTrue(math.isnan(mean_metric([])))

    def test_unlabeled_query_does_not_deflate_mrr_or_mean_precision(self):
        # Pre-fix: reciprocal_rank/precision_at_k returned 0.0 for an empty gold set -> MRR 0.5.
        ns = self.ns
        rr = [ns["reciprocal_rank"](["a"], {"a"}), ns["reciprocal_rank"](["a"], set())]
        self.assertEqual(ns["mean_metric"](rr), 1.0)
        p = [ns["precision_at_k"](["a"], {"a"}, 1), ns["precision_at_k"](["a"], set(), 1)]
        self.assertEqual(ns["mean_metric"](p), 1.0)

    def test_example_reports_skipped_unlabeled_count(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            harness.run_blocks(SLUG)
        self.assertIn("unlabeled queries skipped", out.getvalue())

    def test_uncited_claim_is_reported_not_crash(self):
        broken = self.ns["citation_correctness"](
            [{"claim": "30 days"}, {"claim": "x", "cited_doc_id": "policy_042"},
             {"claim": "y", "cited_doc_id": "ghost"}],
            {"policy_042": "Refunds within 30 days."},
        )
        self.assertEqual([c["claim"] for c in broken], ["30 days", "y"])

    def test_unknown_token_count_is_flagged(self):
        tags = self.ns["scan_failure_modes"]("q", ["a", "b"], {"a"}, ["a", "b"], 100, {"a": 10})
        self.assertIn("context_tokens_unknown", tags)


if __name__ == "__main__":
    unittest.main()
