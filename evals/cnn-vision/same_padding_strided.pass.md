As written it won't run: PyTorch only supports padding='same' for stride-1 convolutions, so nn.Conv2d(64, 128, 3, stride=2, padding='same') raises a ValueError ("padding='same' is not supported for strided convolutions") when the layer is constructed.

Expected size: Keras/TF 'same' at stride 2 gives ceil(56 / 2) = 28, so 28×28 with 128 channels.

In PyTorch, set the padding explicitly. padding=1 gives floor((56 + 2·1 - 3) / 2) + 1 = 28, so the shape matches. Print the tensor shape after the layer to confirm.

The values will not match exactly, though, if you copy the Keras weights. With an odd total padding, TF 'same' pads asymmetrically - here 0 on the top/left and 1 on the bottom/right - while padding=1 pads one pixel on every side, so the kernel is sampled at shifted positions. For numerical parity, pad explicitly with F.pad(x, (0, 1, 0, 1)) and use padding=0 in the conv; then compare outputs on a test input.
