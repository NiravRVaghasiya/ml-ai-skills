"""Opt-in: the CUDA code paths that the CPU-only example tests cannot reach.

Gate: ``ML_AI_SKILLS_INTEGRATION_GPU=1``, on a machine where ``torch.cuda.is_available()`` is true
(the class fails, rather than skips, when it is not). Needs a CUDA build of torch, not the
``+cpu`` wheel in requirements-examples.txt.

What it tests, with the same injected inputs as the offline tests:

* training-deep-models runs verbatim on CUDA: ``device`` is ``cuda``, ``use_amp`` is on, the
  ``torch.amp.GradScaler("cuda")`` is enabled and has a finite positive scale after training,
  the autocast forward really produces float16 activations, no deprecation warning is raised,
  the run restores ``best.pt`` and learns the toy task (validation loss below chance).
  (tests/examples/test_training_deep_models.py only sees the scaler disabled on CPU.)
* pytorch-patterns step 1 on CUDA: ``pin_memory=torch.cuda.is_available()`` turns pinning on,
  batches come out in page-locked memory, and the ``non_blocking`` copy lands on the GPU.
  (The offline test only checks that pinning is off, without a warning, on CPU.)

Not covered: multi-GPU, bf16, ``torch.compile``, throughput, DataLoader workers > 0.
"""
from __future__ import annotations

import contextlib
import io
import math
import tempfile
import unittest
import warnings
from pathlib import Path

from examples import harness
from integration import gate


def cuda_device() -> str | None:
    import torch

    if not torch.cuda.is_available():
        return f"torch.cuda.is_available() is False (torch {torch.__version__}, CUDA build: {torch.version.cuda})"
    return None


@gate.opt_in("GPU", what="CUDA AMP/GradScaler and pin_memory paths", modules=("torch", "numpy"),
             precondition=cuda_device)
class TestCudaPaths(unittest.TestCase):
    def test_training_deep_models_amp_with_grad_scaler_on_cuda(self):
        import torch

        from examples.test_training_deep_models import SLUG, make_namespace

        with tempfile.TemporaryDirectory() as tmp, warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            with contextlib.redirect_stdout(io.StringIO()):
                ns = harness.run_blocks(SLUG, namespace=make_namespace(), workdir=Path(tmp))
            self.assertTrue((Path(tmp) / "best.pt").exists())
        self.assertEqual(ns["device"].type, "cuda")
        self.assertTrue(ns["use_amp"])
        self.assertTrue(ns["scaler"].is_enabled())
        scale = ns["scaler"].get_scale()
        self.assertTrue(math.isfinite(scale) and scale > 0, scale)
        msgs = [str(w.message) for w in caught]
        self.assertEqual([m for m in msgs if "deprecated" in m.lower() or "not available" in m], [])
        self.assertTrue(math.isfinite(ns["best_val_loss"]))
        self.assertLess(ns["best_val_loss"], math.log(2))
        xb, _ = next(iter(ns["train_loader"]))
        ns["model"].eval()
        with torch.no_grad(), torch.autocast(device_type="cuda", dtype=torch.float16, enabled=ns["use_amp"]):
            out = ns["model"](xb.to(ns["device"]))
        self.assertEqual(out.dtype, torch.float16)

    def test_pytorch_patterns_pins_memory_when_cuda_is_present(self):
        import torch

        from examples.test_pytorch_patterns import run_skill

        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            loader = run_skill([0])["train_loader"]
            xb, yb = next(iter(loader))
        self.assertTrue(loader.pin_memory)
        self.assertTrue(xb.is_pinned() and yb.is_pinned())
        self.assertEqual([str(w.message) for w in caught if "pin_memory" in str(w.message)], [])
        on_gpu = xb.to("cuda", non_blocking=True)
        torch.cuda.synchronize()
        self.assertTrue(on_gpu.is_cuda)
        self.assertTrue(torch.equal(on_gpu.cpu(), xb))


if __name__ == "__main__":
    unittest.main()
