Not as written. A pruner can only act on intermediate values your objective reports with `trial.report(value, step)` and checks with `trial.should_prune()`. Your objective returns a single final mean, so MedianPruner has nothing to compare and every trial runs to completion - attaching it changes nothing.

Report per fold instead:

```python
def objective(trial):
    params = {...}
    fold_scores = []
    for step, (tr, va) in enumerate(cv.split(X, y)):
        model = make_model(params).fit(X[tr], y[tr])
        fold_scores.append(score(model, X[va], y[va]))
        trial.report(float(np.mean(fold_scores)), step)
        if trial.should_prune():
            raise optuna.TrialPruned()
    return float(np.mean(fold_scores))
```

Also check the warm-up: with 5 folds you report steps 0-4, and `n_warmup_steps=5` means no step is ever eligible, so pruning would stay disabled even after the fix. Use something smaller, such as `n_warmup_steps=1`.
