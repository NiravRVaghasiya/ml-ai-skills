# Routing benchmark

A labelled benchmark for `scripts/router.py`: 359 free-text requests (217 dev,
142 test), each with the skills an expert would load for it, grouped into 199
paraphrase families (114 dev, 85 test). `scripts/eval_router.py` runs every
case through the router's public `route(task, top_k=...)` and reports how often
it loads the right skills, the wrong ones, or skills it should have left alone.

| File | What it is |
|---|---|
| `benchmark.json` | The labelled cases (schema below). |
| `baseline.json` | Regression floor: the **current** router's measured metrics at the top level; earlier measurements (including the pre-improvement keyword router) under `history`. |
| `synonym-audit.json` | Evidence from skill text for every key of the router's synonym map (same-passage rule, with passage counts), and the keys removed for lacking it (`docs/ROUTING.md`, "Synonym map audit"). |
| `../../scripts/eval_router.py` | The metrics harness, the CI gate (`--gate`) and the benchmark validator. |
| `../../tests/test_eval_router.py` | Metric-math unit tests, benchmark well-formedness, and the regression floor. |
| `../../tests/test_router_gate.py` | The CI gate, its base-commit choice and `accepted_regressions`, in throwaway git repositories. |

> **Current state (2026-10-30).** 359 cases: 217 dev and 142 test (39.6%). The coverage
> and split-integrity rules pass, and `eval_router.py` exits 0 on every split (all, dev,
> test). 115 of the cases (78 test, 37 dev) were added on 2026-10-30 by model writers with no
> repository access (see "Methodology"). The held-out results, with HEAD's router and the
> keyword router on the same cases, are in `docs/ROUTING.md`, "Revision 2026-10-29/30".

```bash
python scripts/eval_router.py                       # human-readable tables + misrouted case ids
python scripts/eval_router.py --split test --json   # held-out split, machine-readable
python scripts/eval_router.py --router path/to/candidate_router.py   # A/B a variant
python scripts/eval_router.py --split test --min-task-accuracy 0.8 --max-negative-false-activation 0.2   # thresholds
python scripts/eval_router.py --gate --base-sha "$BASE_SHA" --event-name pull_request --head-sha "$HEAD_SHA"   # the CI gate
```

Exit codes: `0` ok, `1` a `--min-*`/`--max-*` threshold failed (or a `--gate`
check failed), `2` the benchmark file is missing, unreadable or malformed
(invalid JSON, wrong structure, bad slug, duplicate id, coverage below
minimum, a family spanning splits...) or a CLI usage error such as `--top-k 0`
(`--top-k` must be >= 1). The run is deterministic: no randomness, no
network, no model calls.

## Methodology

- **Labelled blind to the router, except `labeller-b`.** The first 203 cases
  (`labeller-a`) were labelled from each skill's front-matter `description`
  (its trigger phrases and `NOT for` clauses) and its `## Overview` only; the
  router's scoring code and the skills' `capabilities` tags were deliberately
  *not* consulted. Caveats: that labeller saw the scoring weights quoted in
  `docs/ROUTING.md` *after* the labels were drafted, and one case (`con-003`)
  was later relabelled for prerequisite consistency. The 41 cases added on
  2026-10-29/30 (`labeller-b`, the router workstream) were written for known
  router failure modes by someone who had read the router, so they are **not**
  blind; 11 of them (`par-050`-`par-058`, `amb-023`, `non-027`) are the probe
  queries of the review of that router change, labelled by `labeller-b`. They
  are all `dev`, and a test asserts that `labeller-b` cases are never held out.
- **Blind-written cases (`blind-writer-1` to `blind-writer-4`, 115 cases).**
  Written on 2026-10-29/30 by four model writers that saw only each skill's
  name, domain and a one-sentence summary: not the repository, the router,
  the descriptions or the `NOT for` clauses. Their tool-use transcripts show
  they read no repository file. A second blind model (`model-reviewer-blind`)
  labelled each case independently (`label_review`). A separate reviewer, who
  could see the existing 244 cases, assigned the families: a case that
  restates a dev request joined that family in dev, and 2 candidates whose
  family would have spanned both splits were dropped (`hw0-013`, `hw0-023`).
  78 went to test and 37 to dev. The 6 membership-inference and
  training-data-extraction cases were adjudicated by the coordinator to
  `data-privacy` expected and `ai-ml-security` acceptable, following
  `ai-ml-security`'s `NOT for` clause, which the writers had not seen. In 4
  more (`hw0-030`, `hw0-033`, `hw0-040`, `hw0-043`) the reviewer's labels
  differed and were settled as each case's `notes` record; the other 105 are
  `agreed`. Writers and reviewers are models, not people.
