padding='same' keeps the spatial size, so the output stays 56×56 and you get a tensor of shape (N, 128, 56, 56). Keras and PyTorch use the same convention, so the outputs will match.
