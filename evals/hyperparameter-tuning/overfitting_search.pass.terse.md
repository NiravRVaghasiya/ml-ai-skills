Avoid that — the number you report would be inflated. Selecting the configuration with the best test score is itself fitting to the test set, so the reported score is biased upward.

Tune with k-fold cross-validation on the training split (or a dedicated validation set), pick the configuration, refit, and only then evaluate once on the test set. With small datasets, nested CV gives an honest estimate of the entire tuning process.
