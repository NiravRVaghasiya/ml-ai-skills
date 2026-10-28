---
name: python-for-ml
display_name: Python for ML (NumPy & Pandas)
description: >
  Use when the user wants to write idiomatic numpy/pandas code, vectorize a
  slow Python loop, or debug unexpected array/DataFrame behavior.
  Trigger phrases: "vectorize this loop", "numpy broadcasting rules", "speed
  up my pandas code", "why is my pandas apply so slow", "how do I reshape
  this array". NOT for statistical analysis of the resulting data (see
  statistics-for-ml) and NOT for building a modeling-ready preprocessing
  pipeline (see data-preprocessing).
type: reference
domain: foundations
level: beginner
lifecycle: stable
risk_level: low
evidence_level: official-documentation
last_verified: 2026-10-27
capabilities:
  - numpy-broadcasting-rules
  - vectorization-vs-loops
  - view-vs-copy-semantics
  - pandas-chained-indexing-fixes
  - dtype-overflow-debugging
  - pandas-vectorized-groupby
requires:
conflicts:
related:
  - data-preprocessing
  - feature-engineering
  - ml-math-essentials
inputs: A numpy/pandas code snippet or performance problem (slow loop, unexpected shape, chained-indexing warning) to vectorize or debug.
outputs: An idiomatic vectorized numpy/pandas rewrite, or an explanation of the broadcasting/view/dtype mechanism causing the observed bug.
version_constraints:
  - "numpy>=1.17: np.random.default_rng Generator API (executed against numpy 2.4.4 on 2026-10-27; 1.17 lower bound not verified)"
  - "numpy>=2.0: default integer is int64 on 64-bit Windows, int32 before 2.0 (NumPy 2.0.0 release notes; int64 executed against numpy 2.4.4 on Windows on 2026-10-27)"
  - "pandas>=2.0: mode.copy_on_write chained-assignment semantics, opt-in in 2.x and always on from 3.0 (3.0 per pandas 3.0.0 release notes; 2.x behaviour executed against pandas 2.2.3 on 2026-10-27; 2.0 lower bound not verified)"
---

## Overview
The idioms and gotchas of numpy and pandas that separate fast, correct
numerical Python from code that silently does the wrong thing or falls back
to a slow Python-level loop. This is the implementation substrate underneath
almost every other skill in this library — read it when the question is
"how do I express this computation efficiently in numpy/pandas," not "what
should this computation be."

## Key Concepts
- **`ndarray` and dtype.** A numpy array is a fixed-dtype, contiguous block
  of memory — this is what makes vectorized ops fast (SIMD, no per-element
  Python object overhead). Mixed inputs are coerced to one dtype: `[1, 2.5]`
  upcasts to `float64`, `[1, "a"]` becomes a fixed-width string (`<U21`), and
  `None` or arbitrary Python objects force `dtype=object`, which loses all of
  that speed; check `arr.dtype` when something is unexpectedly slow or wrong.
- **Broadcasting.** Numpy aligns array shapes from the *trailing* dimension:
  shapes are compatible if, for each dimension (right to left), they're equal
  or one of them is 1. `(n, d) + (d,)` broadcasts the vector across every
  row; `(n, 1) + (1, d)` produces an `(n, d)` outer-product-like result. Most
  "why is my output huge/wrong shape" bugs are a broadcasting mismatch.
- **Vectorization vs. Python loops.** `for` loops over array elements run at
  Python speed (often one to two orders of magnitude slower, depending on the
  operation and array size); pushing the loop into numpy/pandas C
  internals (`arr * 2`, `df.col.sum()`, boolean masks) is the single biggest
  performance lever available without leaving pure Python.
- **Views vs. copies.** Basic slicing (`arr[1:5]`) returns a *view* — mutating
  it mutates the original. Fancy indexing (`arr[[1,3,5]]`) and boolean masks
  return *copies*. Use `.copy()` explicitly whenever you need to guarantee
  independence, and `np.shares_memory(a, b)` to check whether two arrays
  overlap — not `b.base is not None`, which is also true for some copies
  (e.g. `a[:, [0, 1]]`).
