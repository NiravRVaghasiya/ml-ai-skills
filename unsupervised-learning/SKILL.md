---
name: unsupervised-learning
display_name: Unsupervised Learning
description: >
  Use when the user wants to cluster unlabeled data or reduce dimensionality.
  Trigger phrases: "cluster these customers", "find groups in this data", "how
  many clusters", "reduce dimensions for visualization", "run PCA", "segment my
  users". NOT for feature creation/selection (see feature-engineering) or
  supervised model selection (see supervised-learning).
type: workflow
domain: classical-ml
level: intermediate
lifecycle: stable
risk_level: low
evidence_level: established-practice
last_verified: 2026-10-27
capabilities:
  - kmeans-clustering
  - cluster-count-selection
  - density-based-clustering
  - dimensionality-reduction-pca
  - tsne-visualization
requires:
conflicts:
related:
  - feature-engineering
  - data-preprocessing
  - model-evaluation
  - supervised-learning
inputs: A scaled numeric feature matrix with no target labels.
outputs: Cluster assignments or a reduced-dimension embedding, plus diagnostics (silhouette score, explained variance) justifying the choice.
version_constraints:
  - "scikit-learn>=1.2: KMeans(n_init=10), silhouette_score(sample_size=, random_state=), DBSCAN, NearestNeighbors, PCA(n_components=0.95), TSNE(perplexity=) (executed against scikit-learn 1.9.0 on 2026-10-27; lower bound not verified)"
---

## Overview
Covers the two core unsupervised tasks on tabular data: clustering (finding groups
with no labels) and dimensionality reduction (compressing features while
preserving structure, for visualization or as input to a downstream model). Both
are distance-based, so scaling is non-negotiable. Walks through choosing a cluster
count defensibly, fitting the standard algorithms, and reducing dimensions with PCA
before reaching for non-linear visualization tools like t-SNE/UMAP.

## Workflow
1. **Scale first — every algorithm here is distance-based.** An unscaled feature
   with a larger numeric range silently dominates the distance metric.
   ```python
   from sklearn.preprocessing import StandardScaler

   scaler = StandardScaler()
   X_scaled = scaler.fit_transform(X)
   ```
2. **Choose a cluster count with silhouette score, not inertia alone.** Inertia
   (within-cluster sum of squares) always decreases as k grows, so its "elbow" is
   often ambiguous — silhouette score has a maximum you can read off (though,
   like KMeans, it favors convex clusters).
   ```python
   import numpy as np
   from sklearn.cluster import KMeans
   from sklearn.metrics import silhouette_score

   n = X_scaled.shape[0]
   scores = {}
   # silhouette_score needs 2 <= n_labels <= n_samples - 1, so cap k below n.
   for k in range(2, min(10, n)):
       km = KMeans(n_clusters=k, n_init=10, random_state=42).fit(X_scaled)
       if len(np.unique(km.labels_)) < k:   # duplicate rows -> fewer real clusters than k
           continue
       # Silhouette is O(n^2) in time; sample_size bounds the cost on large data.
       scores[k] = silhouette_score(X_scaled, km.labels_,
                                    sample_size=min(n, 10_000), random_state=42)
   if not scores:
       raise ValueError("no k produced a valid clustering — too few distinct rows")
   best_k = max(scores, key=scores.get)
   print(scores, "-> best k:", best_k)
   ```
3. **Fit KMeans with the chosen k and inspect cluster sizes.** A "winning" k that
   produces one giant cluster and several singletons is usually not useful —
   sanity-check the split.
   ```python
   kmeans = KMeans(n_clusters=best_k, n_init=10, random_state=42)
   labels = kmeans.fit_predict(X_scaled)
   print(np.bincount(labels))
   ```
