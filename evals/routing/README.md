# Routing benchmark

A labelled benchmark for `scripts/router.py`: 203 free-text requests, each with
the skills a correct human expert would load for it. `scripts/eval_router.py`
runs every case through the router's public `route(task, top_k=...)` and
reports how often it loads the right skills, the wrong ones, or skills it
should have left alone.

| File | What it is |
|---|---|
| `benchmark.json` | The labelled cases (schema below). |
| `baseline.json` | Regression floor: the **current** router's measured metrics at the top level; earlier measurements (including the pre-improvement keyword router) under `history`. |
| `../../scripts/eval_router.py` | The metrics harness / CI gate. |
| `../../tests/test_eval_router.py` | Metric-math unit tests, benchmark well-formedness, and the regression floor. |

```bash
python scripts/eval_router.py                       # human-readable tables + misrouted case ids
python scripts/eval_router.py --split test --json   # held-out split, machine-readable
python scripts/eval_router.py --router path/to/candidate_router.py   # A/B a variant
python scripts/eval_router.py --split test --min-task-accuracy 0.8 --max-false-activation 0.0   # threshold flags (example)
```

The CI gate is the "Routing benchmark (test split) must not regress" step of the
`evals-offline` job in `.github/workflows/ci.yml`, and it is stricter than any single
example above. On `--split test` at the baseline `top_k`, it passes all seven floors from
`baseline.json`'s `metrics.test` as `--min-*`/`--max-*` flags, checks `benchmark_sha256`
against `benchmark.json`, and repeats the run against the parent commit's benchmark and
baseline. For that parent run, cases that expect a skill this commit removed are dropped (with
a warning), and the step comment describes how the parent's other labels are carried over;
a parent benchmark that still does not validate fails the step.

Exit codes: `0` ok, `1` a `--min-*`/`--max-*` threshold failed, `2` the
benchmark file is missing, unreadable or malformed (invalid JSON, wrong
structure, bad slug, duplicate id, coverage below minimum...) or a CLI usage
error such as `--top-k 0` (`--top-k` must be >= 1).
The run is deterministic: no randomness, no network, no model calls.

## Methodology

- **Labelled blind to the router.** Labels were written from each skill's
  front-matter `description` (its trigger phrases and `NOT for` clauses) and
  its `## Overview` only. The router's scoring code and the skills'
  `capabilities` tags were deliberately *not* consulted, so the benchmark
  measures "does the router pick what an expert would", not "does the router
  agree with itself". Keep it that way when adding cases.
  Caveats on that claim: the labeller saw the scoring weights quoted in
  `docs/ROUTING.md` *after* the labels were drafted; one case (`con-003`) was
  later relabelled, for prerequisite consistency only; and all labels come
  from a single labeller and have not been independently reviewed.
- **Categories** (each has at least 15 cases):

  | Category | What it tests | n |
  |---|---|---|
  | `typical` | Plain requests close to a skill's own wording | 42 |
  | `paraphrase` | The same needs phrased with synonyms and little or no vocabulary shared with the skill name | 40 |
  | `ambiguous` | Under-specified asks. `expected` is the one skill every reasonable reading needs, and the other readings are `acceptable` | 22 |
  | `multi-skill` | Requests that genuinely span 2-3 skills (all expected) | 26 |
  | `no-skill` | Off-topic or general-programming asks (cooking, CSS, git...). Nothing should be selected | 22 |
  | `conflicting` | The user explicitly excludes a sibling, or the sibling's `NOT for` clause excludes it. That sibling is `forbidden` | 24 |
  | `adversarial` | Misleading homonyms ("attention span", "model railway", "electrical transformer"), keyword stuffing, prompt-injection-style routing directives, negated keywords | 27 |

- **Splits.** The `split` field of each case is authoritative. The first 194
  cases were assigned by a fixed per-category index rule (0-based index `i`
  within the category, in file order, goes to `test` when `i % 5 in (1, 3)`).
  That rule left four skills (`ai-ml-security`, `pytorch-patterns`,
  `rag-evaluation`, `training-deep-models`) with no `test` case, left
  `data-pipelines` with no `dev` case, and put every adversarial case with a
  real task (negation, injection hiding a task) in `dev`. Nine cases were
  therefore added at the end of their categories with an explicit split:
  seven `test` cases (`typ-041`, `par-039`, `par-040`, `con-023`, `adv-025`,
  `adv-026`, `adv-027`) and two `dev` cases (`typ-042`, `con-024`). The test
  additions were written fresh, so no router tuning can have seen them.
  Existing cases were not moved between splits, because a parallel router
  workstream may already have tuned against `dev`. Totals: 118 dev and 85
  test cases (41.9% test). The split is still not stratified by skill or
  sub-type beyond the coverage checks below. Tune router heuristics against
  `dev` only, and report `test` as the held-out number. Re-examining `test`
  failures to tweak the router overfits it.
