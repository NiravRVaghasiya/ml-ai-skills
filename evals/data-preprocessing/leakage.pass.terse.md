Short answer: don't. Scaling before the split means the scaler's statistics (mean/std) are computed partly from rows that will end up in your test set, so the test set is no longer unseen — that's leakage, and it makes your evaluation look better than it should.

Correct order:
1. Split into train and test.
2. Call `.fit()` on the training portion only.
3. `.transform()` both portions with that fitted scaler.

Wrapping the scaler and model in a single sklearn Pipeline makes this automatic and also keeps cross-validation folds clean.
