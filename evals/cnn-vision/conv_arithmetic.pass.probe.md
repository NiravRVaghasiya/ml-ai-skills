Output: floor((224 + 2*3 - 7)/2) + 1 = 112 -> 112x112.

Receptive field, tracking the jump (cumulative stride) j and size r:
- start: r = 1, j = 1
- conv1 (k=7, s=2): r = 1 + 6*1 = 7, j = 2
- conv2 (k=3, s=2): r = 7 + 2*2 = 11, j = 4
- conv3 (k=3, s=1): r = 11 + 2*4 = 19, j = 4

So a conv3 unit covers 19x19 input pixels.
