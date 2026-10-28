Don't ship it - that feature leaks the label. Tickets filed after the scoring date are information from the future: at scoring time in production you can't know them, and a customer who is about to cancel often files tickets while cancelling, so the feature partly encodes the outcome you're predicting. A jump from 0.31 to 0.93 PR-AUC is a red flag, not a win; it will collapse in production.

"More data is better" only holds for data you will actually have when you predict. Fix it by recomputing the feature as of the scoring date, e.g. `support_tickets_30d` = tickets in the window ending at the scoring date, and enforce that with an audit: every feature declares the latest timestamp of the data it was built from, and the pipeline raises if any is after the cutoff or missing.

```python
def audit_feature_asof(feature_cols, feature_asof, cutoff):
    missing = sorted(set(feature_cols) - {f for f, ts in feature_asof.items() if not pd.isna(ts)})
    if missing:
        raise ValueError(f"no as-of timestamp recorded for features: {missing}")
    late = {f: str(ts) for f, ts in feature_asof.items() if ts > cutoff}
    if late:
        raise ValueError(f"features use data from after {cutoff}: {late}")
```

Then re-evaluate against the 0.31 rule baseline; if the honest model still beats it, that's the number to take to launch review.
