I wouldn't report it as is. For a query with no gold labels, recall and reciprocal rank are undefined, not zero - there is nothing to retrieve or rank against. Counting them as 0 deflates the metric in proportion to how many queries are unlabeled: if the 180 unlabeled queries contributed 0, your 0.29 corresponds to about 0.29 x 300 / 120 = 0.725 over the 120 labeled ones. What you'd be reporting is label coverage, not retrieval quality.

Instead:
- Return NaN for unlabeled queries and use a mean that skips NaN.
- Report the metric over the labeled subset together with the count: "MRR 0.73 over 120 labeled queries (180 unlabeled, skipped)".
- Only compare runs evaluated on the same labeled subset.

Meanwhile keep labeling, and use the unlabeled queries for label-free checks such as faithfulness and failure-mode scanning.
