Not yet - "it ran without errors" doesn't mean the model got much smaller or faster.

`quantize_dynamic` only converts the module types you list, here `nn.Linear`. ResNet-50 has a single Linear layer (the final `fc`); all the convolutions stay fp32. For torchvision's ResNet-50 that shrinks the serialized state_dict from about 102.5 MB to 96.4 MB - roughly 6% smaller, not 4x - and latency barely moves because the conv layers do almost all the work.

Before shipping anything, benchmark the fp32 baseline and every candidate on the actual edge device:

```python
baseline = benchmark(model, sample_input)            # median latency + serialized size
candidate = benchmark(quantized_model, sample_input)
acc_fp32 = eval_accuracy(model.eval(), test_loader)
acc_int8 = eval_accuracy(quantized_model, test_loader)
```

Measure size from the serialized state_dict, not `sum(p.numel() ...)` over `parameters()` - quantized layers keep their int8 weights in packed buffers, so that sum reports almost nothing. Then gate: reject the variant if accuracy drops more than your agreed tolerance below the fp32 baseline, or if it is neither faster nor smaller on the device.

For a conv-heavy model the real gains usually come from static post-training quantization with representative calibration data (e.g. ONNX Runtime `quantize_static`, or PyTorch's static flow), quantization-aware training if static PTQ costs too much accuracy, or the device vendor's compiler/runtime (TensorRT etc.). Check that the edge hardware has efficient int8 kernels; on some CPUs int8 is not faster than fp32.
