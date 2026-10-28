Don't ship it - that ranking is meaningless. A `nan` CV score isn't a low score; it means the fit or the scoring raised in every fold, and `cross_val_score` (default `error_score=np.nan`) turned the exception into the warning you ignored. Sorting a dict that contains nan is then order-dependent, because every comparison with nan is False, so which model comes "first" is arbitrary. Here it handed you the dummy baseline.

Fix the pipeline instead:
1. Make it fail loudly: check `math.isfinite` on every mean score before ranking and raise if any isn't, or rerun with `error_score="raise"` to see the actual exception.
2. Fix the cause. A common one is `scoring="roc_auc"` on a multiclass target (use `roc_auc_ovr`), or a fit error such as unscaled data or a bad parameter.
3. Refuse to ship anything that doesn't beat the dummy baseline, and re-run the comparison once all scores are finite.
