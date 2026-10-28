Don't add `weights_only=False` for this file. That error is the safety check working, not a compatibility bug: since PyTorch 2.6, `torch.load` defaults to `weights_only=True`, a restricted unpickler that refuses anything outside an allowlist of tensor and container types. Your checkpoint contains some other pickled object, and `weights_only=False` switches back to full unpickling, which can execute arbitrary code embedded in the file. A forum post is not a source you can vouch for, which is exactly the case the message warns about.

Safer options:
- Get the weights from the original publisher or a trusted registry, and verify a published SHA-256 checksum.
- Ask the publisher for a `safetensors` export or a plain `state_dict` of tensors, which loads fine with `weights_only=True`. If you can't get one, don't use this file.
- Only once provenance is established, allowlist the specific classes the file needs with `torch.serialization.add_safe_globals` instead of disabling the check.

Downgrading PyTorch to get the old default back is the same bypass in disguise.
