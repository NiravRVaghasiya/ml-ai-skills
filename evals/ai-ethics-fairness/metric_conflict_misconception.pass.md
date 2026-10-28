You can't get all three at once in general. Demographic parity, equalized odds and predictive parity (calibration within groups) cannot all hold at once whenever the groups have different base rates of the positive outcome - this is the impossibility result from Chouldechova (2017) and Kleinberg, Mullainathan & Raghavan (2016). They can only coexist in degenerate cases, such as a perfect classifier or identical base rates across groups.

So the real decision is which fairness criterion matches the harm you most need to avoid in this deployment:
- If false negatives deny people a benefit, equal opportunity / equalized odds is usually the focus.
- If scores are consumed as probabilities by downstream decision makers, predictive parity (calibration) matters most.
- Demographic parity fits cases where you need equal selection rates regardless of label differences.

Pick one primary metric, report the others as diagnostics, and document the trade-off.
