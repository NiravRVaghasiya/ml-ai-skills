No - this is a silent broadcasting bug. Numpy compares shapes from the trailing dimension: `(1000,)` against `(1000, 1)` means 1000 vs 1 (compatible, the 1 is stretched) and then a missing dimension vs 1000. So `y_true - y_pred` has shape `(1000, 1000)`: every true value minus every prediction, i.e. all pairwise differences rather than 1000 residuals. Your mean is taken over a million of those, which is why it looks high, and numpy raises no error because the shapes are legal.

Fix the shapes before subtracting:

```python
y_pred = model.predict(X).ravel()          # or .reshape(-1) / [:, 0]
assert y_true.shape == y_pred.shape, (y_true.shape, y_pred.shape)
mse = np.mean((y_true - y_pred) ** 2)
```

or use `sklearn.metrics.mean_squared_error(y_true, y_pred)`, which validates the inputs. In general, check `.shape` after any broadcasted operation where `(n,)`, `(n, 1)` and `(1, n)` might meet.
