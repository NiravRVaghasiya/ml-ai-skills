It's very unlikely to be precision. With `dynamo=False`, `torch.onnx.export` uses the legacy tracing exporter: it runs your sample once and records the operations that actually executed. Your sample had max |x| <= 10 (or > 10), so only that branch of the `if` was recorded; the condition itself is baked out of the graph. Every production input that should take the other branch silently runs the recorded path, so its output is wrong, not slightly off. Loosening `rtol`/`atol` would only hide that, and a different opset still records only the one traced branch.

Fix the model, then the export:

```python
def forward(self, x):
    m = x.abs().max()
    x = torch.where(m > 10, x / m, x)     # both branches live in the graph
    ...

torch.onnx.export(model, (sample,), "model.onnx", input_names=["input"],
                  dynamic_shapes=({0: Dim.DYNAMIC},), opset_version=18, dynamo=True)
```

`dynamo=True` (the `torch.export`-based exporter) refuses data-dependent Python control flow instead of baking it in silently, so it would have failed loudly on the original `if`; `torch.cond` is the other option when the branches differ more than a `where` can express.

Then redo the parity check on inputs that exercise both branches - batches with max |x| above 10 and below it - and on a batch size different from the export sample. Your original check passed because it only ever tested one path.
