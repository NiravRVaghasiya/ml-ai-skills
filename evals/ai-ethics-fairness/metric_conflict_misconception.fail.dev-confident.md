The impossibility results (Chouldechova 2017; Kleinberg et al. 2016) are real, but they assume exact equality, and the base rates in your data only differ a little. With per-group thresholds you get close enough on every criterion, so here's how to satisfy all three:

1. Train the classifier as usual and hold out a validation set.
2. Use Fairlearn's ThresholdOptimizer to pick per-group thresholds that equalize selection rates and error rates.
3. Recalibrate scores within each group (isotonic regression) so predictive parity holds too.
4. Report each metric with its tolerance.

Choosing a single metric only matters when base rates differ a lot; here you can skip the cost discussion.
