No - split first. If you fit the scaler on the combined data, the test rows' mean and standard deviation (or min/max) are baked into the transformation applied to the training data. That is data leakage: information from the test set influences training, so your test score is optimistically biased and no longer an honest estimate of performance on unseen data. The same applies to imputers (test medians) and encoders (test-only categories).

Do it in this order: `train_test_split` first, then fit every transformation on the training split only and apply the fitted transformer to the test split. A scikit-learn `Pipeline` / `ColumnTransformer` enforces this automatically, including inside cross-validation:

```python
pipe = Pipeline([("scale", StandardScaler()), ("model", LogisticRegression())])
pipe.fit(X_train, y_train)      # scaler statistics come from X_train only
pipe.score(X_test, y_test)
```
