Hard to say from accuracy. In fraud detection the positive class is usually well under 1% of transactions, so a classifier that labels everything as legitimate already gets ~99%+ accuracy. Check what the majority-class baseline would score — if fraud prevalence is 0.6%, your 99.4% may mean the model learned nothing.

Look at recall (what share of fraud you catch) and precision (how many alerts are real), the PR curve / average precision, and the confusion matrix. Then decide the threshold based on what a missed fraud costs versus what a false alarm costs your operations team.
