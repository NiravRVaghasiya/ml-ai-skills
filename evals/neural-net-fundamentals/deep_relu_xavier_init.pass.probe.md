## Diagnosis: signal collapse from the wrong init

Glorot/Xavier normal sets Var(W) = 2/(fan_in + fan_out), which for square layers is 1/fan_in. That keeps variance constant only for an activation with unit gain. ReLU kills the negative half, so the second moment of each layer's output is about half of its input's. After 40 layers you're multiplying by ~0.5 forty times, so activations — and, symmetrically, backpropagated gradients — become vanishingly small.

**Fix:** `nn.init.kaiming_normal_(m.weight, mode='fan_in', nonlinearity='relu')`. Then sanity-check by hooking each layer and printing the std of its outputs; it should hover near a constant. Adding LayerNorm or residual blocks would also make the network far less sensitive to this.
