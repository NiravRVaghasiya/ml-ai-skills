No - you need both, and the missing `model.eval()` is very likely why your numbers jump around.

`torch.no_grad()` only turns off gradient tracking: autograd stops building a graph, which saves memory and time. It does not change how any layer behaves. Layer behaviour is controlled by the module's mode:

- **Dropout** in train mode keeps randomly zeroing activations, so every validation pass is a different random sub-network - hence the run-to-run jitter and the lower accuracy.
- **BatchNorm** in train mode normalizes with the current batch's statistics instead of its running statistics, and it keeps updating those running statistics from your validation batches, even under `no_grad`.

`model.eval()` switches both to inference behaviour. Use the two together and switch back before training:

```python
@torch.no_grad()
def evaluate(model, loader, criterion, device):
    model.eval()
    ...

val_loss, val_acc = evaluate(model, val_loader, criterion, device)
model.train()   # before the next training batch
```

`torch.inference_mode()` can replace `no_grad` here, but it also leaves the module in whatever mode it was in.
