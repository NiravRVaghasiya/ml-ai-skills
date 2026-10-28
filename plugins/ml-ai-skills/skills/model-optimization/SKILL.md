---
name: model-optimization
display_name: Model Optimization
description: >
  Use when the user wants to make a trained model smaller or faster —
  quantization, knowledge distillation, or converting to ONNX for portable,
  optimized inference. Trigger phrases: "quantize this model", "distill this
  model into a smaller one", "convert my model to ONNX", "speed up inference
  latency", "shrink this model for edge deployment". NOT for standing up the
  serving endpoint itself (see model-deployment) or searching hyperparameters
  during training (see hyperparameter-tuning).
type: workflow
domain: mlops
level: advanced
lifecycle: stable
risk_level: medium
evidence_level: official-documentation
last_verified: 2026-10-27
capabilities:
  - post-training-quantization
  - onnx-export-and-runtime-inference
  - static-quantization-calibration
  - knowledge-distillation
  - latency-accuracy-benchmarking
requires:
conflicts:
related:
  - model-deployment
  - training-deep-models
  - hyperparameter-tuning
inputs: A trained model (typically PyTorch) plus a representative sample input and, for static quantization, a calibration dataset.
outputs: A smaller/faster model artifact (quantized weights, an ONNX file, or a distilled student model) with a benchmarked latency/size/accuracy comparison against the fp32 baseline.
version_constraints:
  - "torch>=2.6: torch.ao.quantization.quantize_dynamic, torch.onnx.export(dynamo=True, dynamic_shapes=..., opset_version=18), torch.export.Dim.DYNAMIC (executed against torch 2.13.0+cpu on 2026-10-27; 2.6 lower bound not verified; torch 2.13 emits a DeprecationWarning for torch.ao.quantization recommending torchao)"
  - "onnxscript>=0.7: required by the torch.export-based ONNX exporter (executed against onnxscript 0.7.2 on 2026-10-27; export raises ModuleNotFoundError without it)"
  - "onnxruntime>=1.16: quantize_static, CalibrationDataReader, quant_pre_process (executed against onnxruntime 1.31.1 on 2026-10-27; lower bound not verified)"
---

## Overview
Takes a trained model and reduces its size/latency footprint before it goes behind an
endpoint — quantization, ONNX export, and distillation, in roughly increasing order of
effort and payoff. Every technique here trades some accuracy for speed/size, so the
workflow always benchmarks a baseline first and validates the tradeoff last: the goal is
a model that is measurably faster or smaller *and* still within an acceptable accuracy
band, not just a model that technically ran through a quantization API.

## Workflow
The blocks below build on each other and assume you already have: `model` (a trained
`nn.Module` taking one float tensor), `sample_input` (a representative batch for it),
`test_loader` (yielding `(x, y)` with integer class labels), `calibration_batches` (a list
of `float32` NumPy arrays drawn from production-like inputs), and — for step 5 only —
`teacher`, `student`, `train_loader`, and an `optimizer` over `student.parameters()`.

1. **Benchmark the unoptimized baseline first.** Without this you can't tell whether an
   "optimized" model actually improved anything on your target hardware. Size is measured
   from the serialized `state_dict`, because quantized layers keep their int8 weights in
   packed buffers that `model.parameters()` does not return — summing parameters would
   report a dynamically quantized MLP as ~0 MB.
   ```python
   import io
   import statistics
   import time
   import torch

   def serialized_size_mb(model) -> float:
       buf = io.BytesIO()
       torch.save(model.state_dict(), buf)
       return buf.getbuffer().nbytes / 1e6

   def benchmark(model, sample_input, n=50, warmup=5):
       model.eval()
       timings = []
       with torch.inference_mode():
           for _ in range(warmup):
               model(sample_input)
           for _ in range(n):
               start = time.perf_counter()
               model(sample_input)
               timings.append(time.perf_counter() - start)
       # median is less sensitive than the mean to one-off scheduler hiccups
       return {"latency_ms": round(statistics.median(timings) * 1000, 3),
               "size_mb": round(serialized_size_mb(model), 4)}

   baseline = benchmark(model, sample_input)
   print(baseline)
   ```
2. **Try post-training dynamic quantization first.** No calibration data needed — it's
   the cheapest thing to try. `quantize_dynamic` returns a copy (`inplace=False` by
   default), so `model` stays fp32 for comparison. Recent PyTorch releases emit a
   `DeprecationWarning` for `torch.ao.quantization` pointing to `torchao`; it still works
   in the version listed in `version_constraints`, but check before upgrading.
   ```python
   import torch
   from torch.ao.quantization import quantize_dynamic

   quantized_model = quantize_dynamic(
       model,
       {torch.nn.Linear},          # layer types to quantize
       dtype=torch.qint8,
   )
   # Reloading this state_dict requires re-running quantize_dynamic on a fresh fp32 model first.
   torch.save(quantized_model.state_dict(), "model_int8.pt")
   print(benchmark(quantized_model, sample_input))
   ```
