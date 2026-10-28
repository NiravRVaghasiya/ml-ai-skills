Not quite - both parts need changing.

**Scaling.** KMeans assigns points by Euclidean distance, and being unsupervised doesn't change that. Income spans about 480,000 units and age about 72, so income differences dominate every distance; your "segments" would just be income bands with age ignored. Standardize first:

```python
from sklearn.preprocessing import StandardScaler
X_scaled = StandardScaler().fit_transform(df[["age", "annual_income"]])
```

**Choosing k.** Inertia always goes down as k grows, so a fuzzy elbow doesn't tell you much. Compare silhouette scores across k (it has a maximum you can read off), and check the cluster sizes for the winner:

```python
from sklearn.cluster import KMeans
from sklearn.metrics import silhouette_score
scores = {k: silhouette_score(X_scaled, KMeans(n_clusters=k, n_init=10, random_state=42).fit_predict(X_scaled))
          for k in range(2, 10)}
```

If the best k gives one huge cluster and a few tiny ones, or the groups look elongated, try DBSCAN or a Gaussian mixture instead.
