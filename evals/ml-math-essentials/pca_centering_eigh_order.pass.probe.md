Unfortunately both results are misleading, for different reasons.

**SVD route.** PCA is defined on the covariance, i.e. on *mean-centred* data. Running `svd` on raw `X` finds the best rank-1 fit through the origin; since every row has a temperature near 300 K, that offset swamps everything else and \(v_1\) aligns with the temperature axis. Subtract `X.mean(axis=0)` first.

**eigh route.** `np.linalg.eigh` returns eigenvalues sorted from smallest to largest, so `V[:, 0]` is the *least*-variance direction. Temperature, with σ ≈ 0.5, plausibly is the least-variance direction. The first PC is `V[:, -1]`.

Once fixed, both should give the same vector (possibly with opposite sign).
