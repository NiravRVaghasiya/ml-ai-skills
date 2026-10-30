# Quality gates

A skill is "done" when it passes every gate below. The markers say how each gate is
enforced:

- **(CI)**: a script fails a job in `.github/workflows/ci.yml` (job name in brackets). The
  same scripts can be run locally. CI has run on GitHub, but **only the `examples` job's
  results have been observed** (`docs/KNOWN-ISSUES.md` #4), so for every other job "(CI)"
  describes the configured gate, not an observed result. Every job feeds the final
  `all-required` job, which fails unless each one succeeded; it is the one check branch
  protection needs to require (see "CI wiring" below).
- **(review)**: needs human or agent judgement, recorded in `docs/SKILL-AUDIT.md` or the PR.

Passing every (CI) gate shows a skill is well-formed, that its code runs on the test
inputs, and that its eval fixtures grade as intended. It does not show the advice is
correct; that remains a (review) gate.

`python scripts/quality_report.py` runs the offline gates in one go and writes a JSON report
that lists which gates ran, their results, and which were skipped and why. It is a local
tool: CI does not run it, and its report is not itself a gate.

## Structural
- [ ] **(CI: validate)** Frontmatter has every required key from
      `schema/allowed_values.py` `REQUIRED_KEYS`, with valid enum values, inside the
      supported YAML subset, within the Claude limits for `name`/`description`
      (`scripts/validate_skills.py --warnings`).
- [ ] **(CI: validate, generated)** Folder name == frontmatter `name` == INDEX.md slug;
      INDEX.md, the README stats block, `plugins/` and the dependency graph are regenerated,
      not hand-edited (`scripts/validate_skills.py`, `scripts/check_generated.py`).
- [ ] **(CI: validate)** `related`/`requires`/`conflicts` name real skills and `requires`
      has no cycles (`scripts/validate_skills.py`, `scripts/build_dependency_graph.py`).
- [ ] **(CI: validate)** The generated JSON Schema matches `schema/allowed_values.py`
      (`scripts/generate_schema.py --check`), and the public contract matches its lock
      (`scripts/check_contract.py --check`), also against the lock at the base commit
      (`--base-ref`, see "Base commit" below). A breaking change needs a major version
      (`docs/RELEASE.md`).
- [ ] **(CI: plugins)** Every packaged `SKILL.md` is byte-identical to its source and
      `claude plugin validate --strict` passes (`tests/test_generate_plugins.py`, pinned CLI).
      The full marketplace lifecycle on a clean clone is
      `tests/test_claude_cli_integration.py` (opt-in, `ML_AI_SKILLS_CLAUDE_CLI=1`).
      Upload-compatible copies come from `scripts/export_skills.py` and are checked by
      `tests/test_export_skills.py`.

## Content
- [ ] **(review)** Purpose is clear from `## Overview` alone.
- [ ] **(review)** A person's whole-skill review is recorded in `docs/reviews.json` for the
      current SKILL.md hash (`python scripts/quality_report.py` reports `reviewed` /
      `review-stale`; format in `docs/EVIDENCE.md`).
- [ ] Scope boundaries are explicit. **(CI: validate)** checks that the description has a
      "NOT for ..." clause and ≥3 trigger phrases. **(review)** checks that the clause
      names the *right* sibling.
- [ ] **(CI: validate)** Correct section set for the declared `type`; ≥3 Gotchas, ≥2
      References.
- [ ] Workflow steps are actionable. **(CI: validate)** warns on unparseable Python and on
      steps without code. **(CI: examples)** runs the code (see Execution). **(review)**
      confirms each step's code does what the step says.

## Evidence
- [ ] **(review)** `evidence_level` reflects the dominant tier of this skill's claims.
- [ ] **(review)** Heuristics are phrased conditionally (`docs/EVIDENCE.md`).
- [ ] **(review)** References are real and resolvable, each with a one-line reason.
      Live link checks are manual (`validate_skills.py --check-links`, `link-check` job on
      `workflow_dispatch`) or scheduled (`freshness-scheduled.yml`), never a merge gate.
- [ ] **(CI: validate)** Checkable claims in `docs/claims.json` are well-formed, and every
      `runtime-test` claim names a test that exists (`scripts/check_claims.py`). The checker
      does not re-run the test.

## Execution
- [ ] **(CI: examples)** Every runnable `python` block of a code-bearing skill is executed by
      `tests/examples/` against the pins in `requirements-examples.txt`; a missing
      dependency fails instead of skipping (`ML_AI_SKILLS_REQUIRE_EXAMPLE_DEPS=1`), any
      skipped test fails the job, and a `run_blocks` call that runs longer than 900 s ends
      the test process with a traceback (`ML_AI_SKILLS_EXAMPLE_TIMEOUT`). Non-runnable blocks are marked
      `# illustrative` (`docs/EXAMPLE-TESTS.md`).
- [ ] **(CI: validate)** No known-removed API pattern (`STALE`) in the skill text
      (`scripts/check_freshness.py`). This is a regex heuristic, not verification.
- [ ] **(review)** Stubs in example tests reproduce the real behaviour the code relies on,
      and every fixed defect has a regression test.
- [ ] **(opt-in, not CI)** Claims the offline suite fakes (pretrained models, CUDA, TF, S3,
      Docker, Kubernetes, Airflow, Feast) have an integration test under
      `tests/integration/`, gated by `ML_AI_SKILLS_INTEGRATION_<NAME>=1`; once opted in,
      missing infrastructure fails instead of skipping (`docs/EXAMPLE-TESTS.md`,
      "Integration tests (opt-in)").

## Evaluation
- [ ] **(CI: validate)** Every skill has `evals/<slug>/plan.json`, at least one case, and a
      non-happy-path case; every hard assertion is a valid, non-vacuous regex; every
      case's pass fixtures pass and fail fixtures fail
      (`scripts/validate_evals.py --strict --require-full-coverage`). Lint advisories
      (a `must_not_include` pattern no fixture exercises) fail too: `--strict` includes
      them, and `--strict-advisories` checks only them (a separate validate step).
- [ ] **(CI: examples)** `kind: executable` cases' test ids pass
      (`scripts/run_evals.py --run-executable`).
- [ ] **(CI: evals-offline)** The routing benchmark's test split meets this commit's
      baseline on all eight metrics (including false activation over every case that expects
      no skill) and the base commit's baseline on every metric that baseline records (seven
      with 51743b0, the current HEAD, as base: its baseline predates false activation over
      negatives),
      and `baseline.json` matches
      `benchmark.json`'s hash (`scripts/eval_router.py --gate`, `docs/ROUTING.md`; the base
      commit is defined below). A drop below the base commit's floor passes only when this
      commit's `baseline.json` lists it in `accepted_regressions` with the exact measured
      value; it is then a `::warning::`, not an error (`evals/routing/README.md`).