- **Coverage** (enforced by `coverage_errors()`): each of the 38 skills
  appears in `expected` at least twice overall and at least once in *each*
  split, and every category that has real-task cases has at least one in
  each split (so recall and top-1 are defined for it on `test`).

## Case schema and label guidelines

```json
{ "id": "con-006", "query": "...", "category": "conflicting", "split": "dev",
  "expected": ["hyperparameter-tuning"], "acceptable": ["model-evaluation"],
  "forbidden": ["supervised-learning"], "notes": "why, if non-obvious" }
```

- **`expected`**: skills that MUST be selected. Keep it minimal, listing only
  what an expert would insist on. Empty for `no-skill` cases and for
  adversarial cases with no real ML task.
- **`acceptable`**: skills that are fine to load but not required (a
  closely related sibling, or an alternative reading of an ambiguous ask).
  Selecting them costs nothing in precision or unnecessary-activation counts.
- **`forbidden`**: skills that must NOT be selected. Use this only when
  selecting the skill is *wrong*, not just unnecessary. Valid reasons: the
  user excluded it, its `NOT for` clause excludes this request, or the query
  only matches it through a homonym. A skill that is merely irrelevant
  belongs in none of the lists, and loading it still counts as an
  unnecessary activation.
- **Hard prerequisites.** If an expected *or acceptable* skill declares
  `requires`, the router always loads that prerequisite too, so the
  prerequisite must be `expected` or `acceptable` and never `forbidden`.
  The validator enforces this. Today that means `rag-pipeline` whenever
  `rag-evaluation` is expected or acceptable, and `agents-and-tools` whenever
  `agent-evaluation` is.
- The three lists are pairwise disjoint. `no-skill` cases have empty
  `expected` and `acceptable`. `multi-skill` cases need at least 2 expected.
  `conflicting` cases need at least 1 forbidden.
- Add a `notes` string for any label a reviewer might dispute (see the
  `ambiguous` cases).
- Changing labels changes `benchmark.json`'s hash, and the regression-floor
  test then fails until you deliberately re-baseline (see below).

## Metric definitions

Every slug the router returns is counted as **selected**, including slugs
whose reasons say `pulled in as a hard prerequisite`, because the consumer
loads those into context too. All metrics are computed per split (`dev`,
`test`, `all`) and per category. When a metric's denominator is zero it is
reported as `n/a`, not as 0 or 1.

| Metric | Definition (micro-averaged over the cases in scope) |
|---|---|
| `precision` | Σ\|selected ∩ (expected ∪ acceptable)\| / Σ\|selected\| |
| `recall` | Σ\|selected ∩ expected\| / Σ\|expected\| |
| `top1_accuracy` | Share of cases with ≥1 expected skill whose first returned hit is in `expected`, after skipping leading hits that are prerequisites. A hit counts as a prerequisite if its reasons say `pulled in as a hard prerequisite` or it is a declared `requires` of another selected skill. The router places prerequisites before their dependent even when they scored on their own, and without the skip a perfect router would top out at about 0.95. A prerequisite that is itself expected still counts as a top-1 match. An oracle router that returns exactly `expected`, passed through `router.expand_with_requires()`, scores 1.0. |
| `task_accuracy` | Share of cases where every expected skill is selected, AND no forbidden skill is selected, AND (when `expected` is empty) nothing outside `acceptable` is selected. For `no-skill` cases that means nothing selected. |
| `false_activation_rate` | Share of `no-skill` cases with ≥1 selection. Adversarial cases with an empty `expected` are not counted here; a selection on one of them shows up as a failed adversarial case in `task_accuracy` |
| `mean_unnecessary_activations` | Mean per case of \|selected − (expected ∪ acceptable)\| |
| `forbidden_selection_rate` | Share of cases with a non-empty `forbidden` list where ≥1 forbidden skill was selected |
| `mean_selected` | Mean number of selected skills per case (informational, not gated) |

