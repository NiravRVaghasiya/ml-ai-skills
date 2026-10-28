---
name: recommender-systems
display_name: Recommender Systems (Collaborative & Content-Based Filtering)
description: >
  Use when the user wants to build a recommender system, do collaborative
  filtering, matrix factorization, or content-based item recommendations.
  Trigger phrases: "build a recommender system", "recommend products to
  users", "collaborative filtering model", "matrix factorization for
  recommendations", "content-based recommendation engine". NOT for generic
  supervised classification/regression on tabular data (see
  supervised-learning) or generic feature construction (see
  feature-engineering).
type: workflow
domain: specialized
level: intermediate
lifecycle: stable
risk_level: low
evidence_level: established-practice
last_verified: 2026-10-27
capabilities:
  - user-item-interaction-matrix-construction
  - content-based-tfidf-similarity
  - implicit-feedback-als-collaborative-filtering
  - explicit-rating-svd-collaborative-filtering
  - ranking-metric-evaluation
  - cold-start-hybrid-fallback
requires:
conflicts:
related:
  - supervised-learning
  - feature-engineering
  - model-evaluation
inputs: User-item interaction data (implicit events or explicit ratings) and optional item content metadata.
outputs: A trained content-based, collaborative-filtering, or hybrid recommender plus ranking-metric evaluation (Precision@K, NDCG).
version_constraints:
  - "implicit>=0.7,<0.8: AlternatingLeastSquares(alpha=, random_state=), fit(user_items CSR), recommend(userid, user_items=<that user's CSR row>, N=), evaluation.train_test_split/precision_at_k/ndcg_at_k (executed against implicit 0.7.3 on 2026-10-27)"
  - "scikit-surprise>=1.1,<1.2: Dataset.load_from_df, SVD(random_state=), model_selection.KFold(random_state=) and cross_validate (executed against scikit-surprise 1.1.5 on 2026-10-27)"
---

## Overview
Covers building recommenders from both ends: content-based filtering (item
similarity from features/text) and collaborative filtering via matrix
factorization, for both implicit feedback (clicks/views) and explicit
feedback (ratings). The user walks away with a trained model plus the
ranking metrics (Precision@K, NDCG) needed to judge recommendation quality —
not just prediction accuracy.

## Workflow
1. **Build the user-item interaction matrix.** Keep it sparse — a dense
   users × items array costs memory proportional to users × items (100k × 50k
   float32 ≈ 20 GB) even though almost every cell is empty.
   ```python
   import pandas as pd
   from scipy.sparse import csr_matrix

   # Read ids as strings: one blank in a numeric id column makes pandas parse it as
   # float64, and the ids you map back would come out as 1.0 instead of 1.
   interactions = pd.read_csv("events.csv", dtype={"user_id": str, "item_id": str})  # columns: user_id, item_id, qty
   # .cat.codes maps a missing id to -1, which csr_matrix rejects ("negative axis 0 index").
   interactions = interactions.dropna(subset=["user_id", "item_id", "qty"])
   user_cat = interactions["user_id"].astype("category")
   item_cat = interactions["item_id"].astype("category")
   # Keep the categories: row i is user user_ids[i], column j is item item_ids[j].
   # Every model output below is in this internal index space and must be mapped back.
   user_ids, item_ids = user_cat.cat.categories, item_cat.cat.categories
   user_item = csr_matrix(
       (interactions["qty"], (user_cat.cat.codes, item_cat.cat.codes)),
       shape=(len(user_ids), len(item_ids)),
   )  # repeated (user, item) rows are summed into one cell
   ```
2. **Content-based filtering.** Represent items with TF-IDF over text
   metadata and recommend via cosine similarity — no interaction history
   needed, good for cold-start items.
   ```python
   import numpy as np
   from sklearn.feature_extraction.text import TfidfVectorizer
   from sklearn.metrics.pairwise import cosine_similarity

   items = pd.read_csv("items.csv", dtype={"item_id": str})  # columns: item_id, title, description
   # Catalog dumps often repeat an id; keep one row per item so TF-IDF rows map to items 1:1
   # (step 6's id -> row lookup requires unique ids).
   items = items.drop_duplicates("item_id").reset_index(drop=True)
   # NaN + " " + str is NaN, and TfidfVectorizer rejects NaN documents.
   text = items["title"].fillna("") + " " + items["description"].fillna("")
   tfidf = TfidfVectorizer(stop_words="english", max_features=5000)
   item_vecs = tfidf.fit_transform(text)

   target_idx = 0  # ROW POSITION in `items` of the seed item (not a user_item column)
   sims = cosine_similarity(item_vecs[target_idx], item_vecs).ravel()
   sims[target_idx] = -np.inf  # exclude the seed explicitly: an identical-text twin can tie and outrank it
   top_k = np.argsort(-sims, kind="stable")[: min(10, len(items) - 1)]
   print(items.iloc[top_k]["title"])
   ```
