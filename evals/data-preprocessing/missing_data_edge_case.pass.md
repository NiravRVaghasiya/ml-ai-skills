Not by default. At 85% missing, median imputation means 85% of the rows get the same value, which creates an artificial spike at the median, shrinks the variance, and distorts the feature's relationship with the target. The model then learns mostly from a value you made up.

First ask why the values are missing. If missingness is MCAR, the column may simply carry little information; if it is MNAR (e.g. the field is only filled for a certain kind of customer), the fact that it's missing can itself be predictive.

Options, depending on what you find:
- Drop the column if it is mostly empty and the missingness is uninformative.
- Keep it with a missingness indicator flag (`SimpleImputer(add_indicator=True)`) so the model can use "was missing" as a signal.
- Use a model that handles NaN natively (e.g. HistGradientBoosting) and compare the options with cross-validation.

There is no universal missingness cutoff - validate the choice on your data.
