# Evidence discipline

Every skill's frontmatter carries `evidence_level` (see docs/SKILL-SPEC.md for the
five-tier scale). This document is the policy behind that field: how to classify a
claim, and how to phrase the ones that turn out to be heuristics rather than laws.

## The five tiers, and how to tell them apart

| Tier | Ask yourself | Example from this repo |
|---|---|---|
| `primary` | Is this claim traceable to one specific paper/standard? | attention-mechanisms' softmax(QKᵀ/√dₖ)V formula → Vaswani et al. 2017 |
| `official-documentation` | Is the vendor/framework's own doc page the source of truth here? | data-preprocessing's `OneHotEncoder(handle_unknown="ignore")` behavior → scikit-learn docs |
| `established-practice` | Is this consensus practitioner knowledge with no single citable origin? | supervised-learning's "always fit a dumb baseline first" |
| `heuristic` | Does this claim have known exceptions / is it a rule of thumb? | "above roughly 50 categories, consider target encoding" |
| `opinion` | Is this a preference with no objective backing either way? | (rare in this repo; flag these explicitly if you add one) |

`evidence_level` in frontmatter records the **dominant** tier for the skill as a
whole. It does not mean every sentence is independently verified — see
docs/SKILL-SPEC.md "Known limitations" item 3.

## Rewriting a heuristic that was stated as a law

Three real examples fixed during the Phase 15 audit pass (kept here as the
canonical before/after reference for future edits):

1. **Before:** "encoding (see feature-engineering) above ~50 unique values."
   (data-preprocessing) — reads as a hard threshold.
   **After:** "As a rule of thumb — not a hard threshold — once a column has on
   the order of dozens of unique values, start evaluating target/ordinal
   encoding... the right cutoff depends on row count and downstream model."
   **Why this is better:** names the actual variables the real threshold depends
   on (row count, model type) instead of asserting a number as if it were derived
   from theory.

2. **Before:** "Call your model/pipeline here with temperature=0 for
   reproducibility." (llm-evaluation, code comment) — implies a guarantee.
   **After:** "temperature=0 selects the top-probability token at each step,
   which makes output *close to* deterministic — it is not a full
   reproducibility guarantee (see Gotchas below and docs/REPRODUCIBILITY.md)."
   **Why this is better:** states the actual mechanism (top-probability
   selection) rather than the desired outcome, so the reader can reason about
   when it breaks down (ties, floating-point non-associativity, provider-side
   updates) instead of trusting a label.

3. **Before:** "Non-zero temperature makes eval runs flaky and non-
   reproducible. Always pin temperature=0..." (llm-evaluation, Gotcha) — implies
   temperature=0 is the fix that removes non-reproducibility entirely.
   **After:** names the specific residual nondeterminism sources (floating-point
   non-associativity across batch/hardware/kernel, backend routing, provider-
   side snapshot updates behind a fixed model string) and says to treat these
   evals as "approximately, not exactly, reproducible."
   **Why this is better:** a reader who hits a reproducibility bug after
   following the original advice would conclude the advice was wrong; the
   rewritten version predicts the bug instead of being contradicted by it.

## The test for "is this heuristic phrased conditionally enough"

Ask: if a reader followed this claim literally as a universal rule and hit a
counterexample, would they conclude the skill was *wrong*, or would the skill's
own wording have already told them this case might be an exception? If the
former, rewrite it. This is a strictly more useful test than "does it use hedge
words," since hedge words without a stated reason ("usually", "often", with no
explanation of when it fails) are not meaningfully better than the false-certainty
version — they just read less confidently while conveying the same lack of
guidance about which situation you're actually in.

## Where evidence issues get tracked

Findings from the audit pass — including anything not fully resolved — are
recorded per-skill in `docs/SKILL-AUDIT.md`, not just fixed silently, so a future
reviewer can see what was checked and what wasn't.
