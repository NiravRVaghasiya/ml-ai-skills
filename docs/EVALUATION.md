# Evaluation: model runs, grading layers, calibration

`scripts/run_evals.py` grades responses to the cases in `evals/<slug>/` (format:
`evals/SCHEMA.md`). This document covers the parts that involve a model or labels: the answering
backends, cost controls, the with-skill vs without-skill comparison, run directories and resume,
the grading layers and how they are combined, report provenance, and measuring the graders
themselves against labelled sets (`evals/_grading/README.md`).

## Status: what has been run

### Model runs

- **Offline.** Every model-mode path below is exercised with offline fakes in
  `tests/test_run_evals.py` and `tests/test_eval_grading.py`: a scripted HTTP transport, an
  in-process backend, a fake `claude` executable, and the replay backend.
- **Smoke run, 2026-10-30.** The first call to a real model used the `claude-cli` backend with
  `--model sonnet --claude-cli-arg=--bare`, on `claude 2.1.312.1459` (an internal ASBX build,
  authenticated through Bedrock): 1 case x 2 arms, both attempts `ok`, the CLI reported model
  `claude-sonnet-5-5` and a cost of $0.022. That shows the backend, its flags and the cost
  accounting working end to end on one case. It measures nothing.
- **Full run, 2026-10-30.** 84 cases x 2 arms with the same backend, a judge
  (`--judge-provider claude-cli --judge-model opus`) and a $40 ceiling: up to 336 requests
  (168 answers, 168 judge calls). All 336 calls returned `ok`; the CLI-reported cost was
  $6.13. The report is `docs/evidence/model-run-2026-10-30.json`.

Results of the full run (one sample per case per arm; Wilson 95% intervals):

| metric (n = 84 per arm) | with skill | without skill |
|---|---|---|
| correct (combined verdict; needs the judge's rubric pass) | 0.583 [0.477, 0.683] | 0.452 [0.350, 0.559] |
| judge rubric pass | 0.798 | 0.607 |
| regex structural pass | 0.714 [0.610, 0.800] | 0.667 [0.561, 0.758] |
| critical error (`must_not` violation) | 0.119 [0.066, 0.205] | 0.131 [0.075, 0.219] |

Per case, "correct" improved in 18 cases, got worse in 7 and tied in 59 (exact two-sided
sign test on the untied cases: p = 0.043). Read it as one measurement, not an established
effect: the judge is a model and is not calibrated against people, the judge and the
answering model are from the same family, there is one sample per case, and several metrics
are reported without correction. Details and the cases that got worse: `docs/KNOWN-ISSUES.md`
#1. No CI job calls a model.

- The `anthropic` backend has not been run against the real API.
- The `claude-cli` backend's flags (`--system-prompt-file`, `--tools ""`, `--disable-slash-commands`,
  `--strict-mcp-config`, `--no-session-persistence`, `--max-budget-usd`, `--output-format json`)
  were chosen from the CLI's documentation and `--help`. The build above accepted them, with
  `--bare`. Whether they give the isolation they aim for (below) was **not** checked.

### Held-out grader accuracy

`evals/_grading/heldout/manifest.jsonl` holds 249 answers: three for each of 83 cases (every
case except `data-pipelines/ds_partition_migration`, which was added later; 1 to 5 cases per
skill, all 38 skills). Per case there is one correct or borderline-correct answer, one
confidently incorrect answer and one partially correct answer.

- **Writers.** 6 blind model writers. They read only the case input and its rubric
  (`expected_behavior`, `must_not`), never the regexes, the skills or the repository. The
  tool-use transcripts of the blind agents were checked: none touched a repository file.
- **Labels.** Two independent model labelers per answer (`label_source: agent-adjudicated`;
  models, not people). Both agreed with the writer's intended label on all 249 answers
  (83 pass, 166 fail), so none is `unresolved`.
- **Independence from tuning.** The patterns had been tuned on the `evals/<slug>/` fixtures,
  and that tuning had finished before this set was written to disk. Whoever tuned them never
  saw it.

`python scripts/run_evals.py --grade-heldout` (re-run 2026-10-30; regex layer, Wilson 95%):

| regex layer | k/n | rate | 95% interval |
|---|---|---|---|
| TPR: correct answers passed | 71/83 | 0.855 | [0.764, 0.915] |
| FNR: correct answers failed | 12/83 | 0.145 | [0.085, 0.236] |
| TNR: wrong answers failed | 152/166 | 0.916 | [0.863, 0.949] |
| FPR: wrong answers passed | 14/166 | 0.084 | [0.051, 0.137] |

