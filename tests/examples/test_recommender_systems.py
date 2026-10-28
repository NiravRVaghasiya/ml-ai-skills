"""Executes recommender-systems/SKILL.md's python blocks against tiny synthetic CSVs.

Nothing is stubbed: the blocks read ``events.csv`` / ``items.csv`` /
``ratings.csv`` from the working directory, so the test writes small
synthetic versions of those files into a temp dir (the only substitution is
the data itself). implicit (ALS) and scikit-surprise (SVD) run for real.

The fixture deliberately contains the boundary cases found in the audit:
a row with a missing user_id, an item with a missing description, two items
with identical text, items.csv rows in a different order from the CF column
order (plus an item with no metadata and one never interacted with), and a
heavy user who has seen almost every item, plus a duplicated item_id row in
items.csv (common in catalog dumps).

Pre-fix, the skill's blocks could not run at all (``target_idx``,
``ratings_df``, ``cf_scores``/``content_scores`` undefined) and, once those
were supplied, failed on the boundary data: ``csr_matrix`` raised
"negative axis 0 index: -1" for the missing id, TfidfVectorizer raised
"np.nan is an invalid document", ``argsort()[::-1][1:]`` kept the seed item
when a duplicate tied with it, and ``recommend(N=10)`` for the heavy user
returned already-seen items / padded id 0 (see the library-behaviour tests).

Second review round: a real user id absent from ``user_ids`` (true cold start)
made recommend_for/hybrid_recommend raise IndexError (no id-level entry
point); a duplicated item_id in items.csv made ``get_indexer`` raise
InvalidIndexError; numeric id columns with a blank were read as float64 so
ids mapped back as ``1.0`` instead of ``1``.
"""
from __future__ import annotations

import contextlib
import io
import tempfile
import unittest
import warnings
from pathlib import Path

from examples import harness

SLUG = "recommender-systems"
N_USERS, N_ITEMS = 30, 20


def _write_fixture(tmp: Path) -> None:
    import numpy as np
    import pandas as pd

    rng = np.random.default_rng(0)
    rows = []
    for u in range(N_USERS):
        n = 18 if u == 0 else int(rng.integers(3, 8))  # u00 is the heavy user
        for i in rng.choice(N_ITEMS, size=n, replace=False):
            rows.append({"user_id": f"u{u:02d}", "item_id": f"i{i:02d}", "qty": int(rng.integers(1, 4))})
    rows.append({"user_id": None, "item_id": "i00", "qty": 1})  # missing id -> must be dropped
    rows.append({"user_id": "u01", "item_id": "i01", "qty": 2})  # duplicate pair -> summed
    pd.DataFrame(rows).to_csv(tmp / "events.csv", index=False)

    words = ["red", "blue", "green", "shoe", "shirt", "hat", "wool", "cotton", "summer", "winter"]
    items = []
    for i in range(N_ITEMS - 1):  # i19 has no metadata row
        a, b, c = rng.choice(words, size=3, replace=False)
        items.append({"item_id": f"i{i:02d}", "title": f"{a} {b}", "description": f"{c} {a} item"})
    items[5]["description"] = None                    # missing text
    items[1] = {**items[0], "item_id": "i01"}         # identical text to i00
    items.append({"item_id": "i99", "title": "new hat", "description": "never interacted with"})
    items.append({**items[3], "title": "re-exported copy"})  # duplicated item_id i03
    df = pd.DataFrame(items).iloc[::-1]               # rows NOT in item_id order
    # put i00 first so the skill's target_idx = 0 is i00, whose twin is i01 (rows tie)
    df = pd.concat([df[df.item_id == "i00"], df[df.item_id != "i00"]])
    df.to_csv(tmp / "items.csv", index=False)

    ratings = pd.DataFrame({"user_id": rng.integers(0, 20, 200), "item_id": rng.integers(0, 15, 200),
                            "rating": rng.integers(1, 6, 200)})
    ratings.to_csv(tmp / "ratings.csv", index=False)


