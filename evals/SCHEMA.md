# Behavioral evaluation framework

`evals/<skill-slug>/` holds the behavioral test cases for one skill: one case
per `<case>.yaml`, assertion self-test fixtures next to it, and one
`plan.json` describing how the skill is evaluated.

Two scripts operate on it, both stdlib-only and offline by default:

| script | what it does |
|---|---|
| `scripts/validate_evals.py` | structure + regex lint + fixture self-tests + `plan.json` checks; `--coverage` reporting |
| `scripts/run_evals.py` | machine-readable JSON report: validation, self-tests, grading of recorded responses, optional executable tests, optional (explicitly enabled) model runs, `--compare` regression detection, and `--grade-labelled` grader calibration against labelled sets (`evals/_grading/`) |

Model runs, the grading layers and how they combine into one verdict, run directories, cost
controls and grader calibration are documented in [`docs/EVALUATION.md`](../docs/EVALUATION.md).

## What is and is not measured

Read this before quoting any number produced here.

- **Measured, deterministically:** whether a given response text satisfies a
  case's *hard assertions* (`must_include` / `must_not_include` regexes), and
  whether the assertions themselves are satisfiable (pass fixtures pass) and
  discriminating (fail fixtures fail).
- **What "discriminating" means here, and its limit.** Discrimination is shown
  only against the specific wrong answers checked in as fixtures. Every case
  with `must_include` has at least one *on-topic* wrong answer (uses the
  topic's vocabulary, reaches the misconception's conclusion) that a
  `must_not_include` pattern catches, as well as an off-topic one. A wrong
  answer phrased in a way no `must_not_include` pattern anticipates will still
  pass; the rubric remains authoritative.
- **Paraphrase robustness is spot-checked, not proven.** Most pass fixtures
  were written by the people who wrote the patterns, so they are *not* a blind
  test. Fixtures labelled `blind` / `adversarial` / `capitulation` were
  written by an independent reviewer and are kept verbatim. Pre-tuning
  probes varied widely by case batch: on the original 23 cases, 23/23 wrong
  and 21/23 correct answers graded correctly; on later first-draft batches,
  independent reviewers found 13 of 16 (and 16 of 23) on-topic wrong answers
  passing every hard assertion before the patterns were widened. Every probe
  answer was then kept as a fixture and the patterns tuned on the misses, so
  **no held-out measurement of pattern accuracy remains**. Real model outputs
  can still phrase a correct concept in a way a pattern misses, which
  under-counts hard pass rates.
- **Measured when you run them:** whether the `tests/examples` unittest ids a
  `kind: executable` case points at pass (`run_evals.py --run-executable`).
- **Not measured by anything in this repository:** a model's pass rate, or
  the uplift from loading a skill. **No baseline has been measured.** No
  report, score, or pass rate is checked in, and every report states
  `"baseline": {"status": "unmeasured"}` unless you produce one yourself (a
  recorded- or model-response run, then `--compare`).
- **Hard assertions are necessary, not sufficient.** A regex can confirm that
  a response names leakage and recommends a `Pipeline`; it cannot confirm
  the reasoning connecting them is right, or catch every phrasing of a wrong
  claim. The subjective rubric (`expected_behavior` / `must_not`) remains the
  authoritative description of a correct answer.
- **LLM-judge verdicts are opinions of another model.** The optional
  `--judge-model` grading is not validated against human labels here, can be
  biased toward verbose or self-similar answers, and is itself
  nondeterministic. Its raw output is reported under `subjective`, never mixed
  into hard pass rates, and never gates `--compare`. The combined
  `graded[].verdict` uses it only through the documented rules in
  `docs/EVALUATION.md`; a regex-only pass is `structural-pass`, never
  `correct`.
- **Model runs are not reproducible bit-for-bit.** Even with identical
  inputs, sampling and serving-side changes can alter responses. Reports pin
  everything that *is* controllable (model id, prompt-template hash, skill
  and case hashes, API version, temperature if sent) so differences can be
  attributed. Save responses (`--save-responses`) to re-grade them later
  deterministically.
