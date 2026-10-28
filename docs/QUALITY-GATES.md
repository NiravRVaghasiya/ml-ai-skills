# Quality gates

A skill is "done" when it passes every gate below. The markers say how each gate is
enforced:

- **(CI)**: a script fails a job in `.github/workflows/ci.yml` (job name in brackets). The
  same scripts can be run locally; **no CI workflow has run on GitHub yet**
  (`docs/KNOWN-ISSUES.md`), so "(CI)" describes the configured gate, not an observed result.
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
      (`scripts/check_contract.py --check`). A breaking change needs a major version
      (`docs/RELEASE.md`).
- [ ] **(CI: plugins)** Every packaged `SKILL.md` is byte-identical to its source and
      `claude plugin validate --strict` passes (`tests/test_generate_plugins.py`, pinned CLI).

## Content
- [ ] **(review)** Purpose is clear from `## Overview` alone.
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
      dependency fails instead of skipping (`ML_AI_SKILLS_REQUIRE_EXAMPLE_DEPS=1`).
      Non-runnable blocks are marked `# illustrative` (`docs/EXAMPLE-TESTS.md`).
- [ ] **(CI: validate)** No known-removed API pattern (`STALE`) in the skill text
      (`scripts/check_freshness.py`). This is a regex heuristic, not verification.
- [ ] **(review)** Stubs in example tests reproduce the real behaviour the code relies on,
      and every fixed defect has a regression test.

## Evaluation
- [ ] **(CI: validate)** Every skill has `evals/<slug>/plan.json`, at least one case, and a
      non-happy-path case; every hard assertion is a valid, non-vacuous regex; every
      case's pass fixtures pass and fail fixtures fail
      (`scripts/validate_evals.py --strict --require-full-coverage`).
- [ ] **(CI: examples)** `kind: executable` cases' test ids pass
      (`scripts/run_evals.py --run-executable`).
- [ ] **(CI: evals-offline)** The routing benchmark's test split meets the parent commit's
      baseline on all seven metrics, and `baseline.json` matches `benchmark.json`'s hash
      (`scripts/eval_router.py`, `docs/ROUTING.md`).
- [ ] **(not gated)** Grading of real model output. Model mode in `scripts/run_evals.py`
      exists but has never been run; pass rates and uplift are unmeasured.

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
      SHAs, read-only token, no script injection the static audit can see); Python passes
      `ruff` including the bandit (`S`) rules; shell passes `shellcheck`; `pip-audit` finds
      no known advisory in the direct pins or the installed dev/example environments.

## Tooling gates (repository, not per skill)

| Gate | Command | CI job |
|---|---|---|
| Lint | `ruff check .`, `shellcheck install.sh uninstall.sh bin/ml-ai-skills` | lint |
| Unit tests (stdlib only, Python 3.10 and 3.13) | `PYTHONPATH=tests python -m unittest <tests/test_*.py except test_install*>` | unit |
| Unit tests with PyYAML/jsonschema cross-checks, coverage ≥ 84% | same, under `coverage run --branch` | unit-with-dev-deps |
| Installer (ubuntu, macOS with system bash 3.2) | `python -m unittest discover -s tests -p "test_install*.py"` | installer |
| Generated artifacts | `python scripts/check_generated.py` and `--in-place` | generated |
| Release build is reproducible | `python scripts/build_release.py` twice, `cmp` | release.yml `verify` |
