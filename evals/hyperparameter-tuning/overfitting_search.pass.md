That will give you an optimistically biased number. Each time you pick hyperparameters by their test score, information from the test set leaks into your choices; after many trials the test set has effectively become a validation set, and the best score partly reflects luck on those specific rows rather than generalization.

Instead:
1. Run the search with cross-validation on the training data (e.g. GridSearchCV / RandomizedSearchCV / Optuna with CV), or with a separate validation split.
2. Keep the test set untouched until the very end, then evaluate the single chosen model on it once and report that number.
3. If data is limited, use nested cross-validation: an inner CV loop tunes hyperparameters, an outer loop estimates the performance of the whole tuning procedure.
