No - they are not the same, and for a faithful reproduction the difference matters.

- `torch.optim.Adam(weight_decay=0.01)` implements *coupled* L2 regularization: it adds `0.01 * w` to the gradient before the moment estimates are updated. That term is then divided by Adam's adaptive `sqrt(v_hat)` like the rest of the gradient, so parameters with large historical gradients are barely decayed and parameters with small gradients are decayed heavily.
- `torch.optim.AdamW(weight_decay=0.01)` implements *decoupled* weight decay (Loshchilov & Hutter, 2017): it shrinks the weights directly, `w -= lr * 0.01 * w`, separately from the adaptive gradient step, so every parameter is decayed at the same relative rate.

The two coincide for plain SGD, where there is no per-parameter rescaling, but not under Adam. A quick check: with a zero gradient, one step at lr=0.1 and weight_decay=0.5 from w=1 gives 0.90 with Adam (the L2 term goes through the normalized update) and 0.95 with AdamW (w * (1 - lr * wd)).

To reproduce the paper, switch to `torch.optim.AdamW(params, lr=3e-4, weight_decay=0.01)`. The defaults differ too: AdamW's `weight_decay` defaults to 0.01, Adam's to 0. If the paper excluded biases and norm weights from decay, replicate those parameter groups as well.
