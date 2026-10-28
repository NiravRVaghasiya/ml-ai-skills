The culprit is that `output[:, -1, :]` is taken at the *padded* final timestep.

* **Forward direction:** for a sentence of length L < T, the forward GRU keeps updating its state over the T − L pad steps, so the vector at position −1 no longer summarizes the sentence.
* **Backward direction:** the reverse GRU begins at index T−1, i.e. on padding, so its half of `output[:, -1]` has seen essentially nothing.

Use packing and take the final hidden states:

```python
packed = pack_padded_sequence(x, lengths.cpu(), batch_first=True, enforce_sorted=False)
_, h_n = self.gru(packed)
h_n = h_n.view(2, 2, x.size(0), 128)     # (num_layers, num_directions, batch, hidden)
rep = torch.cat([h_n[-1, 0], h_n[-1, 1]], dim=-1)
```

Note `h_n` is always `(num_layers*num_directions, batch, hidden)` — batch_first doesn't change it.
