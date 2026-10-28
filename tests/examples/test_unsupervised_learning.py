"""Executable-example tests for unsupervised-learning/SKILL.md.

Nothing is stubbed: the only external input is the user's feature matrix ``X``,
injected via ``namespace=`` as small synthetic data (make_blobs) so the whole
workflow runs in a few seconds on CPU.
"""
from __future__ import annotations

import unittest
import warnings

from examples import harness

SLUG = "unsupervised-learning"
# Iteration counts only (speed): KMeans restarts and t-SNE optimizer steps.
# Each target must match exactly once, so a skill edit can't silently skip it.
FAST = {
    1: {"n_init=10": "n_init=2"},
    2: {"n_init=10": "n_init=2"},
    5: {"random_state=42).fit_transform": "random_state=42, max_iter=250).fit_transform"},
}


def fast(*indices):
    return {i: FAST[i] for i in indices if i in FAST}


@harness.requires("numpy", "sklearn")
class UnsupervisedLearningExamples(unittest.TestCase):
    def _blobs(self, n_samples, n_features=4, centers=3):
        from sklearn.datasets import make_blobs

        X, _ = make_blobs(n_samples=n_samples, centers=centers, n_features=n_features,
                          cluster_std=0.6, random_state=0)
        return X

    def test_full_workflow_runs(self):
        X = self._blobs(90)
        X[:, 0] *= 1000.0  # one large-range column: the reason step 1 scales
        ns = harness.run_blocks(SLUG, namespace={"X": X}, replace=fast(*range(6)))
        self.assertEqual(ns["best_k"], 3)  # well-separated blobs: silhouette picks the true k
        self.assertEqual(len(ns["labels"]), len(X))
        self.assertEqual(ns["X_tsne"].shape, (len(X), 2))
        self.assertGreaterEqual(ns["pca"].explained_variance_ratio_.sum(), 0.95)

    # --- regression tests: each failed on the pre-2026-10-27 code -------------

    def test_k_search_caps_k_below_n_samples(self):
        # Original loop ran k=2..9 unconditionally; with 6 rows silhouette_score
        # raised "Number of labels is 6. Valid values are 2 to n_samples - 1".
        X = self._blobs(6, centers=2)
        ns = harness.run_blocks(SLUG, [0, 1, 2], namespace={"X": X}, replace=fast(1, 2))
        self.assertTrue(all(k < len(X) for k in ns["scores"]))

    def test_tsne_perplexity_adapts_to_small_n(self):
        # Original hard-coded perplexity=30 raised
        # "perplexity (30) must be less than n_samples (20)".
        X = self._blobs(20, centers=2)
        ns = harness.run_blocks(SLUG, [0, 1, 2, 5], namespace={"X": X}, replace=fast(1, 2, 5))
        self.assertEqual(ns["X_tsne"].shape, (20, 2))

    def test_duplicate_rows_do_not_produce_phantom_k(self):
        # 2 distinct points repeated: KMeans(k>2) silently returns 2 clusters
        # (ConvergenceWarning) and the original loop scored those phantom k's.
        import numpy as np

        X = np.vstack([np.zeros((50, 3)), np.ones((50, 3))])
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")  # expected sklearn ConvergenceWarning
            ns = harness.run_blocks(SLUG, [0, 1], namespace={"X": X}, replace=fast(1))
        self.assertEqual(list(ns["scores"]), [2])

    def test_dbscan_eps_zero_raises_clear_error(self):
        # Original passed eps=0.0 straight to DBSCAN -> sklearn InvalidParameterError
        # with no hint at the cause (duplicate rows).
        import numpy as np

        X = np.vstack([np.zeros((50, 3)), np.ones((50, 3))])
        with self.assertRaisesRegex(ValueError, "deduplicate"):
            harness.run_blocks(SLUG, [0, 3], namespace={"X": X})

    def test_kneighbors_includes_self_matching_dbscan_core_rule(self):
        # The step-4 eps comment claims the last column of kneighbors(X) (self
        # included) is the core-point radius for DBSCAN(min_samples). Run the block
        # verbatim and check its own distances against the fitted DBSCAN's cores.
        import numpy as np

        ns = harness.run_blocks(SLUG, [0, 3], namespace={"X": self._blobs(120)})
        d = ns["distances"]
        self.assertTrue(np.allclose(d[:, 0], 0.0))  # self is neighbor #1
        expected_core = np.flatnonzero(d[:, -1] <= ns["eps_guess"])
        self.assertTrue(np.array_equal(np.sort(ns["dbscan"].core_sample_indices_), expected_core))


if __name__ == "__main__":
    unittest.main()