- **Label review of the original test split.** The 85 cases of the original
  test split (64 still in test, 21 moved to dev) were labelled again by two
  independent blind model reviewers (`model-reviewer-1`, `model-reviewer-2`),
  for `expected` and `acceptable` only (not `forbidden`). 83 matched the labels
  (`adjudication: agreed`). For `amb-017` and `mul-019` the reviewers' sets
  differed but were compatible with the labels, which were kept
  (`adjudicated`). The other 159 dev cases are still `single-labeller`.
- **Categories** (each has at least 15 cases):

  | Category | What it tests | n (dev / test) |
  |---|---|---|
  | `typical` | Plain requests close to a skill's own wording | 62 (46 / 16) |
  | `paraphrase` | The same needs phrased with synonyms and little or no vocabulary shared with the skill name | 82 (61 / 21) |
  | `ambiguous` | Under-specified asks. `expected` is the one skill every reasonable reading needs, and the other readings are `acceptable` | 36 (20 / 16) |
  | `multi-skill` | Requests that genuinely span 2-3 skills (all expected) | 35 (19 / 16) |
  | `no-skill` | Off-topic or general-programming asks (cooking, CSS, git...). Nothing should be selected | 36 (19 / 17) |
  | `conflicting` | The user explicitly excludes a sibling, or the sibling's `NOT for` clause excludes it. That sibling is `forbidden` | 37 (26 / 11) |
  | `adversarial` | Misleading homonyms ("attention span", "model railway", "electrical transformer"), keyword stuffing, prompt-injection-style routing directives, negated keywords | 71 (26 / 45) |

  101 cases expect no skill (41 dev, 60 test): the 36 `no-skill` cases and 65
  `adversarial` ones.

- **Paraphrase families and splits.** Every case has a `family`: cases that
  restate the same request - the same operation on the same kind of input
  with the same goal, allowing different wording, entity names, or one extra
  sub-step - share one; every other case is its own family (its id). Sharing a
  skill, or asking that skill's core task about different data, is not
  enough, and a multi-skill case that contains another case's request as one
  of its parts is a separate family. **No family may span dev and test**
  (`split_integrity_errors()`, enforced by the CLI, the gate and
  `TestBenchmarkWellFormed.test_no_family_spans_dev_and_test`): tuning on one
  wording of a request carries straight to its paraphrase, so a family with
  any dev case is a tuning family.

  The `split` field of each case is authoritative. History: the first 194
  cases were assigned by a fixed per-category index rule (0-based index `i`
  within the category, in file order, goes to `test` when `i % 5 in (1, 3)`);
  nine were added with an explicit split to fill coverage gaps (`typ-041`,
  `par-039`, `par-040`, `con-023`, `adv-025`, `adv-026`, `adv-027` in test;
  `typ-042`, `con-024` in dev). On 2026-10-29 the families were assigned by
  hand (one person, labeller-b) and every family with a dev case was moved
  wholly to dev - never the reverse. **21 test cases moved to dev** (85 -> 64
  test cases); each one's `notes` records the move. The moved cases had been
  scored only in aggregate as part of the test split:

  | moved case | category | family | earlier dev case(s) with the same request |
  |---|---|---|---|
  | `typ-009` | typical | `vectorize-slow-pandas` | `adv-016` |
  | `typ-029` | typical | `log-and-compare-training-runs` | `con-015` |
  | `typ-032` | typical | `nightly-feature-pipeline` | `typ-042`, `con-024` |
  | `typ-034` | typical | `quantize-a-model` | `con-016` |
  | `typ-037` | typical | `anonymize-personal-table` | `con-010` |
  | `typ-041` | typical | `custom-dataset-and-dataloader` | `typ-016`, `par-016`, `con-018` |
  | `par-002` | paraphrase | `new-features-from-existing-columns` | `con-005` |
  | `par-009` | paraphrase | `vectorize-slow-pandas` | `adv-016` |
  | `par-029` | paraphrase | `log-and-compare-training-runs` | `con-015` |
  | `par-032` | paraphrase | `nightly-feature-pipeline` | `typ-042`, `con-024` |
  | `par-034` | paraphrase | `quantize-a-model` | `con-016` |
  | `par-037` | paraphrase | `anonymize-personal-table` | `con-010` |
  | `par-040` | paraphrase | `retrieval-and-groundedness-metrics` | `typ-023`, `par-023`, `con-003` |
  | `con-007` | conflicting | `monitor-deployed-model-for-drift` | `typ-031`, `par-031` |
  | `con-009` | conflicting | `group-fairness-metrics` | `typ-035`, `par-035` |
  | `con-014` | conflicting | `forecast-business-sales` | `typ-026`, `par-026` |
  | `con-017` | conflicting | `fine-tune-text-classifier` | `typ-040` |
  | `con-019` | conflicting | `backprop-and-optimizer-updates` | `typ-011`, `par-011` |
  | `con-023` | conflicting | `warmup-and-clipping-for-unstable-loss` | `typ-015`, `par-015` |
  | `adv-024` | adversarial | `dog-training-reinforcement` | `adv-008` |
  | `adv-026` | adversarial | `impute-encode-tabular` | `typ-001`, `par-001` |

  Judgement calls kept as separate families (so nothing moved): `typ-002` vs
  `con-005` (specific interaction terms and target encoding vs generic new
  features), `con-001` vs `typ-017`/`con-002` ("house format" vs a JSON
  schema), `con-022` vs `typ-008` (difference of two means vs conversion
  rates), `con-004` vs `par-021` (scoring one summary vs a grading harness),
  `adv-023` vs `typ-004`/`typ-039` (k-means on store locations vs customer
  segments), `amb-019` vs `amb-006`, and every multi-skill case. A reviewer may
  merge any of these; doing so moves the test side to dev.

  On 2026-10-30 the 115 blind-written cases were added (78 test, 37 dev),
  which brought the test split to 142 of 359 cases.

  Tune router heuristics against `dev` only, and report `test` as the
  held-out number. Re-examining `test` failures to tweak the router overfits
  it.
