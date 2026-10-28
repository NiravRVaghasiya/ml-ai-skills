Not quite. The scale is chosen to keep the variance of the logits near 1 under a simple assumption, not to guarantee it.

Why sqrt(d_k): suppose the components of q and k are independent, zero-mean and unit-variance. Each product q_i k_i then has mean 0 and variance 1, and q.k is a sum of d_k such independent terms, so Var(q.k) = d_k and its standard deviation grows like sqrt(d_k). Dividing by sqrt(d_k) brings the variance back to about 1 (a quick numpy check gives Var(q.k) of about 16 at d_k = 16 and about 254 at d_k = 256, both about 1 after the division).

Why not d_k: dividing by d_k would give variance 1/d_k - the logits would shrink toward 0 as d_k grows and the softmax would become nearly uniform, washing out the attention pattern.

Why it matters: unscaled logits with a standard deviation around 16 (d_k = 256) push the softmax into saturation, where one weight is close to 1, the rest are close to 0, and gradients through the softmax become very small.

Where your colleague is wrong: learned queries and keys are not independent unit-variance vectors, so nothing guarantees unit variance in a trained model. The sqrt(d_k) factor sets a sensible starting scale; logit magnitudes can and do drift during training, which is why some large models add extra normalization of queries and keys.
