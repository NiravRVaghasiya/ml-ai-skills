Not on the strength of that test. A paired t-test over CV folds assumes the 10 differences are independent, and they aren't: any two training sets in 10-fold CV share about 8/9 of their data, and all test folds come from the same sample. The fold-to-fold variance therefore underestimates the real variability, so the naive test is anti-conservative - it produces p-values that are too small and finds "significant" differences too often (Dietterich, 1998; Nadeau & Bengio, 2003).

Better options:
- Nadeau-Bengio corrected resampled t-test: replace the 1/k variance factor with 1/k + n_test/n_train. For your numbers that factor is (1/10 + 1/9) / (1/10), about 2.1, which turns t of about 3.25 (p = 0.01, 9 df) into t of about 2.24, p of about 0.05 - borderline at best. It works best with repeated CV (e.g. 10x10).
- The 5x2cv paired t-test (Dietterich) as an alternative.
- With a held-out test set: a paired bootstrap over test examples for the AUC difference, or DeLong's test for two correlated AUCs. (McNemar's test fits a thresholded metric such as accuracy - it compares the paired correct/incorrect predictions - not AUC.)

Also look at the effect itself: 0.006 AUC. Report it with a confidence interval and decide whether a difference that size matters for the application. If A was picked as the best of several candidates, account for that multiple comparison too.
