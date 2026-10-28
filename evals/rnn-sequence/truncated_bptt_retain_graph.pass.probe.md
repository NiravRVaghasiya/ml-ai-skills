Don't fight the second error — the first fix was the wrong one. Revert to plain `loss.backward()`.

The underlying issue: the `(h, c)` tuple you hand to chunk *k+1* is still connected to chunk *k*'s computation graph. Detach it at the start of every chunk:

```python
h, c = h.detach(), c.detach()
```

With `retain_graph=True`, each backward pass also walks back through every earlier segment, so compute and memory keep growing — and because `optimizer.step()` updated the parameters in place after the previous backward, the graph's saved copies of the weights are out of date, which is exactly what the in-place error is complaining about. Detaching keeps the numeric values of the hidden state (so context still carries over) and simply truncates the gradient — that's the definition of truncated BPTT.