- The case set is small and hand-written. A pass-rate difference over a few
  dozen cases is not a statistically powered estimate.

## Layout

```
evals/<slug>/
  plan.json                      # how this skill is evaluated (required by --require-full-coverage)
  <case>.yaml                    # one case
  <case>.pass.md                 # a response that must pass every hard assertion
  <case>.pass.<label>.md         # optional extra passing paraphrases (e.g. .pass.terse.md)
  <case>.fail.md                 # a plausible WRONG response that must fail >=1 hard assertion
  <case>.fail.<label>.md         # optional extra wrong answers
```

Label conventions used in this repository (labels are free-form; only the
`pass`/`fail` part is interpreted):

| label | content |
|---|---|
| `pass.terse` | shorter independent paraphrase by the pattern author |
| `pass.negations` | correct answer full of negations ("not negligible", "don't just mark it flaky", "Absolutely not") that over-broad `must_not_include` patterns would trip |
| `pass.blind` | correct answer written by an independent reviewer, verbatim |
| `fail.on_topic` | wrong answer that names the concepts and still reaches the wrong conclusion; it must be caught by `must_not_include` |
| `fail.paraphrase` | a second, differently worded wrong answer |
| `fail.adversarial`, `fail.capitulation`, `fail.blind` | wrong answers written by an independent reviewer, verbatim (numbered variants such as `adversarial2`, `blind2` are the same kind) |
| `pass.probe`, `fail.probe` | written by the pattern author after the patterns were set, graded once, then kept — not blind |
| `pass.holdout`, `fail.holdout`, `pass.fixprobe`, `fail.fixprobe` | written by a fix engineer before reading or editing the patterns, graded once, then kept — not blind once tuned against |
| `fail.dev-confident`, `fail.dev-partial`, `fail.dev-borderline`, `pass.dev-borderline` (optionally with a `-<suffix>`, e.g. `pass.dev-borderline-approval`) | dev fixtures written by the pattern-tuning engineer AFTER reading the case's patterns, then kept (not blind). In order: a confidently wrong on-topic answer; a mostly correct answer with one `must_not` violation; a hedged or softened wrong answer; a correct answer phrased close to a `must_not_include` pattern. Every `fail.dev-*` fixture must be caught by a `must_not_include` pattern, and any label starting with `dev` must have this form (`tests/test_evals.py`). The case's `notes` say when one led to a pattern change |
| other labels (e.g. `fail.target_leak`, `fail.shows-that`) | a single targeted wrong answer for one specific failure mode; see the case's `notes` |

Authoring pitfalls seen in this repository: `` after `%` never matches
(`%` is not a word character), and `[^.]` sentence windows break early on
periods inside "e.g.", "et al.", "F.pad" and decimals.

`<case>` and `<label>` match `[a-z0-9][a-z0-9_-]*`. Any other file in a
skill's directory is an error. Subdirectories of `evals/` that are not skill
slugs (e.g. `evals/routing/`, the `scripts/eval_router.py` benchmark) are
auxiliary datasets owned by other tools and are ignored — except that a
`*.yaml` case or `plan.json` placed in one is an error (unknown skill).

## Case format

Case files use the repository's restricted YAML subset (flat keys, `|`/`>`
block scalars, flat lists of strings; see docs/SKILL-SPEC.md). Single-quote
regexes (`'\bleak\w*'`; write `''` for an apostrophe) so backslashes are
literal. Unknown fields are errors.

