---
name: rnn-sequence
display_name: RNNs & Sequence Modeling
description: >
  Use when the user wants to understand recurrent neural networks — vanilla
  RNNs, LSTMs, GRUs, or how sequence models process ordered data over time.
  Trigger phrases: "explain how LSTMs work", "LSTM vs GRU, which one",
  "what causes vanishing gradients in RNNs", "how does a recurrent network
  process a sequence", "when should I use an RNN instead of a transformer".
  NOT for attention/transformer internals (see attention-mechanisms) or
  time-series forecasting workflows like ARIMA/Prophet (see time-series).
type: reference
domain: deep-learning
level: intermediate
lifecycle: stable
risk_level: low
evidence_level: established-practice
last_verified: 2026-10-27
capabilities:
  - rnn-recurrence
  - backpropagation-through-time
  - lstm-gating
  - gru-gating
  - rnn-vs-attention-tradeoffs
requires:
conflicts:
related:
  - neural-net-fundamentals
  - attention-mechanisms
  - time-series
inputs: A question about how RNNs/LSTMs/GRUs process sequences or why vanilla RNNs struggle with long-range dependencies.
outputs: A conceptual explanation of recurrent architectures and their gating mechanisms, contrasted with attention.
---

## Overview
Recurrent networks process a sequence one element at a time, carrying a hidden
state forward as memory of everything seen so far — the natural fit for text,
audio, or time series where order matters. This card covers the vanilla RNN
recurrence, why it fails on long sequences, and how LSTMs and GRUs fix that with
learned gating. It's the mechanistic foundation for understanding why attention
(see attention-mechanisms) eventually replaced RNNs for most sequence tasks.

## Key Concepts
- **The recurrence.** At each timestep `t`, a vanilla RNN combines the current
  input with the previous hidden state using the *same* shared weights:
  \[ h_t = \tanh(W_{xh}x_t + W_{hh}h_{t-1} + b) \]
  `h_t` is both the output at that step and the memory carried to the next one —
  weight sharing across time is what lets the network handle sequences of any
  length with a fixed parameter count.
- **Backpropagation through time (BPTT).** Training unrolls the recurrence into a
  chain as long as the sequence and backpropagates through the whole chain. This
  is structurally identical to backprop through a very deep feedforward network —
  the same product-of-derivatives that causes vanishing/exploding gradients in
  deep nets (see neural-net-fundamentals) causes it here too, except the "depth"
  is the sequence length, so long sequences make it worse, not just deep stacking.
- **Why vanilla RNNs forget.** Because `h_{t-1}` is squashed through `tanh` and
  remixed with new input at every step, information from many steps back gets
  diluted and its gradient shrinks geometrically — a vanilla RNN's *effective*
  memory is much shorter than its nominal sequence length.
- **LSTM — gating solves the memory problem.** An LSTM keeps a separate **cell
  state** `c_t` that flows across time with only *additive*, gated updates
  (no repeated squashing), controlled by three sigmoid gates (the forget gate was
  added by Gers et al., 1999 — the 1997 original had only input/output gates and a
  fixed self-connection of 1):
  - **Forget gate** `f_t`: how much of the old cell state to keep.
  - **Input gate** `i_t`: how much of the new candidate value to write in.
  - **Output gate** `o_t`: how much of the cell state to expose as the hidden
    state.
  \[ c_t = f_t \odot c_{t-1} + i_t \odot \tilde{c}_t, \quad h_t = o_t \odot \tanh(c_t) \]
  Because the forget gate can sit near 1, gradients can flow through many
  timesteps largely unchanged — this additive path is the actual fix for
  vanishing gradients, not just "more parameters."
- **GRU — a lighter-weight alternative.** Merges the forget/input gates into a
  single **update gate**, adds a **reset gate** that controls how much of
  `h_{t-1}` feeds the candidate state, and drops the separate cell state. With
  three weight blocks (two gates + candidate) instead of four (three gates +
  candidate) it has ~3/4 of an LSTM's parameters at the same hidden size. A direct
  GRU-vs-LSTM comparison (Chung et al., 2014) found no consistent winner, and
  Greff et al. (2017) found that coupling the input and forget gates (as GRU does)
  did not significantly change LSTM performance — a reasonable default when
  training speed or parameter budget matters, but worth checking against an LSTM
  on your task.
- **Bidirectional & stacked RNNs.** A bidirectional RNN runs one RNN
  forward and one backward over the sequence and concatenates their hidden
  states — useful whenever the *entire* sequence is available at once (e.g.,
  offline text classification), not for autoregressive generation or streaming
  inference where future tokens don't exist yet. Stacking multiple RNN layers
  lets later layers operate on the sequence of hidden states from earlier layers,
  building higher-level temporal features.
