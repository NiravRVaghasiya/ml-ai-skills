That makes the reported number optimistic. Once you pick between raw and calibrated by looking at test-set Brier, the test set has been used for model selection; reporting the winner's score on the same data is biased (slightly, for one binary choice, but it compounds as you add more choices like method or bins), and you no longer have an untouched estimate.

Instead:
1. Decide on training data only: compare out-of-fold Brier scores from `cross_val_predict(..., method="predict_proba")` for the raw and the calibrated model on the training split (or use a separate validation/calibration split).
2. Fit the chosen model, then evaluate it once on the test set and report that number - no more decisions after that.

Also make sure the calibrator never sees predictions on rows the base model trained on: `CalibratedClassifierCV(model, cv=5)` handles that by fitting per fold, or wrap an already-fitted model in `FrozenEstimator` and calibrate on a separate split. With small calibration folds (well under ~1,000 samples), prefer `method="sigmoid"`; isotonic tends to overfit there.