3. **Export to ONNX for a portable, runtime-agnostic artifact,** then check numerical
   parity against the original — a shape check is not enough. ONNX Runtime is often faster
   than eager PyTorch on CPU, but measure it on your hardware rather than assuming it.
   ```python
   # pip install onnx onnxscript onnxruntime   (the torch.export-based exporter needs onnxscript)
   import torch
   from torch.export import Dim

   model.eval()
   torch.onnx.export(
       model, (sample_input,), "model.onnx",
       input_names=["input"], output_names=["output"],
       dynamic_shapes=({0: Dim.DYNAMIC},),   # dynamic batch dim; one entry per forward() argument
       opset_version=18,
       dynamo=True,                           # explicit: the default changed across PyTorch releases
       verbose=False,                         # progress output includes emoji that crash cp1252 (Windows) consoles
   )
   ```
   ```python
   import numpy as np
   import onnxruntime as ort

   session = ort.InferenceSession("model.onnx", providers=["CPUExecutionProvider"])
   x_np = sample_input.detach().cpu().numpy().astype(np.float32)
   onnx_out = session.run(None, {"input": x_np})[0]
   with torch.inference_mode():
       torch_out = model(sample_input).cpu().numpy()
   # fp32 kernels differ slightly between runtimes; tighten/loosen tolerances for your model
   np.testing.assert_allclose(onnx_out, torch_out, rtol=1e-3, atol=1e-5)
   ```
4. **Use static quantization with calibration data when dynamic quantization's accuracy
   loss is too high.** It is often faster than dynamic quantization, but needs
   representative data. The reader must yield `float32` NumPy arrays (ONNX Runtime rejects
   torch tensors) and at least one batch (an empty reader raises "No data is collected").
   ```python
   from onnxruntime.quantization import CalibrationDataReader, QuantType, quantize_static
   from onnxruntime.quantization.shape_inference import quant_pre_process

   class CalibReader(CalibrationDataReader):
       def __init__(self, calib_batches):
           self._batches = [np.asarray(b, dtype=np.float32) for b in calib_batches]
           if not self._batches:
               raise ValueError("calibration needs at least one representative batch")
           self.rewind()
       def get_next(self):
           batch = next(self._iter, None)
           return {"input": batch} if batch is not None else None
       def rewind(self):
           self._iter = iter(self._batches)

   quant_pre_process("model.onnx", "model_prep.onnx")   # shape inference + graph cleanup ORT recommends
   quantize_static(
       model_input="model_prep.onnx",
       model_output="model_int8_static.onnx",
       calibration_data_reader=CalibReader(calibration_batches),
       weight_type=QuantType.QInt8,
   )
   ```
5. **Distill into a smaller student model when quantization alone isn't enough** (e.g.
   an architecture change is needed, not just lower precision).
   ```python
   import torch
   import torch.nn.functional as F

   def distillation_loss(student_logits, teacher_logits, labels, T=4.0, alpha=0.5):
       # T**2 keeps soft-target gradients on the same scale as the hard loss (Hinton et al.)
       soft_loss = F.kl_div(
           F.log_softmax(student_logits / T, dim=-1),
           F.softmax(teacher_logits / T, dim=-1),
           reduction="batchmean",
       ) * (T ** 2)
       hard_loss = F.cross_entropy(student_logits, labels)
       return alpha * soft_loss + (1 - alpha) * hard_loss

   teacher.eval()
   student.train()
   for x, y in train_loader:
       with torch.no_grad():
           teacher_logits = teacher(x)
       student_logits = student(x)
       loss = distillation_loss(student_logits, teacher_logits, y)
       optimizer.zero_grad()
       loss.backward()
       optimizer.step()
   ```
