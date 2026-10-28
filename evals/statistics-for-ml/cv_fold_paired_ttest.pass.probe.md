I wouldn't report it as significant yet.

The paired t-test treats the 10 fold-level differences as independent samples. In k-fold CV they're not: the training sets of any two folds overlap by (k−2)/(k−1) ≈ 89%, which induces positive correlation between fold scores. The test then underestimates the variance of the mean difference, so its p-value is too optimistic (Dietterich 1998).

Options:
- Nadeau & Bengio's corrected resampled t-test (variance multiplied by 1/k + n_test/n_train instead of 1/k).
- Dietterich's 5×2cv t-test.
- On a fixed test set, DeLong's test for AUCs or a paired bootstrap.

And a 0.006 AUC lift is small; give a CI and ask whether it matters.
