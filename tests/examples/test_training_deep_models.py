"""Executes training-deep-models/SKILL.md's python blocks on a tiny CPU problem.

The skill hardens an *existing* loop, so the test injects what that loop
already has (see the skill's `inputs`): ``model`` (an MLP with LayerNorm and
BatchNorm so step 2's param-group split has something to split),
``criterion``, ``train_loader`` and ``val_loader``. Nothing is stubbed and no
text is substituted: the blocks run verbatim with the skill's own epoch count.

On a CPU-only machine the AMP path runs with autocast/GradScaler *disabled*;
the fp16 CUDA path is not exercised here (no GPU in CI).

Regression tests pin the defects found in the audit (each failed against the
pre-fix skill code):

* ``torch.cuda.amp.GradScaler()`` is deprecated (FutureWarning) and, with the
  hard-coded ``device_type="cuda"``, warns and silently disables itself on CPU.
* The cosine factor rose back toward the peak LR if the scheduler was stepped
  past ``total_steps`` (e.g. resuming/extending a run).
* A NaN/inf validation loss never compares ``< best``: a run that diverged
  from epoch 0 never wrote ``best.pt`` and crashed with FileNotFoundError on
  restore instead of reporting the divergence.
* Validation loss was a mean of per-batch means, which over-weights a smaller
  final batch.
"""
from __future__ import annotations

import contextlib
import io
import math
import tempfile
import unittest
from pathlib import Path
import warnings

from examples import harness

SLUG = "training-deep-models"


def make_namespace(n_train: int = 128, n_val: int = 10, seed: int = 0, nan_model: bool = False):
    import torch
    from torch import nn
    from torch.utils.data import DataLoader, TensorDataset

    torch.manual_seed(seed)                     # model init below uses the global RNG
    g = torch.Generator().manual_seed(seed)
    X = torch.randn(n_train + n_val, 8, generator=g)
    y = (X[:, 0] + X[:, 1] > 0).long()
    model = nn.Sequential(nn.Linear(8, 32), nn.LayerNorm(32), nn.ReLU(),
                          nn.Linear(32, 16), nn.BatchNorm1d(16), nn.ReLU(), nn.Linear(16, 2))
    if nan_model:
        with torch.no_grad():
            model[-1].bias.fill_(float("nan"))
    return {
        "model": model,
        "criterion": nn.CrossEntropyLoss(),
        "train_loader": DataLoader(TensorDataset(X[:n_train], y[:n_train]), batch_size=32, shuffle=True),
        # 10 samples / batch_size 4 -> batches of 4, 4, 2 (uneven on purpose)
        "val_loader": DataLoader(TensorDataset(X[n_train:], y[n_train:]), batch_size=4),
    }


def run_skill(indices=None, workdir=None, **kw):
    with contextlib.redirect_stdout(io.StringIO()):     # early stopping prints
        return harness.run_blocks(SLUG, indices, namespace=make_namespace(**kw), workdir=workdir)


def weighted_val_loss(ns):
    import torch

    model, crit = ns["model"], ns["criterion"]
    model.eval()
    total, n = 0.0, 0
    with torch.no_grad():
        for xb, yb in ns["val_loader"]:
            total += crit(model(xb), yb).item() * len(yb)
            n += len(yb)
    return total / n


@harness.requires("torch", "numpy")
class TrainingDeepModelsExampleTest(unittest.TestCase):
    def test_full_workflow_runs_and_restores_best_checkpoint(self):
        import torch

        with tempfile.TemporaryDirectory() as tmp:
            ns = run_skill(workdir=Path(tmp))
            best = torch.load(Path(tmp) / "best.pt", weights_only=True)
        # step 2: every 1-D param (biases, LayerNorm/BatchNorm affine) is excluded from decay
        decay = {id(p) for p in ns["decay"]}
        no_decay = {id(p) for p in ns["no_decay"]}
        for name, p in ns["model"].named_parameters():
            self.assertIn(id(p), no_decay if p.ndim == 1 else decay, name)
        self.assertEqual([g["weight_decay"] for g in ns["optimizer"].param_groups], [0.01, 0.0])

        # step 5: restored weights are the saved best ones, and best_val_loss is a per-sample mean
        for k, v in ns["model"].state_dict().items():
            self.assertTrue(torch.equal(v.cpu(), best["model"][k].cpu()), k)
        self.assertTrue(math.isfinite(ns["best_val_loss"]))
        self.assertAlmostEqual(ns["best_val_loss"], weighted_val_loss(ns), places=5)
        self.assertLess(ns["best_val_loss"], math.log(2))   # learned something vs. chance

    def test_schedule_warms_up_then_decays_and_stays_low(self):
        ns = run_skill([0, 1, 2])
        f, warm, total = ns["lr_lambda"], ns["warmup_steps"], ns["total_steps"]
        self.assertGreater(warm, 0)
        self.assertEqual(f(0), 0.0)
        self.assertAlmostEqual(f(warm), 1.0)
        self.assertAlmostEqual(f(total), 0.0, places=9)
        # stepping past the planned horizon must not climb back toward the peak LR
        for extra in (1, total // 2, total):
            self.assertLessEqual(f(total + extra), 1e-9)

    def test_amp_setup_emits_no_deprecation_or_cuda_unavailable_warnings(self):
        import torch

        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            ns = run_skill([0, 1, 2, 3])
            xb, yb = next(iter(ns["train_loader"]))
            ns["train_step"](xb.to(ns["device"]), yb.to(ns["device"]))
        msgs = [str(w.message) for w in caught]
        self.assertEqual([m for m in msgs if "deprecated" in m or "not available" in m], [])
        self.assertEqual(ns["scaler"].is_enabled(), torch.cuda.is_available())

    def test_diverged_validation_loss_fails_loudly(self):
        with self.assertRaisesRegex(RuntimeError, "non-finite validation loss"):
            run_skill(nan_model=True)


if __name__ == "__main__":
    unittest.main()
