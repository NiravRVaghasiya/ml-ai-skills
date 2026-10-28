I need a bit more context before recommending anything. Are you predicting a class label or a continuous value? Roughly how many samples, and are the features mostly numeric or categorical? Any constraints on explainability or prediction latency?

Without that, my default assumption for a mid-sized tabular problem would be: fit a linear baseline, then a gradient boosting model, and compare them with cross-validation.
