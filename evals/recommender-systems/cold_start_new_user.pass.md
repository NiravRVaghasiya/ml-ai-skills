Not as written. Those users are the cold-start case: ALS learns a factor only from a user's interactions, so a user with an empty row ends up with an all-zero factor (implicit does this itself if you refit with the empty row). Every item's score is then the dot product with zero, i.e. exactly 0, so all items tie and `model.recommend` hands back an arbitrary tie order. That is a meaningless list that looks like a personalized one.

Route them to a fallback instead:

```python
def recommend_for_user_id(uid: str, n: int = 10) -> list:
    pos = user_ids.get_indexer([uid])[0]          # -1 for an id the model has never seen
    if pos < 0 or train[pos].nnz == 0:
        return list(item_ids[np.argsort(-popularity, kind="stable")[:n]])
    return hybrid_recommend(pos, n)
```

- Popularity (most-interacted items, optionally per region/category) for brand-new users; if signup collects interests, rank items content-based (TF-IDF similarity to those interests) and blend.
- Once they have a few interactions, score them from those: refit on the next scheduled run, or, with implicit, `model.recommend(userid, user_items=their_row, recalculate_user=True)` computes a factor from that row on the fly.
- Evaluate the fallback separately (precision@K / coverage for users with fewer than N interactions) so you know how good the cold-start experience is.
