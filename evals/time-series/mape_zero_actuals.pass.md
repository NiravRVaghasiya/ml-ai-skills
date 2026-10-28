Before changing models, look at the metric. MAPE divides each error by the actual value, and daily store sales almost always have zero-sales days (closures, holidays). scikit-learn's `mean_absolute_percentage_error` divides by a tiny epsilon there, so a few zero days blow the score up to numbers like 1e15. That is why the seasonal-naive baseline looks just as absurd: the metric is the problem, not necessarily either model.

Instead:
- Score each walk-forward fold with MAE (or RMSE), and add a scale-free view such as MASE or the ratio of your model's MAE to the seasonal-naive baseline's MAE. WAPE (total absolute error over total sales) is an option when totals are well away from zero.
- Compare the model with the seasonal-naive baseline on that metric. If it beats the baseline consistently across folds, it isn't broken; if it doesn't, then look at features and model choice.

Only consider a deep learning model after the metric says the current one is genuinely worse than the baseline.