6. **Validate the accuracy/latency tradeoff before shipping.** Every candidate variant is
   measured (accuracy, latency, size) and gated against the fp32 baseline; one that fails
   goes back to step 2/4 with different settings, not straight to prod. The gate rejects
   a variant that loses too much accuracy or is neither faster nor smaller (all cost, no
   gain), and raises explicitly — a bare `assert` is stripped under `python -O` — if the
   variant you intend to ship is rejected. ONNX latency here includes the runtime change,
   not only quantization; benchmark an fp32 ONNX model too if you need to separate them.
   ```python
   import os
   def eval_accuracy(predict_fn, loader) -> float:
       correct = total = 0
       with torch.inference_mode():
           for x, y in loader:
               correct += (predict_fn(x).argmax(dim=-1) == y).sum().item()
               total += y.numel()
       if total == 0:
           raise ValueError("evaluation loader is empty")
       return correct / total

   def onnx_predict(path):
       sess = ort.InferenceSession(path, providers=["CPUExecutionProvider"])
       return lambda x: torch.from_numpy(sess.run(None, {"input": x.numpy().astype(np.float32)})[0])

   def benchmark_onnx(path, sample_input, n=50, warmup=5):
       sess = ort.InferenceSession(path, providers=["CPUExecutionProvider"])
       feed = {"input": sample_input.detach().cpu().numpy().astype(np.float32)}
       for _ in range(warmup):
           sess.run(None, feed)
       timings = []
       for _ in range(n):
           start = time.perf_counter()
           sess.run(None, feed)
           timings.append(time.perf_counter() - start)
       return {"latency_ms": round(statistics.median(timings) * 1000, 3),
               "size_mb": round(os.path.getsize(path) / 1e6, 4)}

   def gate(results, max_drop, baseline_key="fp32_baseline") -> dict[str, str]:
       """Return {variant: reason} for every candidate that fails the gate."""
       base, rejected = results[baseline_key], {}
       for name, cand in results.items():
           if name == baseline_key:
               continue
           if cand["accuracy"] < base["accuracy"] - max_drop:
               rejected[name] = (f"accuracy {cand['accuracy']:.3f} vs baseline "
                                 f"{base['accuracy']:.3f} exceeds the allowed drop")
           elif cand["latency_ms"] >= base["latency_ms"] and cand["size_mb"] >= base["size_mb"]:
               rejected[name] = "neither faster nor smaller than the baseline"
       return rejected

   MAX_ACCURACY_DROP = 0.01   # set from the product's tolerance, not by default
   STATIC_PATH = "model_int8_static.onnx"
   results = {
       "fp32_baseline": {**baseline, "accuracy": eval_accuracy(model.eval(), test_loader)},
       "int8_dynamic": {**benchmark(quantized_model, sample_input),
                        "accuracy": eval_accuracy(quantized_model, test_loader)},
       "int8_static_onnx": {**benchmark_onnx(STATIC_PATH, sample_input),
                            "accuracy": eval_accuracy(onnx_predict(STATIC_PATH), test_loader)},
   }
   rejected = gate(results, MAX_ACCURACY_DROP)
   for name, r in results.items():
       print(name, r, "REJECTED: " + rejected[name] if name in rejected else "")

   SHIP = "int8_dynamic"   # the variant you intend to deploy
   if SHIP in rejected:
       raise RuntimeError(f"{SHIP}: {rejected[SHIP]}")
   ```

## Gotchas
- **Dynamic quantization only speeds up the layers you target** (typically `nn.Linear`)
  — it barely helps convolution-heavy vision models, where static quantization or a
  hardware-specific compiler (TensorRT) is usually needed for a real win.
- **Skipping the baseline benchmark** means you can't tell whether a "faster" model is
  actually faster on the *target* hardware — int8 kernels can be slower than fp32 on
  CPUs/GPUs that lack efficient int8 support.
- **Calibrating static quantization on a narrow or unrepresentative data sample** silently
  tanks accuracy on inputs the calibration set didn't cover — the calibration data must
  mirror the real production input distribution, not just a convenient dev batch.
- **ONNX export and data-dependent control flow** (an `if`/loop whose branch depends on
  tensor values): the legacy tracing exporter (`dynamo=False`) silently bakes in whichever
  branch the sample input took, so the ONNX model returns wrong answers for inputs that
  would take the other branch; the `torch.export`-based exporter (`dynamo=True`) refuses to
  export it instead. Either way, keep the numerical parity check in step 3 — on inputs
  that exercise every branch, not just one sample batch.
- **`torch.onnx.export` defaults changed across releases.** Recent PyTorch defaults to the
  `torch.export`-based exporter, which needs the separate `onnxscript` package (export
  fails with `ModuleNotFoundError` without it) and prefers `dynamic_shapes` over the legacy
  `dynamic_axes`; pass `dynamo=` explicitly so the same script means the same thing on
  every PyTorch version.
- **Distillation quality is capped by the teacher and the temperature `T`.** Distilling
  from a mediocre teacher just produces a faster mediocre model; a poorly tuned `T` (too
  high smooths away useful signal, too low approaches plain cross-entropy) wastes the
  whole exercise.

## References
- [PyTorch quantization docs](https://pytorch.org/docs/stable/quantization.html) — dynamic vs. static vs. QAT quantization APIs.
- [ONNX Runtime quantization docs](https://onnxruntime.ai/docs/performance/model-optimizations/quantization.html) — static/dynamic quantization for ONNX models.
- [Distilling the Knowledge in a Neural Network (Hinton, Vinyals, Dean, 2015)](https://arxiv.org/abs/1503.02531) — the original knowledge-distillation paper.
