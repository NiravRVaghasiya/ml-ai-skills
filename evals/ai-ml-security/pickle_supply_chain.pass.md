Don't load it as-is. A PyTorch checkpoint is a pickle archive, and unpickling can execute arbitrary code embedded in the file - on PyTorch older than 2.6, or with `weights_only=False`, `torch.load()` on an untrusted file is effectively running unreviewed code from an unknown author on your production machine. Since 2.6 the default is `weights_only=True`, which restricts what can be deserialized, but don't rely on that alone for a file from a random link: that restricted path itself had a code-execution bypass in PyTorch <= 2.5.1 (CVE-2025-32434), and it does nothing about a backdoored model.

Before using it:
- Establish provenance: where did it come from, is there a model card, can you get it from a trusted registry or the original publisher, and does a published checksum (SHA-256) match?
- Prefer a non-code-executing format such as safetensors, or load with `weights_only=True` in a sandboxed environment and re-save the weights yourself.
- Evaluate it on your own held-out data before any production use.

Note this is a supply-chain risk (an artifact you didn't produce), which is distinct from data poisoning (corruption of your own training data).