@harness.requires("numpy", "pandas", "scipy", "sklearn", "implicit", "surprise", "threadpoolctl")
class RecommenderSystemsExampleTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory()
        tmp = Path(cls._tmp.name)
        _write_fixture(tmp)
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()), \
                warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            cls.ns = harness.run_blocks(SLUG, workdir=tmp)
        cls.stdout = out.getvalue()
        cls.warnings = [str(w.message) for w in caught]

    @classmethod
    def tearDownClass(cls):
        cls._tmp.cleanup()

    def test_interaction_matrix(self):
        ns = self.ns
        ui = ns["user_item"]
        self.assertEqual(ui.shape, (N_USERS, N_ITEMS))
        self.assertEqual((len(ns["user_ids"]), len(ns["item_ids"])), ui.shape)
        u01, i01 = ns["user_ids"].get_loc("u01"), ns["item_ids"].get_loc("i01")
        self.assertGreaterEqual(ui[u01, i01], 2)  # duplicate rows summed, not overwritten

    def test_content_neighbours_exclude_seed_even_with_identical_twin(self):
        ns = self.ns
        top = list(ns["items"].iloc[ns["top_k"]]["item_id"])
        self.assertNotIn("i00", top)  # pre-fix argsort()[::-1][1:] kept the seed and dropped its twin
        self.assertEqual(top[0], "i01")
        self.assertEqual(len(top), 10)

    def test_implicit_blas_warning_suppressed(self):
        self.assertFalse([w for w in self.warnings if "OpenBLAS is configured" in w], self.warnings)

    def test_recommend_for_heavy_user_never_returns_seen_or_padding(self):
        ns = self.ns
        seen = ns["train"][0]
        self.assertLess(N_ITEMS - seen.nnz, 10, "fixture must exercise the N > candidates boundary")
        recs = ns["recommend_for"](0)
        self.assertEqual(len(recs), min(10, N_ITEMS - seen.nnz))
        self.assertEqual(len(set(recs)), len(recs))
        self.assertFalse(set(recs) & set(ns["item_ids"][seen.indices]))

    def test_recommend_for_cold_and_saturated_users(self):
        ns = self.ns
        train = ns["train"]
        try:
            lil = train.tolil()
            lil[3, :] = 0                       # user 3 -> cold start
            lil[4, :] = 1                       # user 4 has seen every item
            modified = lil.tocsr()
            modified.eliminate_zeros()
            ns["train"] = modified
            self.assertEqual(ns["recommend_for"](3), [])
            self.assertEqual(ns["recommend_for"](4), [])  # pre-fix: N=0 -> IndexError in implicit topk
            self.assertEqual(ns["hybrid_recommend"](4), [])
            pop = ns["hybrid_recommend"](3)
            self.assertEqual(len(pop), 10)
            popularity = ns["popularity"]
            top_pop = sorted(range(N_ITEMS), key=lambda j: -popularity[j])[:10]
            self.assertEqual(sorted(pop), sorted(ns["item_ids"][top_pop]))
        finally:
            ns["train"] = train

    def test_hybrid_recommend(self):
        ns = self.ns
        recs = ns["hybrid_recommend"](1)
        self.assertEqual(len(recs), 10)
        self.assertFalse(set(recs) & set(ns["item_ids"][ns["train"][1].indices]))
        self.assertTrue(set(recs) <= set(ns["item_ids"]))
        self.assertNotIn("i99", ns["item_ids"])  # never-interacted items aren't CF columns at all
        self.assertEqual(int((ns["row_of"] < 0).sum()), 1)  # i19 has no metadata row

    def test_unknown_real_user_id_gets_popularity_fallback(self):
        # Pre-fix: no id-level entry point; index len(user_ids) -> IndexError, get_loc -> KeyError.
        ns = self.ns
        self.assertNotIn("brand_new_user", ns["user_ids"])
        recs = ns["recommend_for_user_id"]("brand_new_user")
        popularity = ns["popularity"]
        top_pop = sorted(range(N_ITEMS), key=lambda j: -popularity[j])[:10]
        self.assertEqual(sorted(recs), sorted(ns["item_ids"][top_pop]))
        self.assertEqual(ns["recommend_for_user_id"]("u01"), ns["hybrid_recommend"](ns["user_ids"].get_loc("u01")))

    def test_duplicated_item_rows_are_dropped_and_rows_stay_aligned(self):
        # Pre-fix: pd.Index(items["item_id"]).get_indexer raised InvalidIndexError.
        ns = self.ns
        self.assertTrue(ns["items"]["item_id"].is_unique)
        self.assertEqual(ns["item_vecs"].shape[0], len(ns["items"]))
        row_of = ns["row_of"]
        for j, iid in enumerate(ns["item_ids"]):
            if row_of[j] >= 0:
                self.assertEqual(ns["items"].iloc[row_of[j]]["item_id"], iid)

    def test_numeric_ids_with_blanks_keep_original_form(self):
        # Pre-fix: a blank in a numeric id column made pandas read float64 -> ids 1.0, 2.0.
        import pandas as pd
        with tempfile.TemporaryDirectory() as d:
            Path(d, "events.csv").write_text("user_id,item_id,qty\n1,10,1\n,11,1\n2,11,3\n", encoding="utf-8")
            ns = harness.run_blocks(SLUG, [0], workdir=Path(d))
        self.assertEqual(list(ns["user_ids"]), ["1", "2"])
        self.assertEqual(list(ns["item_ids"]), ["10", "11"])
        self.assertIsInstance(ns["user_ids"], pd.Index)

    def test_minmax_constant_input(self):
        import numpy as np
        self.assertTrue(np.array_equal(self.ns["minmax"](np.array([3.0, 3.0])), np.zeros(2)))

    def test_surprise_cross_validation_ran(self):
        self.assertIn("RMSE (testset)", self.stdout)
        self.assertIn("Precision@10:", self.stdout)
        self.assertIn("catalog coverage@10:", self.stdout)

    # --- library behaviour the skill's guidance relies on -------------------
    def test_implicit_precision_denominator_is_min_k_and_test_size(self):
        from implicit.evaluation import precision_at_k
        ns = self.ns
        model, train, test = ns["model"], ns["train"], ns["test"]
        hits = denom = 0
        for u in range(train.shape[0]):
            held_out = set(test[u].indices)
            if not held_out or train[u].nnz == 0:
                continue
            n = min(10, N_ITEMS - train[u].nnz)
            rec, _ = model.recommend(u, train[u], N=n)
            hits += len(set(rec) & held_out)
            denom += min(10, len(held_out))
        self.assertAlmostEqual(precision_at_k(model, train, test, K=10, show_progress=False), hits / denom, places=6)

    def test_implicit_recommend_pads_when_n_exceeds_candidates(self):
        import numpy as np
        ns = self.ns
        seen = ns["train"][0]
        ids, scores = ns["model"].recommend(0, seen, N=N_ITEMS + 5)
        self.assertEqual(len(ids), N_ITEMS + 5)
        tail = ids[N_ITEMS - seen.nnz:]
        # beyond the unseen candidates come already-seen items (score ~ -FLT_MAX) and id-0 padding
        self.assertTrue(set(tail) & set(seen.indices) or (tail == 0).any())
        self.assertTrue((scores[N_ITEMS - seen.nnz:] <= 0).all())
        self.assertTrue(np.isfinite(scores).all())

    def test_implicit_cold_user_gets_zero_factors(self):
        import implicit
        import numpy as np
        lil = self.ns["user_item"].tolil()
        lil[2, :] = 0
        m = lil.tocsr()
        m.eliminate_zeros()
        model = implicit.als.AlternatingLeastSquares(factors=8, iterations=3, random_state=0)
        with contextlib.redirect_stderr(io.StringIO()):
            model.fit(m, show_progress=False)
        self.assertTrue(np.allclose(model.user_factors[2], 0))


if __name__ == "__main__":
    unittest.main()
