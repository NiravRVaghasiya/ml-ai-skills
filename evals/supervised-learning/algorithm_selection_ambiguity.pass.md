It depends on a few things I don't know yet - could you tell me:
1. Is the target a category (classification) or a number (regression)?
2. How many rows and features do you have?
3. What kinds of features - numeric, categorical, text, images?
4. Do you need an interpretable model, and are there latency or inference-cost limits?
5. For classification, how imbalanced are the classes?

If you'd rather I assume: for a typical tabular dataset with a few thousand rows, start with a simple baseline (logistic/linear regression), then compare a gradient-boosted tree model (e.g. HistGradientBoosting or LightGBM) with cross-validation, and keep the simpler model unless the gain is meaningful.
