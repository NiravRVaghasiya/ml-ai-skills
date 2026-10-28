An accuracy drop tells you performance degraded, not why - and "data drift" is only one possible cause:
- Data drift (covariate shift): the input feature distribution moved.
- Concept drift: the relationship between inputs and the true label changed, so the same inputs now mean something different.
- Prediction drift: the model's output distribution shifted (often a symptom of one of the above).
- A pipeline bug: a broken upstream feature, a schema change, or training-serving skew can look exactly like drift - and retraining on last week's (equally broken) data won't fix it.

Diagnose before retraining: compare feature distributions against the training reference (PSI/KS per feature), check the prediction distribution, validate the feature pipeline and data quality, and once delayed labels arrive, check whether the input-label relationship changed. Retrain only when the cause is real drift that recent data captures.
