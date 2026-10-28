"""Executes pytorch-patterns/SKILL.md's python blocks against tiny synthetic data.

Injected: ``X_train`` / ``y_train`` (the only inputs the Workflow expects from
the user). Nothing is stubbed.

One substitution: step 1's ``num_workers=4`` -> ``num_workers=0``. The blocks
are ``exec``-ed in a namespace whose module name is not importable, so spawn-
based DataLoader workers (Windows/macOS) cannot unpickle ``TabularDataset`` —
an artefact of the harness, not of the skill (in a real script the class lives
in an importable module behind an ``if __name__ == "__main__":`` guard, as the
Gotchas say).

Regression tests pin the defects found in the audit (each failed against the
pre-fix skill code):

* ``device`` was used by steps 3-5 but never defined -> NameError when the
  Workflow is followed top to bottom; the model was also never moved to it.
* ``TabularDataset`` accepted features/labels of different lengths silently.
* ``pin_memory=True`` warned (and did nothing) on CPU-only machines.
* The forward hook leaked if the forward pass raised (no try/finally).
* ``save_checkpoint`` wrote in place: a crash mid-write destroyed the previous
  good checkpoint. The first fix (temp file + ``os.replace``) then left a
  partial ``<path>.tmp`` behind when ``torch.save`` raised. Resume returned the
  *finished* epoch, so a resumed run repeated it.
"""
from __future__ import annotations

import contextlib
import io
import os
import tempfile
import unittest
import warnings
from unittest import mock

from examples import harness

SLUG = "pytorch-patterns"
NO_WORKERS = {0: {"num_workers=4": "num_workers=0"}}


def make_data(n: int = 256, seed: int = 0):
    import numpy as np

    rng = np.random.default_rng(seed)
    X = rng.normal(size=(n, 20)).astype("float32")
    y = (X[:, 0] > 0).astype("int64") + 2 * (X[:, 1] > 0).astype("int64")   # 4 separable classes
    return X, y


def run_skill(indices=None, namespace=None):
    X, y = make_data()
    ns = {"X_train": X, "y_train": y}
    ns.update(namespace or {})
    with contextlib.redirect_stdout(io.StringIO()):     # step 4 prints a shape
        return harness.run_blocks(SLUG, indices, namespace=ns, replace=NO_WORKERS)


@harness.requires("torch", "numpy")
class PytorchPatternsExampleTest(unittest.TestCase):
    def test_full_workflow_runs_and_trains(self):
        import torch

        torch.manual_seed(0)
        ns = run_skill()   # every block, top to bottom, with no `device` injected
        model, loader = ns["model"], ns["train_loader"]
        device = ns["device"]
        self.assertEqual(next(model.parameters()).device.type, device.type)

        _, acc_before = ns["evaluate"](model, loader, ns["criterion"], device)
        for _ in range(40):
            train_loss = ns["train_one_epoch"](model, loader, ns["optimizer"], ns["criterion"], device)
        val_loss, acc_after = ns["evaluate"](model, loader, ns["criterion"], device)
        self.assertGreater(acc_after, max(acc_before, 0.8))
        self.assertLess(val_loss, 1.0)
        self.assertGreater(train_loss, 0.0)
        self.assertFalse(model.training, "evaluate() leaves the model in eval mode")

        # step 4 captured the first layer's output for a full batch
        self.assertEqual(tuple(ns["activations"]["first_linear"].shape)[1:], (128,))
        self.assertFalse(ns["activations"]["first_linear"].requires_grad)
        self.assertEqual(len(model.net[0]._forward_hooks), 0, "hook must be removed")

        # step 5: checkpoint for epoch 10 resumes at epoch 11
        self.assertEqual(ns["start_epoch"], 11)

    def test_checkpoint_round_trip_restores_weights(self):
        import torch

        ns = run_skill()
        model, optimizer = ns["model"], ns["optimizer"]
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "ckpt.pt")
            ns["save_checkpoint"](path, model, optimizer, epoch=3)
            fresh = ns["MLP"](in_dim=20, hidden_dim=128, num_classes=10)
            fresh_opt = torch.optim.AdamW(fresh.parameters(), lr=1e-3)
            self.assertEqual(ns["load_checkpoint"](path, fresh, fresh_opt, ns["device"]), 4)
            for a, b in zip(model.state_dict().values(), fresh.state_dict().values()):
                self.assertTrue(torch.equal(a.cpu(), b.cpu()))
            self.assertEqual(os.listdir(tmp), ["ckpt.pt"], "no temp file left behind")

    def test_failed_save_keeps_previous_checkpoint(self):
        import torch

        ns = run_skill()
        model, optimizer = ns["model"], ns["optimizer"]
        real_save = torch.save

        def crash_mid_write(obj, f, *a, **kw):
            with open(f, "wb") as fh:          # partial bytes, then the process "dies"
                fh.write(b"\x80garbage")
            raise OSError("disk full")

        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "ckpt.pt")
            ns["save_checkpoint"](path, model, optimizer, epoch=1)
            with mock.patch.object(torch, "save", crash_mid_write), self.assertRaises(OSError):
                ns["save_checkpoint"](path, model, optimizer, epoch=2)
            self.assertIs(torch.save, real_save)
            # the previous checkpoint is still intact and loadable...
            self.assertEqual(ns["load_checkpoint"](path, model, optimizer, ns["device"]), 2)
            # ...and the partial temp file was cleaned up rather than left beside it
            self.assertEqual(os.listdir(tmp), ["ckpt.pt"], "partial .tmp left after a failed save")

    def test_dataset_rejects_mismatched_lengths(self):
        import numpy as np

        ns = run_skill([0])
        with self.assertRaises(ValueError):
            ns["TabularDataset"](np.zeros((5, 2)), np.zeros(4))

    def test_hook_is_removed_even_if_forward_raises(self):
        ns = run_skill([0, 1, 2])
        model = ns["model"]

        class Boom(RuntimeError):
            pass

        def exploding_forward(x):
            model.net[0](x)          # the hook fires...
            raise Boom("forward failed after the hooked layer")

        with mock.patch.object(model, "forward", exploding_forward), self.assertRaises(Boom):
            with contextlib.redirect_stdout(io.StringIO()):
                harness.run_blocks(SLUG, [3], namespace={k: ns[k] for k in ("model", "train_loader", "device", "torch")})
        self.assertEqual(len(model.net[0]._forward_hooks), 0, "hook leaked after a failed forward")

    def test_no_pin_memory_warning_on_cpu_only_machine(self):
        import torch

        if torch.cuda.is_available():
            self.skipTest("pin_memory is meaningful when CUDA is present")
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            ns = run_skill([0])
            next(iter(ns["train_loader"]))
        self.assertEqual([str(w.message) for w in caught if "pin_memory" in str(w.message)], [])


if __name__ == "__main__":
    unittest.main()
