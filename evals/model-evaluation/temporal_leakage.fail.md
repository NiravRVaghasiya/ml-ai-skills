Sure! Here's standard 5-fold cross-validation, which will give you a stable, high score for your report:

```python
from sklearn.model_selection import KFold, cross_val_score
cv = KFold(n_splits=5, shuffle=True, random_state=42)
scores = cross_val_score(model, X, y, cv=cv)
print(scores.mean())
```

Shuffling ensures each fold has a representative mix of rows, so the timestamp column won't bias the result.
