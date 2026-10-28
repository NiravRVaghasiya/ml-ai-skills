I wouldn't. One-hot encoding 9,000 ZIP codes adds ~9,000 mostly-zero columns: a very sparse, high-dimensional matrix, many categories with only a handful of rows (noisy and overfit-prone), and no useful representation for ZIP codes that first appear at prediction time.

Better options, depending on your model and data:
- Target (mean) encoding - e.g. scikit-learn's `TargetEncoder` - which replaces each ZIP with a smoothed target statistic. It leaks the target if you fit it on the same rows you train on, so it must be cross-fitted (out-of-fold) on the training folds only; `TargetEncoder.fit_transform` does this internally.
- Frequency/count encoding, or grouping rare ZIPs into an "other" bucket.
- Coarsening to a higher geographic level (3-digit ZIP, region) or joining ZIP-level features such as population density.
- Feature hashing, or learned embeddings if you're training a neural network.