- **Coverage** (enforced by `coverage_errors()`): each of the 38 skills
  appears in `expected` at least twice overall and at least once in *each*
  split, every category that has real-task cases has at least one in each
  split, and the test split is 35-45% of cases. These rules pass on the
  current benchmark (2026-10-30).

## Case schema and label guidelines

```json
{ "id": "con-006", "query": "...", "category": "conflicting", "split": "dev",
  "family": "tune-an-already-chosen-model",
  "expected": ["hyperparameter-tuning"], "acceptable": ["model-evaluation"],
  "forbidden": ["supervised-learning"], "notes": "why, if non-obvious",
  "labellers": ["labeller-a"], "adjudication": "single-labeller" }
```

- **`expected`**: skills that MUST be selected. Keep it minimal, listing only
  what an expert would insist on. Empty for `no-skill` cases and for
  adversarial cases with no real ML task.
- **`acceptable`**: skills that are fine to load but not required.
  Selecting them costs nothing in precision, unnecessary activations or
  false activation.
- **`forbidden`**: skills that must NOT be selected. Use this only when
  selecting the skill is *wrong*, not just unnecessary. Valid reasons: the
  user excluded it, its `NOT for` clause excludes this request, or the query
  only matches it through a homonym.
- **Hard prerequisites.** If an expected *or acceptable* skill declares
  `requires`, the prerequisite must be `expected` or `acceptable` and never
  `forbidden` (validator-enforced): today `rag-pipeline` whenever
  `rag-evaluation` is, and `agents-and-tools` whenever `agent-evaluation` is.
- The three lists are pairwise disjoint. `no-skill` cases have empty
  `expected` and `acceptable`. `multi-skill` cases need at least 2 expected.
  `conflicting` cases need at least 1 forbidden.
- **`family`** (lowercase kebab-case): see "Paraphrase families and splits".
  A new case that restates an existing request joins its family and takes its
  split.