- **Why attention displaced RNNs.** Two structural limits: (1) recurrence is
  inherently sequential — step `t` needs step `t-1`'s output, so it can't be
  parallelized across the sequence during training the way attention can; (2)
  even with gating, the path length between two distant tokens is still
  proportional to their distance, versus the constant path length self-attention
  gives every pair of tokens. See attention-mechanisms for the mechanism that
  replaced this. The trade-off runs the other way at inference: an RNN carries a
  fixed-size state, so per-step compute and memory stay constant, while a
  transformer's KV cache grows with context length.
- **When recurrence still makes sense (2026 view).** Consider an LSTM/GRU for
  streaming or on-device inference with tight memory, short sequences or small
  datasets where a large pretrained transformer isn't available or justified, and
  as a cheap baseline. Recurrence has also come back in parallel-trainable forms —
  state-space and linear-recurrent models such as Mamba (Gu & Dao, 2023) train
  with parallel scans and decode with constant state. Whether they beat a
  transformer on a given task is an empirical question, not a settled ranking.

## Gotchas
- **Exploding gradients need explicit clipping.** Unlike vanishing gradients,
  which gating architecturally mitigates, exploding gradients in RNNs are common
  and are handled operationally with gradient norm clipping (Pascanu et al., 2013;
  `torch.nn.utils.clip_grad_norm_`) — skipping this is a frequent cause of loss
  suddenly spiking to `NaN` partway through training.
- **LSTMs/GRUs reduce the vanishing-gradient problem, they don't eliminate it.**
  Effective memory is usually far shorter than the sequence: one study of LSTM
  language models found they used about 200 tokens of context on average and
  were order-sensitive mainly within the last ~50 (Khandelwal et al., 2018) —
  your task may differ, so probe it rather than assume. Truncated BPTT
  (splitting long sequences into chunks and carrying hidden state across chunk
  boundaries) introduces its own bias by cutting gradient flow at chunk edges,
  and in PyTorch you must `.detach()` the carried state between chunks — otherwise
  the second `backward()` raises "Trying to backward through the graph a second
  time". Adding `retain_graph=True` is not a fix: it backpropagates through every
  earlier chunk, and once `optimizer.step()` has updated the weights in place it
  typically fails with an in-place-modification error instead.
- **Padding variable-length sequences without masking corrupts training.**
  Without a length-aware pack/mask, (a) taking `output[:, -1]` as the sequence
  summary gives short sequences the state *after* reading padding, (b) per-step
  losses include pad positions unless masked (e.g., `ignore_index`), and (c) a
  bidirectional RNN's backward pass starts on padding. Use
  `pack_padded_sequence(..., enforce_sorted=False)` and read `h_n` — or, for a
  unidirectional RNN only, gather `output` at `lengths - 1`. For a bidirectional
  RNN, the backward half of `output[lengths - 1]` has read just the last token, so
  use `h_n[-2]`/`h_n[-1]` instead — and on *unpacked* padded input no position is
  clean in a stacked bidirectional RNN (each upper layer's forward pass consumes
  the lower layer's backward outputs, which have read padding), so pack first. This
  is one of the most common silent bugs in RNN code.
- **Output shape confusion with bidirectional/stacked RNNs.** In PyTorch, `h_n`
  has shape `(num_layers * num_directions, batch, hidden)` — `batch_first=True`
  does *not* apply to it. The top layer's states are the *last* entries: for a
  bidirectional RNN, `h_n[-2]` is the top forward state and `h_n[-1]` the top
  backward state (which corresponds to `output[:, 0, hidden:]` for packed or
  unpadded input, not `output[:, -1]`). Indexing `h_n[0]` gives the bottom layer — a common wiring bug
  for classifier heads.

## References
- [Long Short-Term Memory (Hochreiter & Schmidhuber, 1997)](https://doi.org/10.1162/neco.1997.9.8.1735) — the original LSTM paper (DOI landing page; the author-hosted PDF mirror had an expired TLS certificate when checked on 2026-10-27).
- [Learning Phrase Representations using RNN Encoder-Decoder (Cho et al., 2014)](https://arxiv.org/abs/1406.1078) — introduces the GRU.
- [Understanding LSTM Networks (Christopher Olah)](https://colah.github.io/posts/2015-08-Understanding-LSTMs/) — the best visual walkthrough of the gating math.
- [On the difficulty of training Recurrent Neural Networks (Pascanu et al., 2013)](https://arxiv.org/abs/1211.5063) — analyzes vanishing/exploding gradients in RNNs and proposes gradient-norm clipping.