3. **Collaborative filtering on implicit feedback.** Use ALS with
   confidence weighting — treat raw counts as confidence, not preference
   strength. `alpha` and `factors` are dataset-dependent hyperparameters,
   not universal constants — the value below is a Hu/Koren/Volinsky-style
   starting point for confidence scaling; tune it against a held-out
   ranking metric (step 5) rather than trusting a fixed number.
   ```python
   import implicit
   from implicit.evaluation import train_test_split as implicit_split
   from threadpoolctl import threadpool_limits

   # ALS parallelizes itself; implicit warns that a multi-threaded OpenBLAS/MKL
   # "can lead to severe performance issues" and suggests this call. It must run
   # AFTER `import implicit`, or BLAS libraries loaded by that import aren't limited.
   threadpool_limits(1, "blas")

   # Random per-interaction split for brevity — see Gotchas: prefer a time-based split.
   train, test = implicit_split(user_item, train_percentage=0.8, random_state=42)
   model = implicit.als.AlternatingLeastSquares(
       factors=64, regularization=0.05, iterations=20, alpha=15,  # confidence-scaling starting point — tune per dataset
       random_state=42,
   )
   model.fit(train)

   def recommend_for(user: int, n: int = 10) -> list:
       """Top-n unseen item ids for INTERNAL user index `user` (a row of `train`); [] means
       use the step 6 fallback. Serve real user ids through step 6's recommend_for_user_id."""
       seen = train[user]
       if seen.nnz == 0:
           return []  # cold start: ALS learns an all-zero factor, so every score ties at 0
       n = min(n, train.shape[1] - seen.nnz)  # past this, implicit pads with seen items / item 0
       if n == 0:
           return []  # implicit's recommend(N=0) raises IndexError
       item_rows, _scores = model.recommend(userid=user, user_items=seen, N=n)
       return list(item_ids[item_rows])  # internal column indices -> real item ids

   print(recommend_for(0))
   ```
4. **Collaborative filtering on explicit ratings.** Use SVD-style matrix
   factorization when you have real star ratings, not just click counts.
   ```python
   from surprise import Dataset, Reader, SVD
   from surprise.model_selection import KFold, cross_validate

   ratings_df = pd.read_csv("ratings.csv")  # columns: user_id, item_id, rating
   reader = Reader(rating_scale=(1, 5))  # match your data: predict() clips estimates to this range
   data = Dataset.load_from_df(ratings_df[["user_id", "item_id", "rating"]], reader)
   svd = SVD(n_factors=50, n_epochs=20, lr_all=0.005, reg_all=0.02, random_state=42)
   cross_validate(svd, data, measures=["RMSE", "MAE"], cv=KFold(n_splits=5, random_state=42), verbose=True)
   ```
5. **Evaluate with ranking metrics, not just RMSE.** The end product is a
   top-N list, so Precision@K/NDCG reflect real recommendation quality
   better than rating-prediction error.
   ```python
   from implicit.evaluation import precision_at_k, ndcg_at_k

   # implicit's precision_at_k divides hits by min(K, the user's held-out item count),
   # not by K, so it reads higher than textbook P@K for users with few test items —
   # compare models only under the same metric implementation.
   print("Precision@10:", precision_at_k(model, train, test, K=10, show_progress=False))
   print("NDCG@10:", ndcg_at_k(model, train, test, K=10, show_progress=False))

   # Catalog coverage: share of items that appear in anyone's top-10 (see popularity-bias gotcha).
   recommended = {i for u in range(train.shape[0]) for i in recommend_for(u)}
   print("catalog coverage@10:", len(recommended) / len(item_ids))
   ```
