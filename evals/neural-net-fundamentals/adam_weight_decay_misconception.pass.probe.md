**Short answer: no.** In PyTorch these are two different algorithms.

`Adam(weight_decay=λ)` treats decay as an L2 penalty: the gradient becomes g + λw, and that sum is fed into the moment estimates m and v. The decay term is therefore divided by \(\sqrt{\hat v}+\epsilon\), so weights whose gradients are large get very little regularization.

`AdamW` (Loshchilov & Hutter) applies the decay separately: w ← w − lr·λ·w, outside the adaptive update. That decoupled form is what the paper used.

Under vanilla SGD the two would be identical up to a rescaling of λ, but with Adam they diverge. Switch your optimizer to `torch.optim.AdamW(model.parameters(), lr=3e-4, weight_decay=0.01)`.
