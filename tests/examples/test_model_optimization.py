"""Executable tests for model-optimization/SKILL.md.

Every python block runs verbatim (harness.run_blocks) once, in setUpClass, on a
tiny MLP trained for a few hundred steps on synthetic data — so quantization,
ONNX export, static calibration, distillation and the final gate all execute
for real. Nothing is stubbed: the skill's inputs (``model``, ``sample_input``,
``test_loader``, ``calibration_batches``, ``teacher``/``student``/
``train_loader``/``optimizer``) are injected through ``namespace=`` because the
skill documents them as caller-provided.

Regression tests replay the pre-fix code (model-optimization/SKILL.md at commit
8328c60, quoted verbatim below) to show each defect, then assert the fix:

* size_mb summed ``model.parameters()``, which reports a dynamically quantized
  MLP as 0 MB (its int8 weights live in packed params, not parameters);
* step 6 called ``eval_accuracy``, defined nowhere in the skill (NameError);
* the ONNX step said ``pip install onnx onnxruntime`` but recent torch defaults
  to the torch.export-based exporter, which needs ``onnxscript`` (observed as
  ModuleNotFoundError against torch 2.13.0+cpu before onnxscript was installed;
  not re-tested here because torch caches the import once loaded);
* the legacy tracing exporter silently bakes in a data-dependent branch (the
  Gotcha claims this; checked here);
* the calibration reader passed torch tensors / empty lists straight through.

The first torch.export-based ONNX export is slow (tens of seconds of one-off
compile/import cost on a loaded machine); it runs once per class.
"""
from __future__ import annotations

import contextlib
import io
import os
import tempfile
import unittest
import warnings
from pathlib import Path

from examples import harness

SLUG = "model-optimization"

ORIGINAL_BENCHMARK = '''
import time
import torch

def benchmark(model, sample_input, n=50):
    model.eval()
    with torch.no_grad():
        for _ in range(5):          # warmup
            model(sample_input)
        start = time.perf_counter()
        for _ in range(n):
            model(sample_input)
        latency_ms = (time.perf_counter() - start) / n * 1000
    size_mb = sum(p.numel() * p.element_size() for p in model.parameters()) / 1e6
    return {"latency_ms": round(latency_ms, 2), "size_mb": round(size_mb, 2)}
'''

ORIGINAL_GATE = '''
results = {
    "fp32_baseline": {**baseline, "accuracy": eval_accuracy(model, test_loader)},
    "int8_dynamic": {**benchmark(quantized_model, sample_input),
                      "accuracy": eval_accuracy(quantized_model, test_loader)},
}
'''

def in_dir(path, fn, *a, **kw):
    """os.chdir + restore (contextlib.chdir needs Python 3.11; the repo supports 3.10+)."""
    cwd = os.getcwd()
    os.chdir(path)
    try:
        return fn(*a, **kw)
    finally:
        os.chdir(cwd)


def make_inputs():
    """Synthetic 3-class problem + a briefly trained MLP (the skill's documented inputs)."""
    import numpy as np
    import torch

    torch.manual_seed(0)
    gen = np.random.default_rng(0)
    X = torch.from_numpy(gen.normal(size=(384, 16)).astype(np.float32))
    y = (X[:, 0] > 0).long() + (X[:, 1] > 0.5).long()  # classes 0..2

    model = torch.nn.Sequential(torch.nn.Linear(16, 64), torch.nn.ReLU(), torch.nn.Linear(64, 3))
    opt = torch.optim.Adam(model.parameters(), lr=1e-2)
    for _ in range(200):
        opt.zero_grad()
        torch.nn.functional.cross_entropy(model(X[:256]), y[:256]).backward()
        opt.step()
    model.eval()

    student = torch.nn.Sequential(torch.nn.Linear(16, 8), torch.nn.ReLU(), torch.nn.Linear(8, 3))
    batches = [(X[i:i + 32], y[i:i + 32]) for i in range(0, 256, 32)]
    return {
        "model": model,
        "sample_input": X[:32],
        "test_loader": [(X[i:i + 32], y[i:i + 32]) for i in range(256, 384, 32)],
        "calibration_batches": [X[i:i + 32].numpy() for i in range(0, 256, 64)],
        "teacher": model,
        "student": student,
        "train_loader": batches,
        "optimizer": torch.optim.SGD(student.parameters(), lr=0.1),
    }


