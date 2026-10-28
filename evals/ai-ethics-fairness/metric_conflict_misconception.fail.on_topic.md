In theory these criteria are incompatible when base rates differ between groups, but that impossibility result is mostly academic. In practice all three can be achieved to within a few percentage points: train normally, then tune group-specific thresholds with Fairlearn's ThresholdOptimizer until demographic parity, equalized odds and predictive parity are all inside tolerance.

So you don't have to choose a single fairness metric or trade off the costs of different errors - just tighten the tolerance until every metric passes.