4. **Try density-based clustering when clusters aren't spherical.** KMeans assumes
   convex, similarly-sized clusters; `DBSCAN` finds arbitrary shapes and flags
   outliers as noise (`label == -1`) instead of forcing them into a cluster.
   ```python
   import numpy as np
   from sklearn.cluster import DBSCAN
   from sklearn.neighbors import NearestNeighbors

   min_samples = 5
   # Rough eps heuristic: a high percentile of each point's distance to its
   # min_samples-th neighbor (the query point counts as its own first neighbor,
   # matching DBSCAN's core-point rule). Plot the sorted curve to refine.
   neighbors = NearestNeighbors(n_neighbors=min_samples).fit(X_scaled)
   distances, _ = neighbors.kneighbors(X_scaled)
   eps_guess = np.percentile(distances[:, -1], 90)
   if eps_guess <= 0:   # DBSCAN rejects eps=0; happens when most rows are duplicates
       raise ValueError("k-distance is 0 — deduplicate rows (or pass sample_weight) first")

   dbscan = DBSCAN(eps=eps_guess, min_samples=min_samples)
   db_labels = dbscan.fit_predict(X_scaled)
   print("clusters:", len(set(db_labels)) - (1 if -1 in db_labels else 0),
         "noise points:", (db_labels == -1).sum())
   ```
5. **Reduce dimensionality with PCA, choosing components by explained variance.**
   Use this for compression or as a preprocessing step before a downstream model —
   PCA components preserve global linear structure and Euclidean distances are
   still meaningful in the reduced space. When the output feeds a supervised
   model, put the scaler and PCA inside that model's `Pipeline` so they are fit
   on training folds only (see `data-preprocessing`).
   ```python
   from sklearn.decomposition import PCA

   pca = PCA(n_components=0.95, random_state=42)   # keep 95% of variance
   X_pca = pca.fit_transform(X_scaled)
   print("components kept:", pca.n_components_, "variance explained:",
         pca.explained_variance_ratio_.sum())
   ```
6. **Use t-SNE/UMAP only for 2D visualization, never as model input.** These
   methods distort global distances by design — great for eyeballing cluster
   separation, unsafe as features for a downstream estimator.
   ```python
   from sklearn.manifold import TSNE

   # perplexity must be < n_samples. For very wide data, sklearn recommends
   # reducing to ~50 dims with PCA first (faster and less noisy).
   perplexity = min(30, X_scaled.shape[0] - 1)
   X_tsne = TSNE(n_components=2, perplexity=perplexity, random_state=42).fit_transform(X_scaled)
   # plot X_tsne[:, 0] vs X_tsne[:, 1], colored by `labels` from step 3, purely for inspection
   ```

## Gotchas
- **Forgetting to scale before distance-based clustering or PCA.** A single
  unscaled large-range column (e.g., income in dollars next to age in years) will
  dominate `KMeans`/`DBSCAN` distances and PCA's principal axes.
- **Picking k from the inertia "elbow" alone.** It's subjective and inertia keeps
  dropping as k grows; corroborate with `silhouette_score` (or `calinski_harabasz_score`)
  before committing to a k.
- **Treating t-SNE/UMAP distances as meaningful.** Cluster sizes, inter-cluster
  distances, and even density in a t-SNE plot are visualization artifacts, not
  ground truth — never feed t-SNE/UMAP output into a supervised model or use it to
  claim "cluster A is twice as far from B as from C."
- **KMeans on non-spherical or very different-sized clusters.** It assumes
  roughly equal-variance, convex clusters and will slice an elongated or crescent-
  shaped group in half. Check the assumption or switch to `DBSCAN`/Gaussian
  mixtures.
- **The curse of dimensionality.** In high-dimensional raw feature spaces,
  pairwise distances tend to concentrate (nearest and farthest neighbors look
  alike) unless the data lies near a low-dimensional structure, which makes
  clustering and kNN-style methods less reliable — as a rule of thumb, with
  hundreds of columns consider PCA (or feature selection, see
  `feature-engineering`) first.
- **Silhouette on large data or degenerate inputs.** Full `silhouette_score` is
  quadratic in the number of rows (pass `sample_size`), and it raises when the
  clustering has fewer than 2 or more than `n_samples - 1` labels — which
  happens when k approaches n or duplicate rows make KMeans return fewer
  distinct clusters than requested (it emits a `ConvergenceWarning`).

## References
- [scikit-learn: Clustering](https://scikit-learn.org/stable/modules/clustering.html) — algorithm comparison table and assumptions per method.
- [scikit-learn: Decomposing signals (PCA)](https://scikit-learn.org/stable/modules/decomposition.html#pca) — component selection and explained-variance API.
- [scikit-learn: Manifold learning (t-SNE)](https://scikit-learn.org/stable/modules/manifold.html#t-distributed-stochastic-neighbor-embedding-t-sne) — explicit caveats on distance interpretation.