@harness.requires("torch", "numpy", "onnx", "onnxruntime", "onnxscript")
class TestModelOptimizationBlocks(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.inputs = make_inputs()
        # Keep the blocks' artifacts (model.onnx, model_int8_static.onnx) for the gate test.
        cls._tmp = tempfile.TemporaryDirectory()
        cls.workdir = Path(cls._tmp.name)
        with warnings.catch_warnings(), contextlib.redirect_stdout(io.StringIO()) as out:
            warnings.simplefilter("ignore")  # torch.ao.quantization / exporter deprecation noise
            cls.ns = harness.run_blocks(SLUG, namespace=dict(cls.inputs), workdir=cls.workdir)
        cls.stdout = out.getvalue()

    @classmethod
    def tearDownClass(cls):
        cls._tmp.cleanup()

    def test_every_block_executes(self):
        blocks = harness.python_blocks(SLUG)
        self.assertFalse([i for i, b in enumerate(blocks) if harness.is_illustrative(b)],
                         "all model-optimization blocks are expected to be runnable")
        self.assertIn("int8_dynamic", self.ns["results"])

    def test_benchmark_reports_real_quantized_size(self):
        base = self.ns["baseline"]
        q = self.ns["benchmark"](self.ns["quantized_model"], self.ns["sample_input"], n=3)
        self.assertGreater(q["size_mb"], 0.0)
        self.assertLess(q["size_mb"], base["size_mb"])  # int8 Linear weights are ~4x smaller

    def test_original_benchmark_reported_zero_size_for_quantized_model(self):
        ns = {}
        exec(ORIGINAL_BENCHMARK, ns)  # noqa: S102 - replaying the pre-fix published code
        self.assertEqual(ns["benchmark"](self.ns["quantized_model"], self.ns["sample_input"], n=2)["size_mb"], 0.0)

    def test_onnx_parity_and_dynamic_batch(self):
        import numpy as np
        import onnxruntime as ort
        import torch

        # The step-3 parity assert already ran inside the block; re-check it, then check the
        # exported graph really has a dynamic batch dim (the in-memory session outlives the
        # harness's temp dir).
        self.assertIsInstance(self.ns["session"], ort.InferenceSession)
        np.testing.assert_allclose(self.ns["onnx_out"], self.ns["torch_out"], rtol=1e-3, atol=1e-5)
        x = torch.randn(5, 16)  # a batch size different from the export sample (32)
        with torch.inference_mode():
            ref = self.inputs["model"](x).numpy()
        got = self.ns["session"].run(None, {"input": x.numpy()})[0]
        np.testing.assert_allclose(got, ref, rtol=1e-3, atol=1e-5)

    def test_static_quantized_model_was_evaluated(self):
        static = self.ns["results"]["int8_static_onnx"]
        self.assertGreaterEqual(static["accuracy"], 0.0)
        self.assertLessEqual(static["accuracy"], 1.0)
        # Measured, not just evaluated (pre-review the static variant had accuracy only).
        self.assertGreater(static["latency_ms"], 0.0)
        self.assertGreater(static["size_mb"], 0.0)

    def test_gate_covers_every_candidate_variant(self):
        """Pre-review, the gate read only results["int8_dynamic"], so a static-ONNX variant
        that lost accuracy (or gained nothing) was never rejected."""
        gate = self.ns["gate"]
        base = {"accuracy": 0.90, "latency_ms": 1.0, "size_mb": 1.0}
        good = {"accuracy": 0.90, "latency_ms": 0.5, "size_mb": 0.3}
        rejected = gate({"fp32_baseline": base, "int8_dynamic": good,
                         "int8_static_onnx": {**good, "accuracy": 0.70},
                         "pruned": {**base}}, max_drop=0.01)
        self.assertEqual(set(rejected), {"int8_static_onnx", "pruned"})
        self.assertIn("allowed drop", rejected["int8_static_onnx"])
        self.assertIn("neither faster nor smaller", rejected["pruned"])

    def test_original_gate_referenced_undefined_eval_accuracy(self):
        ns = {k: v for k, v in self.ns.items() if k != "eval_accuracy"}
        with self.assertRaises(NameError):
            exec(ORIGINAL_GATE, ns)  # noqa: S102

    def test_gate_raises_instead_of_assert(self):
        import torch

        blocks = harness.python_blocks(SLUG)
        gate = blocks[-1]
        self.assertNotIn("assert ", gate)
        # A "quantized" model that is just the fp32 model again: neither faster nor smaller.
        ns = dict(self.ns)
        ns["quantized_model"] = torch.nn.Sequential(*[m for m in self.inputs["model"]])
        ns["benchmark"] = lambda m, x: dict(ns["baseline"])
        code = compile(gate, "gate", "exec")
        with self.assertRaisesRegex(RuntimeError, "neither faster nor smaller"),                 contextlib.redirect_stdout(io.StringIO()):
            in_dir(self.workdir, exec, code, ns)  # noqa: S102 - the skill's gate block

    def test_eval_accuracy_rejects_empty_loader(self):
        with self.assertRaises(ValueError):
            self.ns["eval_accuracy"](self.inputs["model"], [])

    def test_calib_reader_accepts_tensors_rewinds_and_rejects_empty(self):
        import numpy as np
        import torch

        reader = self.ns["CalibReader"]([torch.ones(2, 16), torch.zeros(2, 16)])
        first = reader.get_next()["input"]
        self.assertEqual(first.dtype, np.float32)
        reader.get_next()
        self.assertIsNone(reader.get_next())
        reader.rewind()
        self.assertIsNotNone(reader.get_next())
        with self.assertRaises(ValueError):
            self.ns["CalibReader"]([])

    def test_distillation_loss_is_finite_and_step_ran(self):
        import torch

        logits = torch.randn(4, 3)
        loss = self.ns["distillation_loss"](logits, logits.clone(), torch.tensor([0, 1, 2, 0]))
        self.assertTrue(torch.isfinite(loss))
        self.assertTrue(self.inputs["student"].training)  # step 5 put the student in train mode


@harness.requires("torch", "numpy", "onnxruntime")
class TestOnnxExporterClaims(unittest.TestCase):
    def test_legacy_exporter_silently_bakes_data_dependent_branch(self):
        import numpy as np
        import onnxruntime as ort
        import torch

        class Branchy(torch.nn.Module):
            def forward(self, x):
                return x * 2 if x.sum() > 0 else x - 100

        m = Branchy().eval()
        with tempfile.TemporaryDirectory() as tmp, warnings.catch_warnings():
            warnings.simplefilter("ignore")
            path = f"{tmp}/branchy.onnx"
            torch.onnx.export(m, (torch.ones(2, 3),), path, input_names=["input"], dynamo=False)
            neg = -np.ones((2, 3), dtype=np.float32)
            onnx_val = ort.InferenceSession(path, providers=["CPUExecutionProvider"]).run(None, {"input": neg})[0]
        self.assertEqual(float(onnx_val[0, 0]), -2.0)                          # traced branch
        self.assertEqual(float(m(torch.from_numpy(neg))[0, 0]), -101.0)       # true answer


if __name__ == "__main__":
    unittest.main()
