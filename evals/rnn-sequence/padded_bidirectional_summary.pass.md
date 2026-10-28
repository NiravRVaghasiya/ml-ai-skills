Two bugs combine here.

1. Padding. For a short sentence padded to length T, output[:, -1] is position T-1, which is padding. The forward GRU has spent the last few steps reading pad tokens, so its state has drifted away from the sentence summary. The backward GRU starts at position T-1, so its half of output[:, -1] has read only padding and none of the sentence. The longest sentence in each batch is unaffected, which is why short sentences suffer most.
2. Direction. Even for a sentence with no padding, the backward direction's summary of the whole sentence is at position 0 (output[:, 0, 128:]), not at the last position.

Fix: pack the batch and use h_n.

```python
packed = nn.utils.rnn.pack_padded_sequence(emb, lengths.cpu(), batch_first=True, enforce_sorted=False)
_, h_n = gru(packed)                            # h_n: (num_layers * 2, batch, 128)
sent = torch.cat([h_n[-2], h_n[-1]], dim=1)     # top layer: forward final, backward final
```

h_n has shape (num_layers * num_directions, batch, hidden) whatever batch_first says, and the top layer's states are its last two entries: h_n[-2] is the top forward state at each sentence's real last token, h_n[-1] the top backward state after reading the whole sentence. h_n[0] and h_n[1] would be the bottom layer.

If you prefer to read from output, do it on the pad_packed_sequence output of the packed run: the forward half ([:, :128]) at lengths - 1 and the backward half from position 0. Without packing there is no clean position to read in this 2-layer model - the top layer's forward states take the bottom layer's backward states as input, and those have already read the padding - so even the forward half at lengths - 1 is contaminated. Also mask pad positions in any per-token loss or pooling.