- [ ] **(not gated)** Grading of real model output. `scripts/run_evals.py` model mode
      (anthropic, claude-cli and replay backends; cost ceiling; layered grading;
      `docs/EVALUATION.md`) was first run against a real model on 2026-10-30 (claude-cli);
      what was measured, and its limits, are in `docs/EVALUATION.md`. No CI job calls a
      model. The regex graders' accuracy on 249 held-out answers labelled by model labelers
      (not people) is reported by `run_evals.py --grade-heldout`: they passed 71 of 83
      correct answers and failed 152 of 166 wrong ones (`docs/EVALUATION.md`).

## Maintenance
- [ ] **(CI: validate, informational)** `last_verified` is within the review window in
      `schema/freshness_policy.json` (180 days by default, shorter for llm, security and
      mlops). Overdue is `REVIEW_DUE`, which does not fail CI.
- [ ] **(review)** Code-bearing skills declare `version_constraints` for the packages their
      Workflow imports, saying how each entry is known (executed against a version, or a
      model-knowledge estimate).

## Security
- [ ] **(review)** Skills whose Workflow has real-world side effects (deployment, deletion,
      sending data externally, granting tool access) have `risk_level` medium or higher and
      name the specific failure mode.
- [ ] **(review)** Skills that put retrieved or third-party content into a model's context
      reference [[ai-ml-security]] instead of re-deriving injection guidance.
