No - the two methods agree for two different wrong reasons, and temperature is probably your *least* important direction.

1. SVD without centering is not PCA. PCA looks for directions of maximum variance around the mean. np.linalg.svd(X) on raw X finds directions of maximum second moment around the origin, and a column whose values all sit near 300 contributes about 300^2 per row to that, so Vt[0] just points at the temperature column's mean offset. Center the columns first:

```python
Xc = X - X.mean(axis=0)
U, S, Vt = np.linalg.svd(Xc, full_matrices=False)   # full_matrices=False avoids an n x n U
pc1 = Vt[0]
explained_var = S**2 / (len(X) - 1)
```

2. eigh sorts eigenvalues in ascending order. np.cov already centers the data, so that route was nearly right, but V[:, 0] is the eigenvector with the *smallest* eigenvalue. With a standard deviation of about 0.5 (variance 0.25), temperature is likely the lowest-variance direction, which is exactly why it shows up there. The top component is V[:, -1] (or reorder with idx = np.argsort(w)[::-1]).

After both fixes, Vt[0] from the centered SVD and V[:, -1] from eigh match up to sign (eigenvectors are only defined up to a factor of -1), and S**2 / (n - 1) equals the eigenvalues in reverse order. If the units aren't comparable you may also want to standardize each column first (PCA on the correlation matrix) - that's a modeling choice, not a bug fix.