- **Label provenance.** `labellers` lists who wrote the labels (anonymous ids:
  `labeller-a` wrote the first 203 cases, `labeller-b` the 41 cases of
  2026-10-29/30, `blind-writer-1` to `blind-writer-4` the 115 blind-written
  cases). Reviewer ids starting with `model-` are models, not people.
  `adjudication` is one of:
  - `single-labeller`: one labeller and no review (159 dev cases today);
  - `agreed`: independent reviewers matched the labels (`label_review.agreement`
    must be `"full"` or 1);
  - `adjudicated`: reviewers disagreed and the labels were settled;
  - `disputed`: a disagreement is recorded and not settled.

  Any status other than `single-labeller` needs a `label_review` record, and a
  `single-labeller` case must not have one:

  ```json
  "label_review": {"reviewers": ["reviewer-1", "reviewer-2"],
                   "agreement": "partial",
                   "unresolved": ["forbidden: is model-evaluation wrong or only unnecessary?"]}
  ```

  `reviewers` is a non-empty list of distinct ids; `agreement` is `"full"`,
  `"partial"`, `"none"` or a number in [0, 1] (for example the share of label
  lists the reviewers matched); `unresolved` lists open disagreements and must
  be non-empty exactly when `adjudication` is `disputed`. These fields are
  optional to `validate_benchmark()` (so a base commit's older benchmark still
  validates in the CI gate) and required on the current benchmark by
  `split_integrity_errors()`.
- Add a `notes` string for any label a reviewer might dispute.
- Changing anything in `benchmark.json` changes its hash, and the
  regression-floor test then fails until you deliberately re-baseline.

## Metric definitions

Every slug the router returns is counted as **selected**, including slugs
whose reasons say `pulled in as a hard prerequisite`. All metrics are computed
per split (`dev`, `test`, `all`), per category, and per split x category
(`split_categories` in `--json`). When a denominator is zero the metric is
`n/a`, not 0 or 1. A **negative** case is one with an empty `expected`, in
any category.

| Metric | Definition (micro-averaged over the cases in scope) |
|---|---|
| `precision` | Σ\|selected ∩ (expected ∪ acceptable)\| / Σ\|selected\| |
| `recall` | Σ\|selected ∩ expected\| / Σ\|expected\| |
| `top1_accuracy` | Share of cases with ≥1 expected skill whose first returned hit is in `expected`, after skipping leading hits that are prerequisites (by reason or by a declared `requires` of another selected skill). An oracle that returns exactly `expected` through `router.expand_with_requires()` scores 1.0. |
| `task_accuracy` | Share of cases where every expected skill is selected, AND no forbidden skill is selected, AND (when `expected` is empty) nothing outside `acceptable` is selected |
| `false_activation_rate` | Share of `no-skill` cases with ≥1 selection (the original metric, kept for continuity) |
| `negative_false_activation_rate` | Share of **negative** cases (every category) that select ≥1 skill outside their `acceptable` list. Gated. |
| `mean_unnecessary_activations` | Mean per case of \|selected − (expected ∪ acceptable)\| |
| `forbidden_selection_rate` | Share of cases with a non-empty `forbidden` list where ≥1 forbidden skill was selected |
| `abstention_rate` | Share of all cases with no selection (coverage = 1 − abstention) |
| `positive_abstention_rate` | Share of cases with ≥1 expected skill where nothing was selected |
| `no_skill_precision` | Of the cases where nothing was selected, the share that are negatives |
| `no_skill_recall` | Of the negatives, the share where nothing was selected |
| `mean_selected` | Mean number of selected skills per case (informational) |
| `n_cases`, `n_families` | Sample sizes. Cases in one family are correlated, so `n_families` is the better guide to how much a number can move. |

- **Intervals.** `intervals` gives, for every proportion above, the
  successes `k`, trials `n` and a 95% Wilson score interval (Wilson 1927,
  z = 1.96). For precision and recall the trials are selections or expected
  labels, which are clustered within cases, and cases are clustered within
  families, so treat the intervals as optimistic.
- **Per skill** (`per_class`): `support` (cases expecting it), `recall`,
  `selected`, `precision` (share of its selections where it was expected or
  acceptable), `forbidden_selected`.
- **Confusion** (`confusion`): rows are expected skills (or `(none)` for a
  negative), columns the skills selected on those cases (or `(none)` when
  nothing was). Multi-label: one case adds to every (expected, selected)
  pair. `top_confusions` lists the most frequent pairs where the selected
  skill was neither expected nor acceptable, plus missed skills (`-> (none)`).