- [ ] **(CI: lint, security)** Workflows pass `scripts/audit_workflows.py --strict` (pinned
      SHAs, read-only token, no script injection the static audit can see, no
      `continue-on-error`, no `|| true`, no pipe without `pipefail`); Python passes `ruff`
      including the bandit (`S`) rules; shell passes `shellcheck`; `pip-audit` finds no known
      advisory in the direct pins, the two dependency locks, or the installed dev/example
      environments.

## Tooling gates (repository, not per skill)

| Gate | Command | CI job |
|---|---|---|
| Lint | `ruff check .`, `shellcheck install.sh uninstall.sh bin/ml-ai-skills` | lint |
| Unit tests (stdlib only, Python 3.10 and 3.13) | `PYTHONPATH=tests python -m unittest <tests/test_*.py except test_install* and test_release_archive.py> integration.test_gate` | unit |
| Unit tests with PyYAML/jsonschema cross-checks, branch coverage ≥ 86% including scripts run as subprocesses | same, under `coverage run` (settings in `.coveragerc`), then `coverage combine` | unit-with-dev-deps |
| Installer (ubuntu, macOS with system bash 3.2) | `python -m unittest discover -s tests -p "test_install*.py"` | installer |
| Generated artifacts | `python scripts/check_generated.py` and `--in-place` | generated |
| Dependency locks are complete and consistent; a changed requirements file resolves on its own | `pip install --dry-run --ignore-installed --report <json> -r <file> -c <lock>` (see the job) | deps |
| Release archive is self-contained (fast checks run from the extracted tarball) | `PYTHONPATH=tests python -m unittest test_release_archive` | release-archive |
| Every job above succeeded | the needed jobs' results | all-required |
| Release archive is reproducible, self-contained and verifiable | `python scripts/build_release.py` twice + `cmp` + `--verify-archive`; `ML_AI_SKILLS_RELEASE_SLOW=1 PYTHONPATH=tests python -m unittest test_release_archive` | release.yml `verify` (the fast checks also run in the ci.yml release-archive job) |
| Reference URLs and PyPI latest releases (weekly and manual, report-only, never a merge gate) | `python scripts/verify_online.py` | freshness-scheduled.yml |

## CI wiring

What makes the (CI) gates above fail when their check fails. All of it is configuration
checked locally, with PyYAML, `scripts/audit_workflows.py --strict` and
`tests/test_audit_workflows.py` running the inline scripts. `actionlint` 1.7.12 with
shellcheck 0.11.0 ran on WSL Ubuntu in an earlier round; this round's actionlint run was on
Windows with shellcheck and pyflakes turned off (`-shellcheck= -pyflakes=`). The last
`ci.yml` edit (`ML_AI_SKILLS_EXAMPLE_TIMEOUT` in the examples job) was checked only with
`audit_workflows.py --strict` (0 errors, 0 warnings). None of it has been observed running
on GitHub (`docs/KNOWN-ISSUES.md` #4).

- **One required check.** `all-required` has `needs:` on every job except the manual
  `link-check`, runs with `if: always()`, and fails unless every needed job's result is
  `success`, so a failed, cancelled or skipped job fails it. Branch protection should require
  only this check; a test fails if a job is added to `ci.yml` without being added to
  `needs:`. Branch protection itself is a repository setting, outside this tree.
- **No swallowed failures.** No job or step uses `continue-on-error`, no `run:` uses
  `|| true`, `|| :`, `|| exit 0`, a `|| echo`/`|| printf` that does not go on to fail, or a
  `set +e` without reading `$?`/`PIPESTATUS` afterwards, and every `run:` that pipes (`cmd | tee log`) sets `pipefail` first:
  GitHub's default shell is `bash -e` without it, so `tests | tee log` would pass when the
  tests fail. `scripts/audit_workflows.py` reports all three as WARN (so `--strict` fails),
  with the limits in its docstring. In `freshness-scheduled.yml` the offline scans' exit codes
  are recorded and judged in the final `Result` step instead of being discarded;
  `tests/test_audit_workflows.py` runs that workflow's steps offline (network blocked through
  an unreachable local proxy), and with stub scripts for each failure mode.
- **Least privilege.** Every workflow's top-level token is `contents: read`, and
  `all-required` drops even that (`permissions: {}`). Only `release.yml`'s `publish` job gets
  write scopes, and that workflow never runs on pull requests. No workflow uses
  `pull_request_target` or any secret other than the automatic token.

