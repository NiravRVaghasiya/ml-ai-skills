Neither is safe.

**Distances in the plot.** t-SNE is built to preserve local neighbourhoods - which points are close to which - and it deliberately distorts global geometry to do so. The gaps between clusters, the cluster sizes and their apparent densities are artifacts of the optimization and of the perplexity you chose, so "A is twice as far from B as from C" is not a statement the plot can support. If you need a number, compute distances between cluster centroids in the scaled original 20-dimensional space, and validate the clusters there with silhouette scores.

**t-SNE coordinates as features.** The embedding is stochastic (a different seed or perplexity rearranges it), and scikit-learn's `TSNE` has no `transform` method, so you can't map new customers into the same coordinates at prediction time - you'd have to refit on everything, which also leaks test rows into the features. If you want a compact representation, use PCA inside the classifier's `Pipeline` so it is fit on training folds only, or just give the classifier the scaled original features.

Keep the t-SNE plot as what it is good for: eyeballing whether the structure looks separated.