Threshold flags: `--min-precision`, `--min-recall`, `--min-top1`,
`--min-task-accuracy`, `--max-false-activation`,
`--max-negative-false-activation`, `--max-unnecessary`,
`--max-forbidden-rate`. They apply to the `--split` being evaluated, and an
`n/a` metric fails any threshold placed on it.

## The CI gate and its thresholds

`python scripts/eval_router.py --gate` runs, on the test split at the
baseline's `top_k`:

1. **integrity**: `baseline.json`'s `benchmark_sha256` matches `benchmark.json`;
2. **own baseline**: this commit's benchmark validates (per case, coverage and
   family rules) and this commit's router meets every floor in this commit's
   `baseline.json` `metrics.test`;
3. **no regression**: this commit's router, on the **base commit's** benchmark
   (labels carried over: cases expecting a removed skill are dropped with a
   `::warning`, removed slugs leave `acceptable`/`forbidden`, a new hard
   prerequisite becomes acceptable unless the base forbade it), meets the base
   commit's floors. Lowering `baseline.json` or deleting hard cases in the same
   change therefore does not lower the bar.

**Thresholds are the recorded floors, with no tolerance**: every
higher-is-better headline metric (precision, recall, top-1, task accuracy)
must be ≥ its floor and every lower-is-better one (false activation,
**false activation over all negatives**, unnecessary activations, forbidden
rate) ≤ its floor. Routing is deterministic, so any change is a real change in
selections. Gating `negative_false_activation_rate` is what stops a recall
gain bought by activating on more off-topic or homonym text; a base commit
whose baseline predates the metric is simply not gated on it.

The base commit comes from `--base-sha` (`github.event.pull_request.base.sha`
or `github.event.before`), `--event-name` and, for pull requests, `--head-sha`
(`github.event.pull_request.head.sha`, which tells GitHub's test merge commit
from a head-only checkout), resolved by `resolve_base()` (table in
`docs/ROUTING.md`, "CI gate and base commit"). A missing or unrelated explicit
base fails the gate instead of skipping the check. A resolved base without a
`baseline.json`, or no base at all (a root commit with no explicit base),
skips check 3 with a warning ("NOT RUN") and does not fail the gate. CI
(`.github/workflows/ci.yml`, evals-offline) runs `eval_router.py --gate
--base-sha "$ROUTING_BASE_SHA"`, so every metric the base commit's baseline
records is checked against that baseline, on the base commit's benchmark (PR
base or the commit before a push; `docs/QUALITY-GATES.md` "Base commit"). With
51743b0 (the current HEAD) as base that is seven metrics; all eight once the
base commit's baseline records them; the step fails if that value is empty.
Run locally without `--base-sha`, the gate uses the first parent of HEAD
(`HEAD^1`); for uncommitted changes pass `--base-sha HEAD`.

### Accepted regressions

Check 3 has one way out, for a trade-off that a reviewer accepts in the
diff. `baseline.json` may hold an `accepted_regressions` list. Each entry has
exactly these keys:

| key | meaning |
|---|---|
| `base_benchmark_sha256` | SHA-256 (newline-normalised) of the base commit's `benchmark.json` |
| `metric` | a gated metric, e.g. `recall` or `top1_accuracy` |
| `floor` | the base commit's floor for that metric (for the reader; not compared) |
| `accepted_value` | the exact value this commit's router gets on the base benchmark |
| `cases` | the base-benchmark case ids that regressed (non-empty) |
| `reason` | why the trade-off is accepted (at least 20 characters) |

A check-3 failure turns into a `::warning::` only when an entry has the same
base benchmark hash, the same metric and the same value (rounded to 4
decimals). A further drop does not match; neither does another base, so an
entry stops applying once the base commit moves on. A malformed entry fails
the integrity check. Checks 1 and 2 have no such list. The entry is
value-pinned and sits in `baseline.json`, so it is part of the reviewed diff.
`tests/test_router_gate.py` covers it:
`test_explicit_acceptance_of_the_exact_values_passes_with_warnings`,
`test_acceptance_does_not_cover_a_further_drop`,
`test_acceptance_for_another_base_benchmark_is_ignored` and
`test_malformed_acceptance_fails_integrity`.

