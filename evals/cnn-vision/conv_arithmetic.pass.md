conv1: H_out = floor((H_in + 2P - K) / S) + 1 = floor((224 + 6 - 7) / 2) + 1 = floor(223 / 2) + 1 = 112. So conv1 outputs 112×112 (conv2 then gives 56×56, and conv3 keeps 56×56).

Receptive field: use r_l = r_(l-1) + (K_l - 1) × (product of the strides of all earlier layers), starting from r_0 = 1.
- conv1: 1 + (7 - 1) × 1 = 7
- conv2: 7 + (3 - 1) × 2 = 11 (earlier stride product = 2)
- conv3: 11 + (3 - 1) × (2 × 2) = 19 (earlier stride product = 4)

So one conv3 output unit has a 19×19 receptive field in the input. The stride term is why later layers grow the field faster: each 3×3 layer adds 2 × (cumulative stride) pixels, not just 2. This is the theoretical receptive field; the effective one, where most of the gradient is concentrated, is smaller.