By answer type: all 83 confidently incorrect answers failed. The 14 false passes are all
partially correct answers (14 of 83). The 12 false fails are 9 of 57 correct and 3 of 26
borderline-correct answers.

Limits of this estimate:

- The labelers agreed with the writer on every answer, which suggests the wrong answers are
  clear-cut. The set is therefore likely easier than real model output, where mistakes may
  be subtler; expect more false passes there.
- The labels come from models. `status.against_humans` says "not measured": no item is
  human-labelled.
- The three answers of a case are not independent, and neither are cases of one skill; the
  intervals treat every answer as independent, so they are optimistic. Per-skill rates rest
  on 3 to 15 answers.
- Only the regex layer was scored here. No judge has been scored against these labels, and
  the `calibration/` set is empty, so any judge, including the one in the full run, is
  **uncalibrated**.
- An answer someone looks at while editing a pattern or a judge stops being held out
  (`evals/_grading/README.md`).

## Commands

```bash
# offline (default): validation, fixture self-tests, recorded responses, executable cases
python scripts/run_evals.py --out report.json
python scripts/run_evals.py --responses recorded.jsonl [--rubric-labels labels.jsonl] --out r.json
python scripts/run_evals.py --run-executable --slow-examples --require-executed --skill experiment-tracking

# see what a model run would send and cost, without sending anything
python scripts/run_evals.py --provider anthropic --model <id> --dry-run \
    --price-input-per-mtok <USD> --price-output-per-mtok <USD> [--max-tokens 1024]

# model runs (explicit opt-in; costs money)
ANTHROPIC_API_KEY=... python scripts/run_evals.py --provider anthropic --model <id> --allow-network \
    --max-cost-usd 5 --price-input-per-mtok <USD> --price-output-per-mtok <USD> \
    --run-dir runs/2026-10-29 --save-responses responses.jsonl --out model-report.json
python scripts/run_evals.py --provider claude-cli --model <id> --allow-network --max-cost-usd 5 --run-dir runs/x
python scripts/run_evals.py --provider replay --replay-file responses.jsonl            # offline, deterministic

# resume an interrupted run (only matching, completed attempts are reused)
python scripts/run_evals.py <same options> --run-dir runs/2026-10-29 --resume

# measure the graders against labelled sets
python scripts/run_evals.py --grade-heldout --out heldout-grading.json
python scripts/run_evals.py --grade-labelled calibration --judge-provider anthropic --judge-model <id> \
    --allow-network --max-cost-usd 2 --judge-price-input-per-mtok <USD> --judge-price-output-per-mtok <USD>
```

## Backends

| `--provider` | what it does | required |
|---|---|---|
| `anthropic` | Messages API over stdlib `urllib` (`--endpoint`, https only; redirects refused so the key is never forwarded) | `--model`, `--allow-network`, `--max-cost-usd`, `--price-input-per-mtok`, `--price-output-per-mtok`, and the API key in the environment variable named by `--api-key-env` (default `ANTHROPIC_API_KEY`) |
| `claude-cli` | runs `claude -p --output-format json` with the case input on stdin and the system prompt in a temporary file, in a fresh temporary working directory, with a bounded `--timeout` | `--model`, `--allow-network`, `--max-cost-usd` (plus `--price-*` with `--no-cli-budget-flag`). It uses **whatever authentication the CLI is configured with** (API key, subscription, Bedrock or Vertex); run_evals neither reads nor records it |
| `replay` | answers from a recorded JSONL file (`--replay-file`), keyed by `(case_id, condition, sample)`; a row may carry `status` to replay a failure | `--replay-file` |

Without the full set, the run is refused before anything is sent: `run_evals: not-run: no model
configured ...` (or `not-run: model mode refused ...`) on stderr, listing what is missing, exit 2.
Any model option given without `--provider` counts as a model run, so a forgotten `--provider`
is refused instead of silently running offline.

Shared request controls, all explicit with fixed defaults: `--max-tokens` (4096), `--temperature`
(not sent unless given; newer models reject non-default sampling), `--timeout` (120 s per
request), `--max-retries` (4), `--backoff-base` (1 s), `--backoff-cap` (60 s), `--anthropic-version`
(`2023-06-01`). The anthropic backend retries 408, 429, 500, 502, 503, 504, 529 and connection
errors with exponential backoff and jitter, or the numeric `retry-after` header (capped). Other
HTTP errors are not retried. Truncated or malformed HTTP responses (`http.client.IncompleteRead`,
`BadStatusLine`, ...) are treated as connection errors: retried, then recorded as an `error`
attempt, never a crash.

