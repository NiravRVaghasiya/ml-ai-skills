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

## Evidence tier vs. verification kind

These are two separate questions:

- **`evidence_level`** (frontmatter, above) answers *what kind of source backs
  this claim*: a paper, vendor docs, consensus, a rule of thumb.
- **Verification kind** (docs/FRESHNESS.md, `VERIFICATION_KINDS` in
  `scripts/check_freshness.py`) answers *how someone checked it*:
  `local-version-check`, `automated-doc-check`, `runtime-test`, `human-review`,
  or `unverified`.

A skill can be `official-documentation` tier and still `unverified`, if nobody
has re-read the docs page since it was written. `static-pattern` (the regex
scan) is never a verification kind for a claim. It only finds known-dead
APIs, and finding none proves nothing.

## Claims registry: `docs/claims.json`

When a specific, checkable claim matters (an API's behaviour, a version
boundary, a vendor limit), give it a structured record, not only prose.
`scripts/check_claims.py` validates the file offline.

| Field | Meaning |
|---|---|
| `id` | Stable kebab-case id. `known_stale_patterns[].claim` in `schema/freshness_policy.json` refers to it. |
| `skill` | The skill slug that relies on the claim, or `null` when it backs repo tooling (e.g. a stale-pattern entry). |
| `claim` | One falsifiable sentence. |
| `verification` | `runtime-test` \| `local-version-check` \| `automated-doc-check` \| `human-review` \| `unverified`. |
| `evidence` | What to re-run or re-read. `runtime-test` must name a test module `tests/**/test_*.py[::Class[::method]]` (helpers such as `tests/testutil.py` don't count). The checker parses the file with `ast` and confirms the class is defined at top level and the method sits inside that class. It doesn't run the test. Otherwise a command, or what the reviewer read. |
| `source_url`, `source_accessed` | The primary source and the date it was read (`YYYY-MM-DD`). Set both or neither. Required for `automated-doc-check` and `human-review`. |
| `applies_to` | `{package, api, version}`. `version` is required for `runtime-test` and `local-version-check`: the version it actually ran against. |
| `reviewer` | Required for `human-review`: who read the source. Agents don't count as human reviewers. |
| `status` | `current` \| `review-due` \| `disputed` \| `unverified`. `unverified` status goes with `unverified` verification; an unverified claim can only be `unverified` or `disputed`. |
| `last_reviewed` | When someone last confirmed the record. Older than the freshness policy limit for the claim's skill means `REVIEW_DUE`. Dates may be at most one day after the checker's today, because a record written in a zone ahead of UTC can be "tomorrow" on a UTC CI runner. |
| `notes` | Optional. Say what the evidence does *not* cover. |

Recording a claim as `unverified` is fine and expected. It turns a gap
nobody knew about into one everybody can see. Never upgrade `verification`
without doing the check.

## Human review: claims that can't be automated

Some claims can't be checked by a test or a URL probe: vendor rate limits,
model retirement dates, a regulation's wording, "this page still recommends
X". For those:

- **Who:** a maintainer, or a contributor a maintainer asks. Not an
  automated agent: an agent's reading goes in `notes`, with `verification`
  left at `unverified` until a person confirms it.
- **When:** (a) when the weekly `freshness-scheduled` workflow flags a cited
  URL as broken or inconclusive, or a dist as `excludes-latest`;
  (b) when `check_claims.py` reports a claim `REVIEW_DUE` (the policy age
  limit for its skill passed); (c) when `check_claims.py` reports
  `stale-pattern-unverified` or `stale-pattern-without-claim`: a
  `known_stale_patterns` entry with level `STALE` fails CI for every skill it
  matches, yet rests on a claim nobody has checked; (d) when a skill that
  relies on the claim is edited.
- **How:** read the primary source at `source_url`. If it still supports the
  claim, set `verification: human-review`, `reviewer: <name>`,
  `source_accessed` and `last_reviewed` to today, and `status: current`. If
  it no longer does, set `status: disputed`, say why in `notes`, and fix the
  skill in the same change (or open an issue linked from `notes`). Then run
  `python scripts/check_claims.py`.
- **Where it's recorded:** in `docs/claims.json` itself, in the same commit,
  so `git log -p docs/claims.json` is the audit trail of who confirmed what,
  and when.
- **Bot walls:** some reference URLs answer every automated client with a
  fixed 401/403/429. They stay `inconclusive` and keep the weekly run red. If
  the page opens in a browser and still supports the skill, add it to
  `acknowledged_inconclusive` in `schema/freshness_policy.json` with `url` (or
  `host`), the exact `http_status` codes, a `reason`, and `acknowledged_on`.
  `verify_online.py` then reports it as `acknowledged` (not a finding) until
  the acknowledgement is older than `default_max_age_days`, when it surfaces
  again for re-confirmation. A 404/410 (`broken`), a timeout or a DNS failure
  can't be acknowledged. The two seeded entries (dl.acm.org and a doi.org link
  to MIT Press) were added by an agent after it saw 403 on both HEAD and GET
  on 2026-10-27. No person has confirmed them in a browser yet, and their
  `reason` says so.

## Where evidence issues get tracked

Findings from the audit pass — including anything not fully resolved — are
recorded per-skill in `docs/SKILL-AUDIT.md`, not just fixed silently, so a future
reviewer can see what was checked and what wasn't. Individual checkable claims
and their verification state live in `docs/claims.json` (above).
