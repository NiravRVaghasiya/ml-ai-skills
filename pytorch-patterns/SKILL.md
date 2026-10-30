---
name: pytorch-patterns
display_name: Idiomatic PyTorch Patterns
description: >
  Use when the user wants to structure idiomatic PyTorch code — a custom Dataset
  and DataLoader, a clean training/validation loop, forward/backward hooks for
  debugging or feature extraction, or correct model checkpointing. Trigger
  phrases: "write a PyTorch training loop", "create a custom Dataset class",
  "how do I use forward hooks in PyTorch", "save and load a PyTorch
  checkpoint", "structure my PyTorch project". NOT for training tricks like
  mixed precision, LR schedules, or early stopping (see training-deep-models)
  or optimizer/backprop theory (see neural-net-fundamentals).
type: workflow
domain: deep-learning
level: intermediate
lifecycle: stable
risk_level: low
evidence_level: official-documentation
last_verified: 2026-10-27
capabilities:
  - custom-dataset-dataloader
  - train-eval-mode-switching
  - forward-hook-instrumentation
  - model-checkpointing
  - gradient-zeroing
requires:
conflicts:
related:
  - training-deep-models
  - neural-net-fundamentals
inputs: A request to structure PyTorch code — a Dataset/DataLoader, a training loop, hooks, or checkpointing.
outputs: Idiomatic PyTorch Dataset/DataLoader, train/eval loop, hook, and checkpoint code ready to adapt.
version_constraints:
  - "torch>=1.13: torch.load(..., weights_only=True) keyword (1.13 lower bound documented: the PyTorch 1.13 release notes add the weights_only option, and the 2.6.0 release notes flip its default to True, both read 2026-10-29; 1.13 and 2.6 not executed; all Workflow blocks executed CPU-only against torch 2.13.0 on 2026-10-27)"
---

## Overview
Most PyTorch bugs come from deviating from a small set of idioms: how a `Dataset`
feeds a `DataLoader`, when to call `model.train()`/`model.eval()`, when gradients
must be zeroed, and how to save state without pickling the whole model. This
skill walks through building a custom `Dataset`, wiring a correct train/eval
loop, using hooks to inspect intermediate activations, and checkpointing for
resumable training.

## Workflow
1. **Write a custom `Dataset` and wrap it in a `DataLoader`.** `__getitem__`
   should do per-sample work (load, transform); batching, shuffling, and
   parallel loading belong to the `DataLoader`, not the dataset.
   ```python
   from torch.utils.data import Dataset, DataLoader
   import torch

   class TabularDataset(Dataset):
       def __init__(self, features, labels):
           self.X = torch.as_tensor(features, dtype=torch.float32)
           self.y = torch.as_tensor(labels, dtype=torch.long)
           if len(self.X) != len(self.y):   # else extra labels are silently ignored
               raise ValueError(f"{len(self.X)} feature rows but {len(self.y)} labels")

       def __len__(self):
           return len(self.X)

       def __getitem__(self, idx):
           return self.X[idx], self.y[idx]

   train_loader = DataLoader(
       TabularDataset(X_train, y_train),
       batch_size=64, shuffle=True, num_workers=4,
       pin_memory=torch.cuda.is_available(),   # pinning only helps host->GPU copies
   )
   ```

2. **Define the model as an `nn.Module` with layers built in `__init__`.**
   Keep `forward` free of parameter creation — every learnable layer must exist
   before the first forward pass so the optimizer sees all parameters.
   ```python
   import torch.nn as nn

   class MLP(nn.Module):
       def __init__(self, in_dim, hidden_dim, num_classes):
           super().__init__()
           self.net = nn.Sequential(
               nn.Linear(in_dim, hidden_dim),
               nn.ReLU(),
               nn.Dropout(0.2),
               nn.Linear(hidden_dim, num_classes),
           )

       def forward(self, x):
           return self.net(x)

   device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
   model = MLP(in_dim=20, hidden_dim=128, num_classes=10).to(device)  # move before building the optimizer
   ```

3. **Write the train/eval loop with explicit mode switches and `no_grad`.**
   `model.train()`/`model.eval()` change how Dropout and BatchNorm behave;
   `torch.no_grad()` during validation stops autograd from building a graph you
   don't need, saving memory and time.
   ```python
   optimizer = torch.optim.AdamW(model.parameters(), lr=1e-3)
   criterion = nn.CrossEntropyLoss()

   def train_one_epoch(model, loader, optimizer, criterion, device):
       model.train()
       running_loss = 0.0
       for X, y in loader:
           X, y = X.to(device), y.to(device)
           optimizer.zero_grad(set_to_none=True)   # else grads accumulate across batches
           loss = criterion(model(X), y)
           loss.backward()
           optimizer.step()
           running_loss += loss.item() * X.size(0)
       return running_loss / len(loader.dataset)

   @torch.no_grad()
   def evaluate(model, loader, criterion, device):
       model.eval()
       total_loss, correct = 0.0, 0
       for X, y in loader:
           X, y = X.to(device), y.to(device)
           logits = model(X)
           total_loss += criterion(logits, y).item() * X.size(0)
           correct += (logits.argmax(dim=1) == y).sum().item()
       n = len(loader.dataset)
       return total_loss / n, correct / n
   ```

