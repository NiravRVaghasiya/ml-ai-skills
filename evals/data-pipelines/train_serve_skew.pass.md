Probably not drift. A model that is worse from the first day in production, with features computed by two different code paths, is the textbook signature of train/serve skew: the model was trained on one definition of `orders_30d` / `spend_30d` and is scored on another. Drift usually shows up as a gradual decline after a period of good performance.

Likely sources of skew in your setup:
- Different sources: the orders table vs the request log (do cancelled/refunded orders, retries, or bot traffic appear in one and not the other?).
- Different windows: the SQL job's 30 days end at midnight UTC of the run; the API's "last 30 days" ends now, possibly in another timezone.
- Different null/zero handling for customers with no orders.

Diagnose before changing anything: log the feature values the API actually served (with customer id and request time), then compare them with the offline values for the same customers and dates. Any systematic difference is your answer.

Fix it structurally: define the features once and serve the same values to both paths - e.g. a Feast `FeatureView` fed by the Airflow job, `feast materialize-incremental` into the online store, and `store.get_online_features(...)` in the API instead of recomputing in Python. Build training sets with point-in-time joins from the same source.

Retraining weekly would not help: the new model is still trained on the offline definition and scored on the online one.
