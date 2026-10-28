This is the classic initialization/activation mismatch, and depth makes it severe.

Xavier/Glorot init (Var(W) of about 1/n_in for a square layer) is derived for activations that are zero-centered with slope about 1 near zero, like tanh. ReLU zeroes out roughly half of its inputs, so each layer passes on only about half the second moment it receives. Across 40 un-normalized layers that factor compounds: the activation std shrinks by roughly (1/sqrt(2))^40, about 1e-6, and the backward pass shrinks the gradients the same way - exactly the flat loss and tiny early-layer gradients you see. In a quick torch check, 40 ReLU layers of width 256 with xavier_normal_ ended with activation std around 6e-7, versus about 0.5 with He init.

Fix:
1. Use He (Kaiming) init, Var(W) = 2/n_in: `nn.init.kaiming_normal_(layer.weight, nonlinearity="relu")`, biases at zero.
2. Verify: log each layer's activation std on one batch and each layer's gradient norm after one backward pass; they should stay roughly constant across depth instead of decaying.
3. For a network this deep, BatchNorm/LayerNorm or residual connections make training much more robust to the init choice.

PyTorch's default nn.Linear init is neither He nor Xavier (kaiming_uniform_ with a=sqrt(5), Var = 1/(3 n_in)), so it would also shrink signals here. Adam rescales per-parameter steps, but it cannot recover a signal that has vanished in the forward pass, so the learning rate is not the first thing to change.
