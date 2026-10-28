Expect 27×27: floor((56 - 3) / 2) + 1 = 27, because 'same' padding in PyTorch only adds padding when the stride is 1. The outputs will match Keras since both frameworks compute cross-correlation.
