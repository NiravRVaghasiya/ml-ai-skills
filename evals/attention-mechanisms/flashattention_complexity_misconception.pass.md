No. FlashAttention computes exact attention - it is not an approximation - and its compute is still quadratic: O(n²·d) FLOPs in sequence length n, the same as standard attention.

What it changes is memory traffic. It is IO-aware: it tiles Q, K and V into blocks that fit in on-chip SRAM, computes the softmax incrementally, and recomputes what it needs in the backward pass, so it never materializes the full n×n score matrix in GPU HBM. Extra memory therefore grows linearly in n instead of quadratically, and wall-clock time drops a lot because far fewer bytes move between HBM and SRAM.

So the quadratic wall is pushed further out, not removed. For very long contexts the O(n²) compute still dominates, which is why sparse, sliding-window, and linear-attention variants (which change the asymptotics, usually by approximating) and other long-context techniques remain relevant - it's a trade-off against exactness, not an obsolete line of work.
