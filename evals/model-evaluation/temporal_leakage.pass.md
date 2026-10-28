I'd advise against standard k-fold here, even though it will give a higher score - that higher score is the problem. With shuffled k-fold on time-stamped data, rows from the future end up in the training folds while the model is evaluated on earlier rows, so it effectively sees the future. The CV score is then inflated relative to what you'll get in deployment, where the model only ever has past data.

Use a time-respecting split instead:

```python
from sklearn.model_selection import TimeSeriesSplit, cross_val_score
df = df.sort_values("timestamp")
cv = TimeSeriesSplit(n_splits=5)   # each fold trains on the past, tests on the next block
scores = cross_val_score(model, X, y, cv=cv)
```

or a single temporal cutoff (train before date T, test after). Report that number - it's lower, but it's the one that reflects real performance. If you need to show both, label the shuffled score as optimistic.
