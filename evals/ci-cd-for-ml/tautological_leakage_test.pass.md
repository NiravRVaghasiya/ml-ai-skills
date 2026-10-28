That test can't fail, so its passing tells you nothing. It's a tautology: you build `feature_cols` by removing "churned" and then assert "churned" isn't in it. Worse, it checks a list the test made up rather than the columns the pipeline actually trains on, so a copy of the target under another name (say `churn_flag_copy`), or a post-outcome proxy such as a cancellation date, sails straight through.

A test that can catch leakage:
- Import the pipeline's real `FEATURE_COLUMNS` and assert none of them is in a forbidden list (the target, its known copies, post-outcome fields).
- Add a behavioral check: no single numeric feature should separate the target almost perfectly on its own, e.g. single-feature ROC AUC (or 1 - AUC) below a threshold like 0.95.
- Prove it can fail: run it against a deliberately leaky fixture variant of the pipeline and make sure it goes red.
