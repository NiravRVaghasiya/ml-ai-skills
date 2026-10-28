Not quite - two corrections.

First, the terminology: unmasked self-attention is permutation-equivariant, not permutation-invariant. Permute the input tokens and the outputs are permuted the same way; only a pooled or summed output would be invariant. Without positional information, an unmasked, bidirectional encoder therefore really is order-blind - it sees a bag of tokens.

Second, a decoder-only GPT is not unmasked. The causal mask breaks that symmetry: token i attends only to the tokens up to i, so each position sees a different-length prefix, and the network can learn positional information from that asymmetry (for example by implicitly counting how many tokens it can attend to). Haviv et al. (2022) trained decoder-only LMs with no positional encodings ("NoPE") and found them still competitive and still able to recover position.

So "literally no way" is wrong. That said, standard practice still adds explicit positional information - RoPE, ALiBi-style biases, learned or sinusoidal embeddings - rather than relying on the mask alone.