```yaml
id: leakage                      # optional; defaults to and MUST equal the file stem
skill: data-preprocessing        # required; real skill folder; must equal the directory
category: misconception          # required; see Categories
kind: executable                 # required; response | executable
input: |                         # required; the user message
  I have train and test data. Can I normalize the whole dataset before splitting?
# ---- subjective rubric (human / LLM-judge; never auto-graded by regex) ----
expected_behavior:               # required, non-empty
  - identifies that fitting a scaler on combined data leaks test statistics into training
must_not:
  - say it is fine or "usually fine" to normalize before splitting
# ---- hard assertions (deterministic) ----
must_include:                    # ALL must match (concept present)
  - '\b(leak\w*|contaminat\w+|data snooping)\b'
  - '\b(pipeline|columntransformer)\b'
must_not_include:                # NONE may match (wrong claim absent)
  - '\A\W*(yes|sure|of course)\b'
# ---- execution / metadata ----
executable_tests:                # kind: executable only; unittest ids relative to tests/
  - examples.test_data_preprocessing.DataPreprocessingExampleTest.test_full_workflow_runs_and_generalizes_to_unseen_categories
requires:                        # closed set: network, api-key, gpu, example-deps, large-download
  - example-deps
artifacts:                       # what a correct run produces
  - fitted Pipeline persisted as model.joblib
limitations:                     # what this case cannot detect
  - regexes check vocabulary, not reasoning
notes: >
  Why the case exists.
```

| field | type | meaning |
|---|---|---|
| `id` | string | Defaults to the file stem; if present it must equal it. Reports use `<skill>/<id>` (the *case id*). |
| `skill`, `category`, `kind`, `input`, `expected_behavior` | required | as above |
| `must_not` | list | rubric: things a correct response must not do (any form, even hedged) |
| `must_include` | list of regex | hard: every pattern must match the normalized response |
| `must_not_include` | list of regex | hard: no pattern may match |
| `executable_tests` | list | `examples.<module>.<Class>.<test_method>`; resolved statically (AST, no import) so missing optional deps never affect validation |
| `requires` | list | prerequisites to *verify* the case; `run_evals.py --run-executable` skips (with a reason) executable tests of cases requiring anything other than `example-deps` |
| `artifacts`, `limitations` | list | documentation surfaced in plans/reports |
| `notes` | string | free text |

### Kinds

- `response` — graded only on the response text (hard assertions, rubric).
  `executable_tests` is not allowed.
- `executable` — additionally names `tests/examples` tests proving the
  skill's published workflow code for this situation actually runs (e.g. the
  pipeline that keeps injected text inside a delimited data region). The
  tests prove the *skill's code*, not that a model's response wrote
  equivalent code.

### Categories

| category | tests whether the agent... |
|---|---|
| `happy_path` | correctly executes the intended workflow with no complications |
| `edge_case` | handles a realistic but non-default situation (small data, high cardinality, missing values, class imbalance, ...) |
| `adversarial` | resists a request actively trying to produce an unsafe/incorrect/leaking/injected result |
| `misconception` | corrects a plausible-sounding but wrong claim instead of going along with it |
| `failure_recovery` | notices its own action failed/was wrong and recovers instead of looping or confidently continuing |
| `ambiguity` | asks a clarifying question or states an assumption instead of silently guessing |

Every category except `happy_path` counts as **non-happy-path** for coverage.

## Hard assertions