The `claude-cli` backend has no flags for `max_tokens` or `temperature`, so neither is applied; the
report says so under `model_run.backend.not_controlled`. The base flags are **intended** to remove
Claude Code's own system prompt, tools, skills and slash commands, and MCP servers, so that an
installed copy of these skills does not leak into the `without_skill` arm. This is **unverified**:
the 2026-10-30 runs used these flags, but nothing checked what reached either arm. Without `--bare`, hooks from settings and installed plugins (for
example SessionStart context injection) and user-level CLAUDE.md or rules files may still be
loaded into both arms. Where your auth mode allows it, add `--claude-cli-arg=--bare` (in the CLI
build inspected here, `--bare` skips hooks, plugin sync and CLAUDE.md auto-discovery, but limits
Anthropic auth to `ANTHROPIC_API_KEY` or an `apiKeyHelper`), or other isolation flags your build
supports, with `--claude-cli-arg=<flag>` (repeatable; use the `=` form for values that start with
`-`). Pass `--no-cli-budget-flag` if your build has no `--max-budget-usd`; prices are then required.

## Cost controls

Paid backends (`anthropic`, `claude-cli`) never run without `--max-cost-usd`. No price table is
built in, because a stale price would make the ceiling meaningless; for `anthropic`, give the
current per-million-token prices with `--price-input-per-mtok` / `--price-output-per-mtok` (and
`--judge-price-*` for a judge on a different model).

- **Preflight.** Before any request, run_evals prints the request count and a worst-case cost:
  every call uses its `max_tokens` (`--judge-max-tokens` for the judge), and input is estimated
  at one token per two UTF-8 bytes (a deliberate overestimate; it is not a tokenizer). If the
  worst case exceeds the ceiling, the run is refused (exit 2) unless `--allow-partial-budget` is
  given. `--dry-run` prints the plan as an `ml-ai-skills/eval-plan` document (with a `provenance`
  block) and sends nothing; it needs no key and no `--allow-network`. The same preflight and
  `--dry-run` apply to judge-only runs (`--responses` or `--grade-*` with a paid judge). A
  `claude-cli` call has no worst case, priced or not, because the CLI has no max-tokens flag
  (`ClaudeCliBackend.worst_case_cost` returns `None`). So for a `claude-cli` run the preflight
  prints `unknown (unpriced)` and cannot refuse up front.
- **During the run.** A call is made only if `spent + its worst case <= ceiling`. Without a worst
  case (every `claude-cli` call, and an unpriced backend) a call is made only if
  `spent < ceiling`. Spend is charged from the cost the claude CLI reports (`total_cost_usd`), or
  the reported usage times your prices. If neither exists but a worst case does (`anthropic`),
  the worst case is charged. If a paid call has **no** known cost (for `claude-cli`: no reported
  cost and no usage-times-prices), the run stops there: spend can no longer be bounded, so
  every remaining attempt is `budget-exhausted` and `model_run.budget.stop_reason` says why. For `claude-cli`, the remaining budget is also
  passed to the CLI as `--max-budget-usd`, which bounds each call by the CLI's own accounting.
  Known limitation: with `--no-cli-budget-flag` nothing bounds a single `claude-cli` call. The
  ceiling is then checked between calls only, so one call can take spend past the ceiling.
  Once one call is unaffordable, every remaining attempt is recorded as `budget-exhausted`;
  nothing is skipped silently.
- **Retries.** For `anthropic`, each HTTP attempt that ended in a timeout or dropped connection
  may still have been billed, so it is charged one more worst case on top of the observed cost.
  The pre-call check reserves only one worst case, so a call that retries after connection
  errors can overshoot the ceiling by up to `--max-retries` worst cases. 429 and 5xx responses
  are not charged.
- **Scope.** The ceiling applies to **one invocation**. `--resume` starts again from $0 and does
  not count what earlier invocations spent (reused attempts cost nothing). Track the total across
  resumes yourself, or lower the ceiling.
- Spend in the report (`model_run.budget`) is an estimate, not an invoice.

## Controlled comparison