6. **Blend collaborative and content-based scores for cold start.** New
   users/items have no collaborative signal — fall back to content-based or
   popularity scores until enough interactions accumulate. Items absent from the
   interaction log aren't columns of `user_item` at all; surface them through
   step 2's item-to-item similarity. `model.item_factors`/`user_factors` below
   are NumPy arrays on implicit's CPU backend.
   ```python
   def minmax(x: np.ndarray) -> np.ndarray:
       span = x.max() - x.min()
       return np.zeros_like(x, dtype=float) if span == 0 else (x - x.min()) / span

   def hybrid_score(cf_score, content_score, cf_weight=0.7):
       # ALS dot products are unbounded while cosine similarity is in [0, 1]; rescale
       # both first, or whichever has the larger spread silently dominates the blend.
       return cf_weight * minmax(cf_score) + (1 - cf_weight) * minmax(content_score)

   # TF-IDF rows follow items.csv order; CF columns follow item_ids. Align to item_ids.
   row_of = pd.Index(items["item_id"]).get_indexer(item_ids)  # -1 = item has no metadata
   popularity = np.asarray(train.sum(axis=0)).ravel()

   def hybrid_recommend(user: int, n: int = 10) -> list:
       liked = train[user].indices
       if len(liked) == 0:
           final_scores = popularity.astype(float)  # in-matrix user with no train rows: no CF/content profile
       else:
           cf_scores = model.item_factors @ model.user_factors[user]
           content_scores = np.zeros(len(item_ids))
           liked_rows = row_of[liked][row_of[liked] >= 0]
           if len(liked_rows):
               profile = np.asarray(item_vecs[liked_rows].mean(axis=0))  # mean TF-IDF of liked items
               content_by_row = cosine_similarity(profile, item_vecs).ravel()
               content_scores[row_of >= 0] = content_by_row[row_of[row_of >= 0]]
           final_scores = hybrid_score(cf_scores, content_scores)
       final_scores[liked] = -np.inf  # don't re-recommend what the user already has
       n = min(n, len(item_ids) - len(liked))
       return list(item_ids[np.argsort(-final_scores, kind="stable")[:n]])

   def recommend_for_user_id(uid: str, n: int = 10) -> list:
       """Serving entry point: real user id in, real item ids out.
       A user absent from the training matrix (a truly new user) has no internal index —
       recommend_for/hybrid_recommend would raise IndexError — so route them to popularity."""
       pos = user_ids.get_indexer([uid])[0]  # -1 = unknown id (ids are strings, see step 1)
       if pos < 0:
           return list(item_ids[np.argsort(-popularity, kind="stable")[:n]])
       return hybrid_recommend(pos, n)

   print(hybrid_recommend(0))
   print(recommend_for_user_id("never-seen-user"))
   ```

## Gotchas
- **Treating implicit counts as explicit ratings.** A click or view count is
  a *confidence* signal, not a preference *magnitude* — feeding raw counts
  into an SVD built for 1-5 star ratings (instead of ALS with confidence
  weighting via `alpha`) produces a biased model.
- **Random train/test splits leak future interactions.** Splitting
  interactions randomly (as step 3 does with implicit's `train_test_split`, for
  brevity) lets a user's later purchases influence recommendations evaluated on
  "earlier" holdout rows — when you have timestamps, split by time so test
  interactions strictly follow train interactions.
- **Popularity bias in top-N evaluation.** A recommender that just returns
  the globally most-popular items scores deceptively well on Precision@K;
  also track catalog coverage / long-tail exposure, not only precision.
- **Cold-start users/items have no factorized embedding.** Matrix
  factorization can't score a user or item with zero training interactions —
  always have a popularity- or content-based fallback path, not a crash or
  a zero-vector default.
- **Raw term counts dominate content similarity.** Using raw bag-of-words
  counts (instead of TF-IDF) for content-based similarity lets common,
  low-information words dominate cosine similarity — prefer an IDF-weighted
  representation (TF-IDF, BM25) or learned embeddings over raw counts.
- **Model indices are not item IDs, and the two index spaces differ.** implicit
  returns column indices of `user_item` (ordered by `item_ids`), while TF-IDF rows
  follow `items.csv` order; serving or blending without the `item_ids` /
  `row_of` mappings from steps 1 and 6 silently recommends the wrong items.

## References
- [Koren, Bell & Volinsky, "Matrix Factorization Techniques for Recommender Systems" (2009)](https://ieeexplore.ieee.org/document/5197422) — the canonical MF-for-recommenders paper.
- [implicit library documentation](https://benfred.github.io/implicit/) — ALS/BPR for implicit feedback plus ranking-metric evaluation utilities.
- [Surprise library documentation](https://surprise.readthedocs.io/) — SVD and other explicit-rating collaborative filtering algorithms.
