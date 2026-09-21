# Reproducibility policy

Applies to any skill whose Workflow trains/evaluates a model. Be precise about
what reproducibility claim is actually being made — overclaiming here is one of
the most common ways ML guidance misleads.

## What a fixed seed does and does not guarantee

Setting `random_state=42` / `torch.manual_seed(0)` etc. makes a **single run on
the same code, same library versions, same hardware, same backend** reproduce
identical results. It does **not** guarantee reproducibility:

- **across different library versions** — an algorithm's internal random-number
  consumption order can change between releases even with the same public API.
- **across different hardware** (CPU vs. GPU, different GPU models) — floating-
  point operations are not strictly associative, and parallel reduction order
  differs by device/kernel, so summed/averaged results can differ in the last
  few bits, which compounds over many training steps.
- **across different backend configurations** — e.g. cuDNN's non-deterministic
  algorithms (enabled by default in many setups for speed) pick different
  kernels run-to-run unless deterministic mode is explicitly enabled (which
  usually costs performance).
- **for any inherently distributed/async computation** — multi-worker
  `DataLoader`s, multi-GPU data-parallel training, and asynchronous data
  pipelines can process examples in a different order run-to-run even with a
  fixed seed, unless you also fix worker seeding and disable relevant async paths.

**Precise claim to make:** "seeding makes THIS run reproducible on THIS
environment," never "seeding guarantees reproducibility." See the
`llm-evaluation` skill's Gotchas for the equivalent point about `temperature=0`
not guaranteeing bit-for-bit determinism for LLM sampling specifically, and
`evals/llm-evaluation/temperature_determinism_misconception.yaml` for the
eval case built around exactly that misconception.

## What to specify, per skill, when reproducibility matters

- **Random seeds** — set them, and set them for every source of randomness
  (numpy, the framework's own RNG, Python's `random`, data-loader workers),
  not just the model's.
- **Environment specification** — package name + version for anything whose
  behavior the workflow depends on (this is exactly what `version_constraints`
  in a skill's frontmatter is for — see docs/SKILL-SPEC.md).
- **Dataset version** — a hash, a fixed snapshot/date, or a pinned dataset
  release tag; "the current data" is not a reproducibility unit.
- **Model version** — for anything wrapping a hosted model behind a name string
  (e.g. a vendor API model ID), note that the string may point to a
  continuously-updated model unless it's a dated/pinned snapshot — see
  `scripts/check_freshness.py`'s pinned-snapshot pattern checks.
- **Evaluation dataset version** — same as dataset version, tracked separately,
  since eval sets and training sets drift independently.
- **Hardware assumptions** — note when a workflow assumes a GPU, a minimum VRAM
  budget, or specific accelerator features (e.g. bf16 support) rather than
  silently failing on CPU-only or older hardware.
- **Deterministic vs. nondeterministic behavior** — say which regime the
  workflow is in and why (e.g. "training uses non-deterministic cuDNN kernels
  for speed; expect run-to-run metric variance of roughly X" — state this as an
  observation to make per-project, not a universal number).

## What this repo does NOT claim

No skill in this repository claims **complete** reproducibility across
environments, and none should. Where a skill's code sets a seed, that is a
best-effort reproducibility aid for a single environment, not a guarantee.