Current entries (2026-10-30): base `51743b0`'s benchmark (sha256
`297a157e...`), `recall` 0.8571 (floor 0.8701) and `top1_accuracy` 0.7727
(floor 0.7879), cases `amb-007`, `con-004`, `mul-002`. The reason is in
`docs/ROUTING.md`, "Revision 2026-10-29/30". Run on the 2026-10-30 working
tree, `eval_router.py --gate --base-sha HEAD` exits 0 with these two warnings.
That is a local run; no CI run of the gate has been observed.

## Historical baseline: pre-improvement keyword router (2026-10-27)

Kept for comparison; it is `history[0]` in `baseline.json`. Measured on the
original 203-case benchmark (118 dev / 85 test) with `scripts/router.py` at
git `8328c60`, `top_k=5`, `min_score=1.0`, against skill metadata as it stood
on 2026-10-27:

| split | n | precision | recall | top-1 | task acc. | false-activation | unnecessary/query | forbidden rate |
|---|---|---|---|---|---|---|---|---|
| dev | 118 | 0.2090 | 0.8214 | 0.5106 | 0.5339 | 1.0000 | 4.1695 | 0.8148 |
| test | 85 | 0.1978 | 0.8442 | 0.5000 | 0.5529 | 1.0000 | 4.2471 | 0.7273 |
| all | 203 | 0.2043 | 0.8307 | 0.5062 | 0.5419 | 1.0000 | 4.2020 | 0.7755 |

It over-activated on everything (5-7 skills for every off-topic query), it
depended on shared vocabulary (recall 1.0 on `typical`, 0.53 on
`paraphrase`), and it selected a forbidden sibling in 71% of `conflicting`
cases. Current numbers and the before/after comparison are in
`docs/ROUTING.md`.

Re-measured on 2026-10-30 on the current 359-case benchmark (the last
`history` entry, same `benchmark_sha256` as the top-level entry): the same
router code (`8328c60`, passed with `--router`) scoring the current skill
metadata.

| split | n | precision | recall | top-1 | task acc. | false activation (all negatives) | unnecessary/query | forbidden rate |
|---|---|---|---|---|---|---|---|---|
| dev | 217 | 0.2012 | 0.7576 | 0.4261 | 0.4793 | 1.0000 | 4.2258 | 0.5263 |
| test | 142 | 0.1385 | 0.6600 | 0.3537 | 0.3028 | 1.0000 | 4.5986 | 0.5059 |
| all | 359 | 0.1763 | 0.7248 | 0.4031 | 0.4095 | 1.0000 | 4.3733 | 0.5167 |

## Regression floor and re-baselining

`tests/test_eval_router.py::TestRegressionFloor` runs the current router on
the full benchmark at the baseline `top_k`. For every split it asserts that
each higher-is-better metric is ≥ the baseline and each lower-is-better
metric is ≤ it, that the top-level entry records every headline metric, and
that `benchmark.json`'s SHA-256 (newline-normalised) still matches.

After an **intentional** router improvement, a deliberate label change, or a
`SKILL.md` edit that moves the numbers, re-baseline with a new date and note
and explain the change in the commit; move the old top-level entry into
`history` (`--write-baseline` does not preserve it):

```bash
python scripts/eval_router.py --top-k 5 --write-baseline evals/routing/baseline.json \
  --baseline-date YYYY-MM-DD --baseline-note '<what changed and why>'
```

`--write-baseline` validates the benchmark first, and exits 2 if the coverage
rules fail. The current top-level entry (2026-10-30, after the blind-written
cases were added) was written by the CLI; its `command` field has the exact
call. The entry before it was moved into `history` by hand.

`TestRegressionFloor.test_current_floor_not_below_pre_improvement` compares
the top-level entry with the latest PRE-IMPROVEMENT `history` entry that has
the same `benchmark_sha256`. (It used to compare across different
benchmarks.) On the current benchmark it passes: all-splits recall 0.802 vs
the keyword router's 0.725, task accuracy 0.780 vs 0.410, false activation
over all negatives 0.208 vs 1.000. After a benchmark change, re-measure the
keyword router (`8328c60`) with `--router` and add a new `history` entry;
until then the test fails.

An improvement that trades one metric for another will trip the floor on
purpose, so the trade-off is acknowledged when re-baselining instead of
slipping through. Because the router scores skill metadata and bullet
lead-ins, editing a `SKILL.md` can also move these numbers.
