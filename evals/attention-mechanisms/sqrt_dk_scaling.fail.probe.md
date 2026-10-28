The variance of q·k grows with d_k, which makes the softmax saturate and the gradients small. Dividing by d_k (not its square root) fixes this by bringing the variance back to 1.
