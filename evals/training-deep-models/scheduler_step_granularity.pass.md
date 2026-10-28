No - the placement is the problem. Your schedule is built in optimizer steps: `total_steps = 20 * 500 = 10,000` and warmup is 5% of that, i.e. 500 steps. `LambdaLR` advances by one unit per `scheduler.step()` call, so with one call per epoch it has only advanced 20 steps after 20 epochs. Training never leaves warmup: the LR ends at 20/500 = 4% of your peak and spent most of the run far below that, which fits a loss that barely moved.

Call it once per batch, right after the optimizer step:

```python
for epoch in range(num_epochs):
    for batch, targets in train_loader:
        ...
        optimizer.step()
        scheduler.step()      # per-step schedule -> step every batch
```

The PyTorch examples that step once per epoch use schedulers defined in epochs (e.g. `StepLR(step_size=30)` meaning 30 epochs). The rule is to step in the same units the schedule was built in. Log `scheduler.get_last_lr()` for the first few hundred steps to confirm the warmup ramp and the decay look as intended.
