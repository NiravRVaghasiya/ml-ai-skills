Mostly. He et al. describe degradation: higher training error at 34 layers than 18, even with BatchNorm. Vanishing gradients are the reason, and the identity shortcut F(x) + x fixes them.
