I'd be careful here — **n = 10,000 doesn't automatically save you.**

The classical CLT requires the underlying distribution to have a finite variance. If the latencies really behave like a Cauchy (or another distribution with tail index < 2), the variance is infinite and the sample mean has no normal limit — more data doesn't make it normal, and for a true Cauchy, which has no mean, it doesn't stabilize at all. Even if the variance is finite in a technical sense (e.g. timeouts cap latency), with 1000× outliers the mean is driven by a handful of requests and the normal approximation can be poor even at this n.

What I'd do instead:
1. Decide whether you care about typical latency (median), tail latency (p95/p99), or mean latency.
2. For medians or percentiles, use a bootstrap confidence interval of the difference.
3. Consider analyzing log(latency).
4. Mann–Whitney U can tell you whether one arm tends to be slower, but it is not a test of mean latency.