Each case is answered under `with_skill` (its `SKILL.md` in the system prompt inside a fixed,
hashed template) and `without_skill` (the same base instruction without it), with the same
model and parameters. `--samples N` repeats each (case, condition) N times. Calls are ordered
case by case, sample by sample, with both conditions in turn, so a stop (budget, interruption)
affects both arms alike.

Uplift (`report.uplift`) is computed per layer: `structural` (hard-assertion pass), `correct`
(final verdict `correct`) and `critical_error` (any must_not violation; a negative delta means
fewer critical errors with the skill). The `correct` layer has an outcome only for attempts whose
rubric was graded (labels or judge). A regex-only result, including a `must_not_include` `fail`,
has none, so regexes alone never make it `measured`. A refusal counts as not correct when a
rubric grader was configured for the run (`graded[].rubric.grader_configured`); otherwise it has
no correctness outcome. `summary.final.*.correct` uses the same rule. With
`rubric_graded_attempts: 0`, `critical_error` counts `must_not_include` regex hits only. A case
counts only when **both** arms have an outcome on
that layer for every sample, with equal sample counts. Anything else is listed under
`incomplete_cases` with the reason (a missing arm, an errored or timed-out attempt, an ungraded
rubric) and left out. An uplift is never computed from one arm. Per-case deltas are averaged
over complete cases. The pooled per-arm rates carry Wilson 95% intervals, which treat samples
as independent and are therefore too narrow when `--samples` > 1. Nothing is tested for
significance, and no report claims significance. The legacy `skill_uplift` key is the
`structural` layer.

## Attempt statuses

Every attempt is recorded with one status:

| status | meaning |
|---|---|
| `ok` | the model answered |
| `refusal` | the model answered with a refusal (`stop_reason: refusal`); graded, never a pass |
| `error` | a non-retryable failure (HTTP 4xx, CLI non-zero exit or unparseable output, no replay row) |
| `timeout` | the request or CLI did not finish in `--timeout` |
| `rate-limited` | HTTP 429 after all retries |
| `server-error` | HTTP 5xx or 529 after all retries |
| `budget-exhausted` | not sent: the cost ceiling would have been exceeded |

A failure has no hard verdict (`hard.passed: null`) and its status is its final verdict. None of
them is ever counted as a pass, and `--strict` exits 1 if any attempt is not `ok`.
`model_evaluation.complete` is false when any failure status occurred.

## Grading layers and the final verdict

1. **structural**: the case's regex hard assertions (`must_include` / `must_not_include`),
   deterministic (`eval_lib.grade_response`). Recorded under `graded[].hard`.
2. **rubric**: one verdict per `expected_behavior` item (`met` / `not-met`) and per `must_not`
   item (`violated` / `not-violated`), under `graded[].rubric`. The verdicts come from
   `--rubric-labels` (for example human review) or from the judge; labels win when both exist.
   With neither, every item is `not-graded`. A judge that cannot be parsed or errors makes the
   layer `abstained`.
3. **judge** (optional): a separately configured model (`--judge-provider`, `--judge-model`,
   `--judge-max-tokens` (2048), `--judge-temperature`, `--judge-price-*`, `--judge-replay-file`)
   that grades the rubric. The raw judge output is recorded under `graded[].subjective`. Its prompt
   is hashed (`provenance.judge_prompt_sha256`). Parsing is strict: each list must hold exactly
   the indices `0..n-1`, each with a JSON boolean. Anything else is `unparseable`, never coerced.
   When the judge and the answering model are the same model, `model_run.judge_same_model_as_answerer`
   is true; expect self-preference bias.

The final verdict (`graded[].verdict`) applies these rules in order (`eval_grading.VERDICT_RULES`,
copied into every report as `verdict_rules`):

1. An attempt whose status is not `ok` gets that status as its verdict. It is never a pass.
2. Any must_not violation, from a `must_not_include` regex match or a rubric `must_not` item
   graded violated, makes the verdict `fail` and counts as a critical error, whatever the other
   layers say.
3. If the rubric layer graded every item: every `expected_behavior` met and every hard assertion
   passed gives `correct`; an `expected_behavior` item not met gives `incorrect`; every item met
   but a `must_include` regex missed gives `disputed` (the layers disagree; not counted as correct).
4. Without a rubric verdict: every hard assertion passed gives `structural-pass` (concepts present,
   correctness unknown; **never reported as correct**); a `must_include` miss gives
   `structural-fail`; a case without hard assertions is `ungraded`.