**Matching.** Python `re`, `re.IGNORECASE`, `search` semantics, against the
response after normalization: Unicode NFKC; curly quotes to straight
quotes; en/em dashes and minus signs to `-`; `*` and `` ` `` removed (so
`**data leakage**` and `` `Pipeline` `` match plain text); every whitespace
run (including newlines) collapsed to one space; trimmed. Consequently `\A`
anchors to the start of the answer and `[^.]{0,N}` bounds a match to roughly
one sentence. `_` is kept (`weights_only`, `add_indicator` stay intact).

**Authoring rules** (the fixtures and lint enforce the mechanical part):

- One concept per `must_include` pattern; use alternation for synonyms and
  inflections (`\b(leak\w*|contaminat\w+|data snooping)\b`). Write the
  pattern from the *concept*, then check it against a differently worded
  correct answer — not by copying phrases from your pass fixture.
- Make `must_not_include` negation-safe. Adjacency patterns like
  `(is|was) (fine|ok)\b` do not fire on "is not fine"; when an adverb must
  sit in front, add fixed-width lookbehinds (`(?<!not )(?<!n't )`). Prefer
  anchored openings for capitulation (`\A\W*(yes|sure)\b`).
- A `must_not_include` pattern catches only the phrasings it lists. Keep
  them narrow and high-precision; the rubric `must_not` covers the rest.
- Write `must_not_include` from the *wrong conclusion*, not just the opening
  word: a realistic wrong answer often starts neutrally, names every concept
  ("GPU batching exists in theory...") and then concludes wrongly ("...so
  outputs are deterministic; no need to pin a snapshot"). Capitulation
  openers should cover the common affirmatives (`yes`, `yep`, `absolutely`,
  `sure`, `correct`, `good news`, ...) but not their negations: end the opener
  with `\b(?![\s,!.:;-]*(not|but)\b)` so "Absolutely not." passes.
- Guard against negated correct statements with fixed-width lookbehinds
  (`(?<!not )(?<!n't )`) or a trailing hedge lookahead
  (`(?![^.]{0,40}\b(only|if|unless|but)\b)`), and test the pattern against a
  `pass.negations` fixture. A pattern that fires on "being grounded doesn't
  mean the answer is correct" is worse than no pattern.
- `\b` is a word boundary: `\b...%\b` never matches before a space; put
  symbols outside the trailing `\b`.

**Lint (errors).** A pattern that is empty, does not compile, matches the
empty string (`.*`, `\w*`, `(x)?`, `^`), or matches a content-free probe text
(`Thanks for the question.`, lorem ipsum, a bare refusal) is **vacuous**: a
`must_include` like that passes any response, a `must_not_include` like that
fails every response.

**Self-tests (errors).** Every case with hard assertions needs at least one
`.pass*.md` and one `.fail*.md`. Each pass fixture must pass every hard
assertion (the assertions are satisfiable and not over-fitted); each fail
fixture must fail at least one (they discriminate a realistic wrong answer —
typically one that follows the misconception). Fixtures without assertions,
and fixtures without a case, are errors.

**Non-discriminating patterns (warning; error with `--strict`).** A
`must_include` pattern that also matches *every* fail fixture does not
distinguish any known wrong answer; the case's discrimination then rests on
its other assertions. Add a second wrong answer that lacks the concept, or
sharpen the pattern.

**Concept-free fail fixtures (warning; error with `--strict`).** A fail
fixture is *on-topic* when it matches at least half (rounded up) of the
case's `must_include` patterns. If no fail fixture of a case with
`must_include` is on-topic, every wrong answer is a strawman that misses the
concept vocabulary entirely. It fails the concept patterns trivially and
never exercises `must_not_include`, so the self-test cannot show the case
rejects the realistic threat: an answer that names the right concepts and
still concludes wrongly. Add a `fail.on_topic` fixture. Cases with only
`must_not_include` are exempt. Together the two warnings push each case
toward one off-topic and one on-topic wrong answer. Each self-test result
reports `must_include_matched` and `on_topic`.

`--strict` reports zero warnings and zero advisory warnings on the shipped
tree (it also fails on unexercised `must_not_include` patterns;
`--strict-advisories` checks only those), and `tests/test_evals.py`
additionally requires that, for every case with `must_include`, at least one
on-topic wrong answer is caught by a `must_not_include` pattern.
`tests/test_evals.py` also requires every `must_not_include` pattern to be
triggered by at least one fail fixture.

## plan.json

```json
{
  "skill": "data-preprocessing",
  "type": "workflow",
  "representative_tasks": [{"task": "...", "expected_outcome": "..."}],
  "negative_cases": ["leakage", "A planned case described in a sentence (planned)"],
  "assertion_strategy": ["hard-assertions", "executable", "rubric"],
  "rubric": ["..."],
  "prerequisites": ["..."],
  "execution_constraints": ["..."],
  "expected_artifacts": ["..."],
  "known_limitations": ["..."],
  "unsupported_use_cases": ["..."],
  "non_happy_path_exemption": "optional sentence explaining why no non-happy-path case applies"
}
```

| field | rule |
|---|---|
| `skill` | equals the directory; skill must exist |
| `type` | equals the skill's `SKILL.md` `type` (`workflow` / `reference`) |
| `representative_tasks` | non-empty list of objects with exactly `task` and `expected_outcome` |
| `negative_cases` | strings; a bare id (`[a-z0-9_-]+`) must name an existing **non-happy-path** case in this directory; anything with spaces is a description of a planned case |
| `assertion_strategy` | one value or a list of unique values from `hard-assertions`, `executable`, `rubric`; `hard-assertions` requires a case with hard assertions, `executable` a `kind: executable` case, `rubric` a non-empty `rubric` |
| other lists | lists of non-empty strings (may be empty) |
| `non_happy_path_exemption` | optional, ≥ 20 characters; waives the non-happy-path coverage requirement |

Unknown fields are errors.

## Coverage

```bash
python scripts/validate_evals.py --coverage            # table + summary; never fails on gaps
python scripts/validate_evals.py --coverage --json     # machine-readable
python scripts/validate_evals.py --require-full-coverage
```

Per skill (all skills in the repo, not just covered ones): plan present, case
count, non-happy-path cases, hard-assertion cases, fixture-backed cases,
executable cases, and what is missing. `--require-full-coverage` exits 1 if
any skill lacks `plan.json`, a case, or a non-happy-path case (unless
exempted). The default run does not fail on coverage gaps.

## Running evals

### Offline (default; no network, deterministic)

```bash
python scripts/run_evals.py --out report.json                       # validate + fixture self-tests
python scripts/run_evals.py --responses recorded.jsonl --out r.json # + grade recorded responses
python scripts/run_evals.py --run-executable --python <venv python> # + run executable_tests
python scripts/run_evals.py --run-executable --slow-examples --require-executed   # opt-in slow tests too; a skip fails
python scripts/run_evals.py --grade-heldout                          # regex graders vs evals/_grading/heldout
```

A skipped executable test is reported as `skipped (not evidence)` with its reason and is never
counted as passed. `--slow-examples` and `--require-example-deps` set
`ML_AI_SKILLS_SLOW_EXAMPLES=1` / `ML_AI_SKILLS_REQUIRE_EXAMPLE_DEPS=1` for the tests, and
`--require-executed` exits 1 if any requested test skipped (`docs/EVALUATION.md`, "Executable
cases").

`recorded.jsonl`: one JSON object per line, all fields strings:
`{"case_id": "data-preprocessing/leakage", "condition": "with_skill", "response": "..."}`,
optionally with an integer `sample` (default 0) and `status` (`ok` or `refusal`).
`condition` matches `[a-z][a-z0-9_]*`; `with_skill` / `without_skill` are
the conditions used for uplift. Unknown case ids, duplicates, and malformed
lines are reported in `input_errors` (exit 1), never guessed at. `--skill` /
`--case` filter (repeatable). `--strict` exits 1 if any graded response fails
a hard assertion (default: a failing response is a measurement, not a harness
error).

### Model mode (explicit opt-in; network; costs money)

Full contract: [`docs/EVALUATION.md`](../docs/EVALUATION.md). In short:

- `--provider anthropic | claude-cli | replay`. Paid providers (`anthropic`,
  `claude-cli`) need `--model`, `--allow-network` and a cost ceiling
  `--max-cost-usd`; `anthropic` also needs the API key in `ANTHROPIC_API_KEY`
  (or the variable named by `--api-key-env`) and your current prices
  (`--price-input-per-mtok`, `--price-output-per-mtok`). `replay` needs only
  `--replay-file` and runs offline. Without the full set the run prints
  `not-run: no model configured` (or `not-run: model mode refused`) with the
  missing items and exits 2 before sending anything. That includes any
  model-only option given without `--provider`, so a forgotten `--provider` is
  refused instead of silently running offline. An invalid eval suite exits 1
  before any request.
- `--dry-run` prints the request plan and a worst-case cost; a worst case
  above the ceiling is refused unless `--allow-partial-budget` is given. The
  same applies to judge-only runs (`--responses` / `--grade-*` with a paid
  judge). A paid call whose cost is unknown (no prices, and the CLI reported
  none) stops the run: later attempts are `budget-exhausted`. The ceiling is
  per invocation; `--resume` starts again from $0.
- Each case runs with the full `SKILL.md` in the system prompt (`with_skill`)
  and without it (`without_skill`), same model and parameters, `--samples N`
  times each. Uplift is computed only from cases complete in both arms.
- Temperature is **not sent** unless `--temperature` is given (newer models
  accept only default sampling values). Timeouts, retries and backoff are
  explicit options (`--timeout`, `--max-retries`, `--backoff-base`,
  `--backoff-cap`).
- Every attempt has a status (`ok`, `refusal`, `error`, `timeout`,
  `rate-limited`, `server-error`, `budget-exhausted`); none but `ok` can pass.
- `--run-dir DIR` records every attempt; `--resume` reuses only completed
  attempts whose provenance hash matches.
- `--judge-provider` / `--judge-model` configure an optional judge separately
  from the answering model. Parsing is strict: each list must hold exactly the
  indices `0..n-1` once, each with a JSON boolean `met` / `violated`. A string
  `"false"`, `1`, a duplicate or missing index, or a non-object reply is
  `unparseable`, never coerced into a verdict.
- `--save-responses FILE.jsonl` writes the responses in the recorded format,
  so the grading can be repeated offline without re-sampling.

### Compare (regression detection)

```bash
python scripts/run_evals.py --compare base.json new.json --out diff.json
```

Exit 1 if any of the following is found; the gate cannot be passed by
deleting or weakening what used to pass.

- **`regressions`**, each with `base` → `new`:
  - an assertion that passed in BASE fails in NEW (`pass` → `fail`), or no
    longer exists (`pass` → `missing`, e.g. a pattern was deleted);
  - a case that passed now fails (`pass` → `fail`);
  - a response graded in BASE errored in NEW (`graded` → `error`);
  - a case that was hard-gradable in BASE is not in NEW, because its
    assertions were removed (`pass`/`fail` → `not-gradable`);
  - a `(case_id, condition)` that passed in BASE is absent from NEW
    (`pass` → `missing`). Absent keys that did not pass in BASE are only
    listed in `missing_in_new`.
- **`fixture_regressions`**: a fixture self-test that was OK in BASE fails
  (`new: fail`) or is gone (`new: missing`) in NEW.
- **`new_report_problems`**: the NEW report is not a sound measurement, because
  its suite failed validation or its response input had errors. Such a report
  cannot certify "no regressions".

Also reported: improvements, cases missing or added, cases whose file hash
changed (`case_changed`; comparisons across an edited case are flagged, not
hidden), and the with-vs-without-skill uplift of both reports with its
change. A BASE report without graded responses yields
`"baseline": {"status": "unmeasured"}`. Subjective results are shown for
context only and never gate.

## Report format (`ml-ai-skills/eval-report`, schema_version 2)

JSON, `indent=2`, keys sorted, trailing newline. Identical inputs produce
byte-identical reports except the fields listed in `nondeterministic_fields`
(currently only `generated_at`); model-mode responses are inputs, so two
model runs differ wherever the model's answers differ. `--compare` reads
schema_version 1 and 2 reports.

| key | content |
|---|---|
| `schema`, `schema_version`, `harness_version`, `grader_version` | `ml-ai-skills/eval-report`, `2`, `eval_lib.HARNESS_VERSION`, `eval_grading.GRADER_VERSION` |
| `generated_at`, `nondeterministic_fields` | UTC timestamp; `["generated_at"]` |
| `mode` | `offline` or `model` |
| `environment` | `python_version`, `python_implementation`, `platform`, `git_revision` (or `null`), `git_dirty` |
| `dependencies` | python and harness versions; third-party: none |
| `provenance` | git revision and dirty flag, harness/grader versions, dataset/skill/case hashes, prompt-template and judge-prompt hashes, input-file hashes, the full run `config` (no secret values) and `config_sha256` (`docs/EVALUATION.md`) |
| `dataset` | `dataset_sha256` (over all case and fixture hashes), `case_count`, `cases{case_id: {path, sha256, fixtures{name: sha256}, skill_sha256, category, kind}}`, `skills{slug: {sha256, plan_sha256}}` — hashes are of LF-normalized text |
| `validation` | `ok`, `errors`, `warnings` (same as validate_evals.py) |
| `input_errors` | problems in `--responses`, `--rubric-labels` or replay input |
| `fixture_self_tests` | `total`, `ok`, `failed`, `results[{case_id, fixture, expected, observed, ok, must_include_matched, on_topic, assertions}]` |
| `graded` | `[{case_id, condition, sample, source (recorded/model), status (ok/refusal/error/timeout/rate-limited/server-error/budget-exhausted), response, response_sha256, hard{gradable, passed, assertions[{id, type, pattern, passed, evidence}], skipped_reason?}, rubric{status, source, items[{kind, index, criterion, verdict}]}, subjective{status, ...}, verdict{final, critical_error, critical_sources, rule, reasons}, stop_reason?, model_reported?, usage?, cost_usd?, attempts?, error?}]` |
| `executable` | `null`, `{status: not-run, reason}`, or `{status: run, python, env_overrides, results[{test_id, cases, status (passed/failed/skipped), executed, status_label?, detail}], counts}` |
| `skipped` | `[{case_id, what, reason}]` — rubric-only cases, cases without a supplied response |
| `summary` | `hard{condition: {responses, gradable, passed, failed, errors, refusals, not_gradable, pass_rate}}`, `subjective{condition: {graded, rubric_passed, not_graded, judge_errors, rubric_pass_rate}}`, `final{condition: {attempts, by_status, verdicts, structural_pass, correct, critical_error, rubric_coverage, abstentions, disputed}}` (rates as `{k, n, rate, ci95}`) |
| `skill_uplift` | the structural layer: `unmeasured` with reason and `incomplete_cases`, or paired-case `with_skill_pass_rate`, `without_skill_pass_rate`, their `ci95`, `delta`, and the cases that differ |
| `uplift` | `{structural, correct, critical_error}`, each paired over cases complete in both arms, with `incomplete_cases` |
| `verdict_rules` | the rules that produced `graded[].verdict` |
| `baseline` | always `{status: unmeasured, reason}` in a single report |
| `model_evaluation` | `{status: not-run, reason}` offline; `{status: run, complete, attempts, by_status, provider, model}` for a model run |
| `model_run` | `null`, or provider, model, judge_model, endpoint, anthropic_version, conditions, samples, max_tokens, temperature (+ note), prompt_template_version/sha256, timeout, retries, http_attempts, http_client, backend, judge, budget, preflight, run_dir, resume |

The compare document (`ml-ai-skills/eval-compare`) has `base`/`new`
summaries, `baseline`, `regressions`, `improvements`, `fixture_regressions`,
`new_report_problems`, `missing_in_new`, `added_in_new`, `changed_cases`,
`skill_uplift`, `subjective`, and `summary` counts.

## Rubric grading contract (human or LLM judge)

- Grade on whether every `expected_behavior` item is *substantively* present
  (a paraphrase counts), and whether any `must_not` item is present (any
  form of it is a fail, even hedged).
- A response can say more than what's listed — extra correct detail is not a
  failure.
- A response that hedges appropriately ("this depends on X") is not
  automatically a failure of `happy_path` cases; only mark it down if the
  hedge avoids answering the actual question.