Threshold flags: `--min-precision`, `--min-recall`, `--min-top1`,
`--min-task-accuracy`, `--max-false-activation`, `--max-unnecessary`,
`--max-forbidden-rate`. They apply to the `--split` being evaluated, and an
`n/a` metric fails any threshold placed on it.

## Historical baseline: pre-improvement keyword router (2026-10-27)

Kept for comparison; it is `history[0]` in `baseline.json`. Current router metrics and the
before/after comparison are in `docs/ROUTING.md`.

Measured with `scripts/router.py` unmodified from git HEAD `8328c60`, using
the default `top_k=5` and `min_score=1.0`, against skill metadata as it stood
in the working tree on 2026-10-27. The old router scores whatever skill
metadata is in the tree, so re-running it after later `SKILL.md` edits moves
these numbers (on 2026-10-28, after audit-round skill edits: task accuracy dev
0.5424, test 0.5294, all 0.5369). The table is the recorded baseline. Command
(the exact invocation, also stored in `baseline.json`):

```bash
python scripts/eval_router.py --top-k 5 --write-baseline evals/routing/baseline.json \
  --baseline-date 2026-10-27 --baseline-note '<note text stored in baseline.json>'
```

These numbers were re-measured on 2026-10-27 after review fixes: the nine
added cases, prerequisite labels added to `amb-002` and `amb-003`, and the
prerequisite-aware top-1 definition. They replace the first 194-case
measurement from the same day.

| split | n | precision | recall | top-1 | task acc. | false-activation | unnecessary/query | forbidden rate |
|---|---|---|---|---|---|---|---|---|
| dev | 118 | 0.2090 | 0.8214 | 0.5106 | 0.5339 | 1.0000 | 4.1695 | 0.8148 |
| test | 85 | 0.1978 | 0.8442 | 0.5000 | 0.5529 | 1.0000 | 4.2471 | 0.7273 |
| all | 203 | 0.2043 | 0.8307 | 0.5062 | 0.5419 | 1.0000 | 4.2020 | 0.7755 |

Per category (all splits, from `python scripts/eval_router.py`):

| category | n | precision | recall | top-1 | task acc. | unnecessary/query |
|---|---|---|---|---|---|---|
| typical | 42 | 0.273 | 1.000 | 0.857 | 1.000 | 3.809 |
| paraphrase | 40 | 0.129 | 0.525 | 0.175 | 0.525 | 4.575 |
| ambiguous | 22 | 0.331 | 0.727 | 0.227 | 0.727 | 3.591 |
| multi-skill | 26 | 0.440 | 0.927 | 0.846 | 0.846 | 2.885 |
| no-skill | 22 | 0.000 | n/a | n/a | 0.000 | 5.682 |
| conflicting | 24 | 0.216 | 0.917 | 0.375 | 0.292 | 4.083 |
| adversarial | 27 | 0.050 | 0.833 | 0.333 | 0.074 | 4.926 |

What the baseline shows:

- **It over-activates on everything.** The router returned 5 to 7 skills
  for every one of the 22 off-topic queries ("banana bread", "center a
  div"), so the false-activation rate is 100%. On average it loads about
  4.2 skills per query that no label asked for.
- **It depends on shared vocabulary.** Recall is 1.0 on `typical` but 0.53
  on `paraphrase`.
- **It ignores exclusions.** It selected a forbidden sibling in 71% of
  `conflicting` cases. Homonyms ("attention span", "cluster headaches")
  reliably trigger the matching skill.
- `typical` task accuracy is 1.0 only because `typical` cases have no
  forbidden lists and top-5 selection is generous. Precision on the same
  cases is 0.27.

## Regression floor and re-baselining

`tests/test_eval_router.py::TestRegressionFloor` runs the current router on
the full benchmark at the baseline `top_k`. For every split it asserts that
each higher-is-better metric (precision, recall, top-1, task accuracy) is
≥ the baseline and each lower-is-better metric is ≤ it. It also asserts that
`benchmark.json`'s SHA-256 (newline-normalised) still matches the one
recorded in `baseline.json`.

After an **intentional** router improvement, or a deliberate label change,
re-run the command above with a new date and note, and explain the change in
the commit. An improvement that trades one metric for another (for example,
less over-activation at the cost of some recall) will trip the floor on
purpose. That forces the trade-off to be acknowledged when re-baselining
instead of slipping through silently.

Because the router scores against skill metadata (descriptions and tags),
editing a `SKILL.md` front-matter can also move these numbers.
