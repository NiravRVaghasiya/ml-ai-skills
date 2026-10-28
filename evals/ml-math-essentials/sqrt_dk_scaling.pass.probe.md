Your colleague is overstating it.

Take q, k ∈ ℝ^{d_k} with i.i.d. entries of mean 0 and variance 1. Then \(q^\top k=\sum_{i=1}^{d_k} q_i k_i\) has mean 0 and variance \(d_k\), so its typical magnitude is \(\sqrt{d_k}\). Scaling by \(1/\sqrt{d_k}\) makes the variance ≈ 1; scaling by \(1/d_k\) would make it \(1/d_k\), so the scores would collapse toward zero and attention would be almost uniform.

The motivation is the softmax: with large logits it saturates to a near one-hot distribution and its gradient nearly vanishes.

But this is an argument under an idealized independence assumption. After training, Q and K are learned, correlated and not unit-norm, so the scaling does not guarantee unit variance — logit growth is a known issue at scale.
