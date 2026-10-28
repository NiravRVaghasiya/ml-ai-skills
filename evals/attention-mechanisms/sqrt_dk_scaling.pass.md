Because the raw dot products grow with the head dimension. If the components of q and k are roughly independent with mean 0 and variance 1, then q·k is a sum of d_k such products, so its variance is about d_k and its standard deviation about √d_k (with d_k = 64, typical scores are around ±8).

Without the scaling, those large logits push the softmax into saturation: nearly all the weight lands on one key (an almost one-hot distribution), and the softmax's gradients with respect to the scores become tiny, so learning in the query and key projections slows or stalls.

Dividing by √d_k rescales the scores back to roughly unit variance, independent of d_k, which keeps the softmax in a regime with useful gradients. It is not what makes the weights sum to 1 - the softmax does that either way.