- **pandas Series/DataFrame vectorized ops.** Prefer `df["a"] + df["b"]`,
  `df.groupby("key")["val"].sum()`, and boolean masks (`df[df["x"] > 0]`) over
  `.apply()`/`.iterrows()` — the former call optimized C/Cython code, the
  latter are Python-level loops in disguise.
- **`np.where` / `np.select` / `pd.merge`.** `np.where(cond, a, b)` is a
  vectorized ternary; `np.select([cond1, cond2], [val1, val2], default)`
  generalizes it to multiple conditions; `pd.merge` is the vectorized
  equivalent of a manual row-by-row lookup join.

```python
import numpy as np
import pandas as pd

# Broadcasting: normalize every row of a (n, d) matrix by its own mean/std
X = np.random.default_rng(0).normal(size=(1000, 5))
mean, std = X.mean(axis=1, keepdims=True), X.std(axis=1, keepdims=True)
# Constant row -> 0, not NaN or +/-1. Test constancy exactly with ptp: rounding can
# leave a constant row's std at ~1e-14 instead of 0, and `std == 0` would miss it.
const = np.ptp(X, axis=1, keepdims=True) == 0
X_norm = np.where(const, 0.0, (X - mean) / np.where(const, 1.0, std))

# Vectorized instead of a Python loop over rows
df = pd.DataFrame({"category": ["a", "b", "a", "c"], "value": [10, 20, 30, 40]})
totals = df.groupby("category")["value"].sum()          # fast, C-level
tier = np.select(
    [df["value"] < 15, df["value"] < 35],
    ["low", "mid"],
    default="high",
)
df["tier"] = tier
```

## Gotchas
- **Chained assignment may not write back.** `df[df.x > 0]["y"] = 1` assigns
  into an intermediate object: in pandas 2.x without Copy-on-Write, whether
  `df` changes depends on whether that intermediate is a view (usually with a
  `SettingWithCopyWarning`); under Copy-on-Write (the only mode from
  pandas 3.0) `df` never changes. Use
  `df.loc[df.x > 0, "y"] = 1` — a single indexing call that mutates `df`.
- **`.apply()` is not vectorization.** `df["col"].apply(some_func)` still
  calls `some_func` once per row in a Python-level loop — it's more readable
  than `.iterrows()` but not fundamentally faster. Reach for a truly vectorized
  expression, `np.where`/`np.select`, or `.map()` with a dict for lookups first.
- **Unintended broadcasts fail silently, not loudly.** Incompatible shapes
  raise, but `(n,)` vs. `(n, 1)` vs. `(1, n)` are compatible and combine into
  different (often unintended) shapes — `(n,) + (n, 1)` is `(n, n)` — without
  an error. Always check `.shape` after a broadcasted op you didn't expect to
  be tricky.
- **Fixed-width integer overflow wraps silently.** Elementwise int arithmetic
  wraps around without an error (adding 1 to an int32 array holding `2**31 - 1`
  gives `-2**31`); reductions like `.sum()` accumulate small ints in the platform
  integer instead. Before NumPy 2.0 that default integer was `int32` on Windows
  (`int64` elsewhere), so the same code overflowed on one OS only. Be explicit
  about `dtype` in numeric-heavy code.
- **Mutating a subset may mutate the source.** In numpy, writing into a
  slice view always changes the original array. In pandas 2.x without
  Copy-on-Write, `sub = df.iloc[:5]; sub.iloc[0, 0] = 0` can alter `df` (with a
  `SettingWithCopyWarning`); under Copy-on-Write it never does. Use `.copy()`
  when you intend an independent subset — it behaves the same on every version.

## References
- [NumPy — Broadcasting](https://numpy.org/doc/stable/user/basics.broadcasting.html) — the authoritative rules, worth memorizing.
- [pandas — Indexing and selecting data](https://pandas.pydata.org/docs/user_guide/indexing.html) — covers `.loc`/`.iloc`, views vs. copies, and chained-indexing pitfalls.
- [pandas — Enhancing performance](https://pandas.pydata.org/docs/user_guide/enhancingperf.html) — official guidance on replacing `.apply()`/loops with vectorized code.