## Base commit

`check_contract.py --base-ref` (validate) and the routing no-regression check (evals-offline)
compare a change against the commit it started from. The `Resolve the base commit` step
(identical in validate, evals-offline and deps) picks that commit; those jobs check out the
full history (`fetch-depth: 0`) so it is in the clone.

| Event | Base |
|---|---|
| `pull_request`, HEAD is GitHub's test merge commit (second parent = the payload's head sha) | the merge commit's first parent, if it descends from the payload's `base.sha` (which can lag the tip GitHub merged against) |
| `pull_request`, any other HEAD | `git merge-base HEAD <payload base.sha>` |
| `push` | the payload's `before`. `HEAD^1` would compare only the last commit of a multi-commit push, so a regression plus a lowered baseline in an earlier commit would pass |
| `push` that creates the branch (`before` is all zeros), `workflow_dispatch` | the merge base with `origin/<default branch>`, or `HEAD^1` when HEAD is on the default branch |

A payload sha that is not 40 hex characters fails the step. A base that is not in the clone
(a force push leaves the old tip unreferenced) is first fetched by sha with an anonymous
`git fetch origin <sha>`, which GitHub serves for a public repository until the object is
garbage-collected. If that fails too, the step fails; it never silently compares against
another commit. Recovery: push a new commit on top (its `before` is the current tip), or for
a PR push to its branch so the payload is refreshed. Re-running the same run reuses the same
payload, and a `workflow_dispatch` run does not help a release: `release.yml` accepts only a
successful push run of the tagged commit, so a force-pushed tip whose base cannot be fetched
is released by pushing a new commit.
The `ci.yml` routing step fails when `ROUTING_BASE_SHA` is empty and passes it to
`scripts/eval_router.py --gate --base-sha`; run locally without `--base-sha`, the gate
compares against the first parent of HEAD (`HEAD^1`). For uncommitted changes, pass
`--base-sha HEAD` so the comparison is with the commit the changes start from.
`tests/test_audit_workflows.py` runs the step against temporary git repositories for each
row, and runs the routing gate on a two-commit push whose first commit regresses the router
and lowers the baseline.

## Coverage

`unit-with-dev-deps` measures branch coverage of `scripts/` and `schema/` with the settings in
`.coveragerc`: `parallel = True` and `patch = subprocess` (coverage 7.10 or later; the pin is
7.17.0), so scripts the tests launch as `sys.executable scripts/<name>.py` are measured as
well as code the tests import. A child started with a fresh environment, or a copy of
`scripts/` in a temp tree, is still not measured. Before `coverage combine`, a guard counts
the lines measured only in child processes (every child data file's lines minus those of the
parent, the largest file) and fails below `MIN_SUBPROCESS_LINES` (100). Counting data files
is not enough, because a child that measured nothing still writes one; the floor is not
enough either, because the in-process-only total (86.96%) is above it. On `51743b0`
subprocess coverage added 154 statements (Windows) and 153 (WSL) that the in-process run
missed (716 missed before, 562/563 after), so 100 leaves a margin of about a third. That
estimate comes from the before/after reports; the guard itself has only run against
hand-written data files in `TestSubprocessCoverageGuard`, not on a full run's data.

Measured on 2026-10-29 on commit `51743b0` plus `.coveragerc` (the 594 unit tests the CI
command selects; 6,104 statements, 2,640 branches):

| Machine | Without subprocess coverage | With subprocess coverage |
|---|---|---|
| Windows 11, CPython 3.13.2, coverage 7.14.3 (1 skip) | 86.96% | 89.46% |
| WSL Ubuntu 26.04, CPython 3.13.18 (python-build-standalone), coverage 7.17.0, only `requirements-dev.txt` installed (7 skips) | 86.96% | 89.43% |

Neither is a GitHub runner, and the tree has changed since that commit. Policy: the floor in
`ci.yml` (`--fail-under`, now 86) is the lowest measured total minus a 3-point margin for
platform-dependent branches and tests that skip on some platforms, rounded down. Raise it
when coverage grows; never lower it to make a change pass.
