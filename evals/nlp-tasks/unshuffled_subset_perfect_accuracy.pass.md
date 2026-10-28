No - treat a perfect score after one epoch as a bug report, not a result. The IMDB splits on the Hub are stored sorted by label (all negative reviews first, then all positive), so `ds["train"].select(range(2000))` and `ds["test"].select(range(500))` almost certainly contain a single class. A model that always predicts that class gets accuracy 1.0 on a one-class test set without learning anything about sentiment.

Macro-F1 does not save you here: scikit-learn's `f1_score(average="macro")` averages over the labels that actually appear in `y_true`/`y_pred`, so a one-class evaluation set also gives 1.0. It only exposes the problem if you pass `labels=[0, 1]` explicitly.

What to do:
1. Check the label distribution of both subsets before trusting any metric:
   ```python
   from collections import Counter
   print(Counter(train["label"]), Counter(test["label"]))
   ```
2. Shuffle with a fixed seed before subsetting (or draw a stratified sample):
   ```python
   train = ds["train"].shuffle(seed=42).select(range(2000))
   test = ds["test"].shuffle(seed=42).select(range(500))
   ```
3. Retrain and re-evaluate; report macro-F1 and per-class precision/recall. If you tune epochs on this set, carve a validation split out of train and keep the test split for the final number.