Per condition, `summary.final` reports attempts by status, verdict counts, `structural_pass`
(over hard-gradable answers), `correct` (over rubric-graded answers, plus refusals when a rubric
grader was configured), `critical_error` (over
answers), `rubric_coverage`, `abstentions` and `disputed`, each rate as `{k, n, rate, ci95}`.
`summary.hard` and `summary.subjective` keep the separate per-layer counts.

### Rubric labels (`--rubric-labels`)

JSONL, one object per labelled response:
`{"case_id", "response_sha256", "expected_behavior": [bool, ...], "must_not": [bool, ...],
"label_source": "human" | "agent-adjudicated" | "fixture-author"[, "labeler"]}`. `response_sha256`
is the `graded[].response_sha256` of the response (SHA-256 of its UTF-8 text). A list whose
length does not match the case makes the rubric `abstained` for that response.

## Run directories and resume

`--run-dir DIR` writes:

- `manifest.json`: the run's provenance (below) and the backend and judge identities. Each resume
  adds a `manifest.resume-<timestamp>.json`.
- `records.jsonl`: append-only, one line per answer or judge attempt, flushed and fsynced:
  `{type: answer|judge, key: "<case_id>|<condition>|<sample>", provenance_sha256, recorded_at,
  completion | judge, ...}`.
- `report.json`: the final report.

An existing run directory with records is refused unless `--resume` is given. With `--resume`, a
recorded attempt is reused only when its key **and** its provenance hash match and it completed
(answer `ok` / `refusal`; judge `graded` / `unparseable`). The answer provenance hash covers
what is sent to the answering model (system prompt, user message, `max_tokens`, temperature,
condition, sample, and the hash of the answer prompt templates) and the backend identity
(provider, model, endpoint and API version, or CLI arguments). Editing a `SKILL.md` therefore
re-runs that skill's `with_skill` answers but reuses its `without_skill` ones. Editing a case's
regexes re-runs nothing, because answers are re-graded on every run. The judge hash adds the
answer hash, the response, the rubric items, the judge identity and the judge prompt hash, so
editing only the judge prompt re-runs the judge calls and reuses every answer. Failed attempts (`error`, `timeout`, `rate-limited`,
`server-error`, `budget-exhausted`) are attempted again. A truncated last line, left by a crash
mid-write, is skipped. `model_run.resume` counts `reused`, `reattempted`, `stale_ignored`,
`corrupt_lines` and `recorded`. Two concurrent runs on one directory are not supported.

## Report provenance (`ml-ai-skills/eval-report`, schema_version 2)

Every report carries a `provenance` block: `git_revision` and `git_dirty`, `harness_version`,
`grader_version`, `dataset_sha256`, `skill_sha256` per skill and `case_sha256` per case
(LF-normalized), `prompt_template_version` / `prompt_template_sha256` (the answer templates),
`judge_prompt_sha256` (when a judge ran), `responses_sha256` / `rubric_labels_sha256` /
`replay_file_sha256` for input files, and `config`, every option of the run with its
`config_sha256`. `config` holds environment-variable *names* (`api_key_env`), never values. The
same block (with its own `report_schema`) is in every `--dry-run` plan, every run-directory
manifest, and every `--grade-labelled` / `--grade-heldout` calibration report, which adds
`manifest_sha256`. The UTC timestamp is the top-level `generated_at`, the only
field allowed to differ between two runs on identical inputs. Model runs add `model_run` (backend
description, judge description, budget, preflight, resume counts) and per-attempt
`model_reported`, `stop_reason`, `usage`, `cost_usd` and `attempts`.

Schema version 2 added `graded[].sample`, `graded[].rubric`, `graded[].verdict`, `summary.final`,
`uplift`, `provenance`, `verdict_rules`, `model_evaluation`, `grader_version` and
`summary.hard.*.refusals`. `--compare` reads version 1 and 2 reports and refuses other versions.

## Secrets

API keys are read from the environment and placed only in the `x-api-key` request header. As
defence in depth, every report, run-directory file, saved-responses file and stderr line passes
through a redactor. The redactor replaces the values of environment variables whose names
contain `KEY`, `TOKEN`, `SECRET`, `PASSWORD`, `CREDENTIAL` or `AUTH` (8 characters or longer),
plain or JSON-escaped, with `[REDACTED]`. `RedactionTests` in `tests/test_run_evals.py` checks this
with a proxy that echoes the key in an error body and a model that echoes another token.

## Measuring the graders (calibration and held-out)