4. **Use forward hooks to inspect or capture intermediate activations.** Hooks
   let you observe a layer's output without modifying `forward()` — useful for
   feature extraction, debugging dead ReLUs, or visualizing activations. Always
   remove the handle when done to avoid leaking references.
   ```python
   activations = {}

   def make_hook(name):
       def hook(module, inputs, output):
           activations[name] = output.detach()
       return hook

   handle = model.net[0].register_forward_hook(make_hook("first_linear"))
   try:
       model.eval()
       with torch.no_grad():
           X_batch, _ = next(iter(train_loader))
           model(X_batch.to(device))
   finally:
       handle.remove()   # runs even if forward raises; a leaked hook keeps its closure + tensors alive
   print(activations["first_linear"].shape)
   ```

5. **Checkpoint `state_dict`s, not whole model objects, and support resume.**
   Saving `model.state_dict()` (weights only) ties the checkpoint to a fresh
   instantiation of the *current* class definition, so it survives refactors that
   `torch.save(model)` (pickles the class + weights together) does not.
   ```python
   import os

   def save_checkpoint(path, model, optimizer, epoch):
       tmp = f"{path}.tmp"   # same directory, so os.replace is a same-filesystem rename
       try:
           torch.save({
               "epoch": epoch,                       # last *completed* epoch
               "model_state": model.state_dict(),
               "optimizer_state": optimizer.state_dict(),
               # also store scheduler/GradScaler state_dicts here if you use them
           }, tmp)
           # atomic rename: an exception or process crash mid-write leaves the previous
           # checkpoint intact (surviving power loss additionally needs an fsync first)
           os.replace(tmp, path)
       except BaseException:
           if os.path.exists(tmp):
               os.remove(tmp)                        # don't leave a partial file behind
           raise

   def load_checkpoint(path, model, optimizer, device):
       # weights_only=True refuses arbitrary pickled objects (code execution on load);
       # it is the default since PyTorch 2.6 but explicit is safer on older versions
       ckpt = torch.load(path, map_location=device, weights_only=True)
       model.load_state_dict(ckpt["model_state"])
       optimizer.load_state_dict(ckpt["optimizer_state"])
       return ckpt["epoch"] + 1                  # the epoch to resume at

   save_checkpoint("epoch_10.pt", model, optimizer, epoch=10)
   start_epoch = load_checkpoint("epoch_10.pt", model, optimizer, device)   # -> 11
   ```

## Gotchas
- **Forgetting `model.eval()` during validation/inference** leaves Dropout active
  and BatchNorm using per-batch statistics instead of running averages — metrics
  computed this way are noisy and not representative of deployed behavior. Pair
  every `model.eval()` with a `model.train()` before the next training batch, or
  wrap eval logic in a helper (as in step 3) so it can't be missed.
- **Forgetting `optimizer.zero_grad()`** accumulates gradients across batches
  instead of computing a fresh gradient each step — the model still trains,
  just wrong, which makes it a hard bug to notice from loss curves alone.
  (`set_to_none=True` is the default since PyTorch 2.0; step 3 spells it out
  for older versions.)
- **`DataLoader(num_workers > 0)` on Windows (and macOS) requires an `if
  __name__ == "__main__":` guard.** These platforms use the `spawn` start method
  for worker processes, which re-imports the launching script — without the
  guard, worker processes re-run (and can infinitely recurse into) the top-level
  training code. Spawned workers also unpickle the dataset, so a `Dataset` class
  defined interactively (notebook/REPL) can fail to load there; use
  `num_workers=0` or move the class into an importable module.
- **Hooks that aren't removed leak memory.** A forward hook closure that stores
  tensors (as in step 4) keeps those tensors — and the whole autograd graph
  behind them, if not `.detach()`-ed — alive for as long as the handle is
  registered. Always call `handle.remove()` in a `finally` block (as in step 4)
  so an exception in the forward pass doesn't skip it.
- **`torch.save(model)` instead of `torch.save(model.state_dict())`** pickles the
  exact class path and object graph — loading it later after even a minor
  refactor (renamed class, moved module) raises an unpickling error, and since
  PyTorch 2.6 `torch.load`'s default `weights_only=True` refuses it outright. Ship
  `state_dict`s plus the code that defines the architecture, never the pickled
  object, especially for anything meant to outlive the current session.

## References
- [PyTorch: Datasets & DataLoaders tutorial](https://pytorch.org/tutorials/beginner/basics/data_tutorial.html) — official pattern for custom `Dataset`/`DataLoader` code.
- [torch.nn.Module — register_forward_hook docs](https://pytorch.org/docs/stable/generated/torch.nn.Module.html#torch.nn.Module.register_forward_hook) — hook API and handle-removal semantics.
- [PyTorch: Saving and Loading Models](https://pytorch.org/tutorials/beginner/saving_loading_models.html) — official guidance on `state_dict` vs. whole-model serialization.
