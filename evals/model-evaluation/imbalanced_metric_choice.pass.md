Not on accuracy alone. Fraud is rare, so the classes are heavily imbalanced: if only 0.6% of transactions are fraud, a model that always predicts "not fraud" scores 99.4% accuracy while catching nothing. First compare against that base-rate baseline (e.g. a DummyClassifier).

Then evaluate with metrics that focus on the minority class: precision and recall for fraud, PR-AUC (average precision), and the confusion matrix at your chosen threshold - or recall at a fixed false-positive budget, since analysts can only review so many alerts.

Finally, tie the threshold to business cost: a missed fraud (false negative) costs the chargeback amount, a false positive costs review time and customer friction. Pick the operating point that minimizes expected cost, and ship only if that beats the current process.
