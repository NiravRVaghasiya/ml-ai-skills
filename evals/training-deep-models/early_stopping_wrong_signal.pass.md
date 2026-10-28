No - both choices undo the point of early stopping.

1. **Wrong signal.** Training loss keeps going down while the model overfits; that's exactly what you're seeing after epoch 8. Monitoring it, early stopping will essentially never trigger. Monitor validation loss (or the validation metric you care about).
2. **Wrong weights.** Even with the right signal, the loop only stops `patience` epochs after the best one, so the final weights are already past the optimum. Save a checkpoint every time validation loss improves, and restore the best checkpoint after the loop.

```python
best_val_loss, patience, bad_epochs = float("inf"), 5, 0
for epoch in range(num_epochs):
    train_one_epoch(...)
    val_loss = evaluate(...)          # per-sample mean over the whole validation set
    if not math.isfinite(val_loss):
        raise RuntimeError(f"non-finite validation loss at epoch {epoch}")
    if val_loss < best_val_loss:
        best_val_loss, bad_epochs = val_loss, 0
        torch.save({"model": model.state_dict()}, "best.pt")
    else:
        bad_epochs += 1
        if bad_epochs >= patience:
            break
model.load_state_dict(torch.load("best.pt", weights_only=True)["model"])
```

With your curves this would stop around epoch 13 and give you the epoch-8 weights. If you also want to fight the overfitting itself, add weight decay (excluding norms and biases), dropout, or augmentation.