`--grade-labelled SET` scores graders against labelled answers. SET is `heldout`, `calibration`,
`dev` (`evals/_grading/<SET>/manifest.jsonl`), `dev-fixtures` (the tuned `evals/<slug>/` fixtures),
or a path. `--grade-heldout` is short for `--grade-labelled heldout`. The format, partition rules
and label sources are in `evals/_grading/README.md`.

- Graders scored: `regex` always; with a judge configured, also `judge` (rubric pass) and
  `layered` (the final verdict, where `correct` counts as pass and `fail` / `incorrect` /
  `disputed` / `structural-fail` count as fail). Both share one judge call per item. A replay
  judge (`--judge-provider replay`) is keyed per answer by `(case_id, "labelled:<first 12 hex
  digits of the answer's SHA-256>", 0)`.
- The positive class is `final_label: pass`. Reported: TP/FN/TN/FP, TPR, FNR, TNR and FPR, each
  with `{k, n, rate, ci95}`, in aggregate, `by_skill` and `by_label_source`, plus per-item outcomes.
  FPR, the share of wrong answers a grader passes, is the gate's miss rate.
- Excluded and counted, never scored: `unresolved` labels, unknown cases, and answers identical
  after normalization to a dev fixture (`dev_overlap`, checked for any non-dev item). Abstentions
  (a judge that did not return a verdict) and `not_gradable` (rubric-only cases, for the regex
  grader) are counted separately.
- `status.against_humans` says whether any scored item is human-labelled. Agreement with
  `agent-adjudicated` labels is agreement with model labelers, not people.
- `dev-fixtures` scores 100% by construction, because the patterns were tuned on those
  fixtures. It shows the plumbing works, not accuracy.
- A manifest with errors is scored by the regex grader only; no judge call is spent on it.
- Exit 0 when scored or when the set is empty or missing (`status: unmeasured`); exit 1 on manifest
  errors.

## Executable cases

`--run-executable` runs the `tests/examples` unittest id of each `kind: executable` case in a
subprocess (`--python`, `--executable-timeout` per id, default 900 s). Each result is `passed`,
`failed` or `skipped`. A skipped result has `executed: false` and `status_label: "skipped (not
evidence)"`, and its skip reason is in `detail` and on stderr; it is never counted as passed. A
failed result keeps the last 4000 characters of the test output (the traceback) in `output_tail`.

- `--slow-examples` sets `ML_AI_SKILLS_SLOW_EXAMPLES=1` for the tests, so opt-in slow tests run
  (for example `tests/examples/test_experiment_tracking.py`, roughly 100-160 s of MLflow setup).
- `--require-example-deps` sets `ML_AI_SKILLS_REQUIRE_EXAMPLE_DEPS=1`, so a missing example
  dependency fails instead of skipping. CI's `examples` job sets this variable for the whole job.
- `--require-executed` exits 1 if any requested executable test was skipped, including tests of
  cases whose `requires` names something other than `example-deps`.
- `executable.env_overrides` records which of these variables run_evals set.
- Any of these three flags without `--run-executable` is a usage error (exit 2), because nothing
  would be executed and `--require-executed` would pass vacuously.

## Exit codes

| code | meaning |
|---|---|
| 0 | ran; a failing response is a measurement, not a harness error |
| 1 | validation, self-test or input failure; a failed executable test; a skipped one under `--require-executed`; labelled-set errors; a `--compare` regression; with `--strict`, any hard failure or non-`ok` attempt |
| 2 | usage error (including an executable-only flag without `--run-executable`), or **not-run**: no model configured, a missing requirement, a worst-case cost above the ceiling (model or judge-only run), or a used run directory without `--resume`. Nothing was sent |

## Known limitations

- Hard assertions check vocabulary, not reasoning. On 249 held-out answers labelled by models
  they passed 14 of 166 wrong answers (all of them partially correct) and failed 12 of 83
  correct ones; that set is likely easier than real output (see "Held-out grader accuracy").
- The judge is another model's opinion: uncalibrated against people, possibly biased toward long
  or self-similar answers, and itself nondeterministic.
- Token and cost estimates are heuristics. The ceiling bounds spend by those estimates (and, for
  `claude-cli`, by the CLI's reported cost), so a provider's real bill can differ. The ceiling is
  per invocation, not per run directory.
- `claude-cli` isolation between the two arms is intended, not verified (see Backends).
- The case set is small and hand-written. A difference of a few cases is not a powered estimate.
