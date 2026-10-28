Close, but no. Greedy decoding at temperature 0 removes sampling randomness, yet outputs can still differ between runs: GPU floating-point arithmetic is non-associative, so different batching or hardware can change logits slightly and flip near-ties, and hosted models can be updated or routed to different infrastructure.

To make evals reproducible enough: pin the exact model snapshot, store every raw response, and repeat key runs several times to see how much they vary.
