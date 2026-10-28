conv1 output is (224 - 7) / 2 = 108.5, which rounds to 109×109 since padding is usually ignored at the borders. The receptive field just adds up the kernel sizes: 7 + 3 + 3 = 13 pixels.
