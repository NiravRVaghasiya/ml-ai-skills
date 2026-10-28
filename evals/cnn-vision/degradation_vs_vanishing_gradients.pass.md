Not quite - that's the popular version, but it isn't what the ResNet paper argued.

He et al. (2015) describe a degradation problem: a 34-layer plain network had higher training error than an 18-layer one. Higher training error rules out overfitting as the cause. They also argue it is unlikely to be caused by vanishing gradients, because the plain networks were trained with BatchNorm and they checked that the forward and backward signals had healthy norms. Their diagnosis is an optimization difficulty: deeper plain stacks are harder for SGD to fit, even though a deeper net could in principle copy the shallower one and add identity layers.

The residual formulation targets exactly that. Each block learns a residual F(x) and outputs F(x) + x (with a 1×1 projection when shapes change), so representing an identity mapping only means pushing F(x) toward zero.

The gradient-flow picture isn't wrong, just partial: the identity shortcut does give gradients a direct additive path to early layers, which He et al. analyzed in the 2016 "Identity Mappings" paper. Together with BatchNorm, that let 152-layer ImageNet ResNets train well. Vanishing gradients from poor initialization had largely been handled earlier by normalized initialization and BatchNorm.
