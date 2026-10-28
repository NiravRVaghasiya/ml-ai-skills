Before picking XGBoost or anything else, the problem needs to be framed - "use ML to reduce churn" is not yet a spec, and the model is the last thing to decide. A few questions to answer first:

1. **Decision.** What action changes based on the prediction? For example: "each month, the retention team calls the 500 highest-risk customers." That determines what a useful prediction is.
2. **Unit, horizon and label.** Predict per customer, as of a scoring date, whether they cancel within the next 30 days (or whatever window gives the team time to act). A bare `churned` yes/no column has no date, so you can't tell what was known before the outcome.
3. **Metric.** Accuracy is misleading here: if 5% of customers churn, a model that says "nobody churns" is 95% accurate. Pick a metric tied to the decision - PR-AUC, or precision/recall in the top 500 you can actually contact - plus the business metric (churn rate among treated customers).
4. **Baseline.** Score the existing rule (e.g. "3+ support tickets in 30 days") and a prior-class dummy model on the same split. A model has to beat those to be worth building.
5. **Leakage.** Every feature must be computed from data available before the scoring date; anything recorded after (cancellation reason, final invoice) leaks the answer.

Once that's written down, XGBoost may well be a reasonable model to try - but we can't say which model is best until we know what "best" is measured against.
