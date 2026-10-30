# Contributing

This guide takes one skill from an empty folder to a merged change: author it, validate it,
execute its code, give it eval cases and routing cases, record its checkable claims,
regenerate the derived files, and describe the change. [`CLAUDE.md`](CLAUDE.md) is the
authoring manual (section contract, Definition of Done, guardrails). The binding frontmatter
contract is [`docs/SKILL-SPEC.md`](docs/SKILL-SPEC.md).

All scripts are stdlib-only Python 3.10+. Example tests need the pinned packages in
`requirements-examples.txt` (CPU-only torch comes from the PyTorch index):

```bash
python -m venv .venv-examples
.venv-examples/bin/python -m pip install -r requirements-examples.txt   # Windows: .venv-examples\Scripts\python.exe
python -m pip install -r requirements-dev.txt                           # optional: ruff, PyYAML/jsonschema cross-checks, coverage
```

On Linux x86_64 with CPython 3.13 (as in CI), add `-c constraints/examples-linux-cp313.txt`
to the example install (and `-c constraints/dev-linux-cp313.txt` to the dev one).

## 1. Author the skill

1. Copy `_TEMPLATE/SKILL.md` to `<slug>/SKILL.md`. The slug is lowercase-kebab and equals
   the frontmatter `name`. Never edit the template or the two gold-standard skills
   (`data-preprocessing`, `attention-mechanisms`) unless that is the task.
2. Fill every frontmatter key (`docs/SKILL-SPEC.md`) and replace every `TODO` the template
   leaves (the validator rejects any that remain: `placeholder-text`). `description` starts
   with "Use when the user wants to ...", has at least 3 quoted trigger phrases and a "NOT
   for ..." clause naming the sibling that owns the adjacent topic. It is at most 1,024
   characters and contains no XML-like tags (`<task>`, `</x>`, `List<int>`: an error). Write
   comparisons out rather than using `<` or `>` at all: a bare `a < b` is only a warning
   here, but Anthropic's upload validator rejects it.
3. Write `## Overview`, then `## Workflow` (workflow skills; every step has runnable code) or
   `## Key Concepts` (reference skills), then `## Gotchas` (at least 3, specific) and
   `## References` (at least 2 real links, each with a reason). Use `#`-style headings: a
   heading or bullet inside a code fence or an HTML comment does not count, and a text line
   underlined with `---` or `===` is an error (`setext-heading`).
4. Phrase heuristics conditionally ("as a rule of thumb, above roughly N ..."). See
   `docs/EVIDENCE.md` for the test and worked examples. Pick `evidence_level` and
   `risk_level` for this skill's own content, not by copying a sibling.
5. A code block that cannot run standalone (needs a GPU cluster, a paid API, a running
   service) starts with `# illustrative`. It is then documentation only and does not count
   as executed coverage.

## 2. Validate

```bash
python scripts/validate_skills.py --warnings     # zero errors required; fix warnings or say why not
python scripts/check_freshness.py                # no STALE findings
```

Every finding has a stable `code` and a `fix` hint; the table is in `docs/SKILL-SPEC.md`.

## 3. Execute the skill's code (workflow skills, and reference skills with code)

Add `tests/examples/test_<slug_with_underscores>.py`, following `docs/EXAMPLE-TESTS.md`:

- Run the skill's own blocks with `harness.run_blocks("<slug>", ...)`; never copy the
  code into the test.
- Guard the class with `@harness.requires("numpy", "sklearn", ...)`.
- Inject inputs through `namespace=` and fake services through `stubs=`. Say in the test
  what is stubbed and why.
- Shrink expensive settings only with `replace=`, which must match exactly once.
- Pin every defect you fix with a regression test that fails on the old code.

```bash
ML_AI_SKILLS_EXAMPLE_COVERAGE=coverage.jsonl \
  .venv-examples/bin/python -m unittest discover -s tests/examples -t tests -p "test_<slug>.py" -v
```

A passing example test shows the code runs against the pinned versions with the injected
inputs. It does not show the advice is optimal; do not describe it that way.

## 4. Add eval cases

Follow `evals/SCHEMA.md`. In `evals/<slug>/`:

- `plan.json`: representative tasks, negative cases, assertion strategy, known limitations.
- At least one **non-happy-path** case (`edge_case`, `adversarial`, `misconception`,
  `failure_recovery` or `ambiguity`), with `expected_behavior`, `must_not`, and hard
  assertions (`must_include` / `must_not_include` regexes).
- Fixtures for each case with hard assertions:
  - `<case>.pass.md` and at least one more passing paraphrase (`pass.terse`,
    `pass.negations`);
  - `<case>.fail.md`, plus a `fail.on_topic` wrong answer that names the right concepts and
    is caught by a `must_not_include` pattern.
- For workflow skills, a `kind: executable` case whose `executable_tests` name real
  `tests/examples` test ids.
- If someone who has not read the patterns can write answers for you, keep them verbatim as
  `pass.blind` / `fail.adversarial` fixtures, and record the before-tuning counts in
  `plan.json`. Once you tune against them they are fixtures, not a held-out measurement.

```bash
python scripts/validate_evals.py --strict --coverage     # 0 errors, 0 warnings, skill fully covered
python scripts/run_evals.py --out eval-report.json       # offline fixture self-tests
python scripts/run_evals.py --run-executable --python .venv-examples/bin/python --skill <slug>
```

Never report an eval pass rate for real model output unless a model-mode run produced it.

## 5. Add routing benchmark cases

Add cases for the new skill to `evals/routing/benchmark.json` (label rules in
`evals/routing/README.md`): at least one `typical` case and one `paraphrase` case, in both
the `dev` and `test` splits, and `forbidden` labels where a sibling must not fire.
Give each new case a `family` (a case that restates an existing request joins that family
and takes its split), `labellers` and `adjudication: "single-labeller"`. A test case must
not restate any dev case. See `evals/routing/README.md`.

```bash
python scripts/eval_router.py --split dev             # tune on dev only
python scripts/eval_router.py --split test --json     # look at test in aggregate only
```

Editing the benchmark, or any skill's description or capabilities, can move the metrics.
`tests/test_eval_router.py::TestRegressionFloor` and the CI gate then fail until you
re-baseline on purpose (`evals/routing/README.md`, "Regression floor and re-baselining").
Explain any trade-off in the commit.

## 6. Record checkable claims

When the skill depends on a specific API behaviour or version boundary, add a record to
`docs/claims.json` (fields in `docs/EVIDENCE.md`, "Claims registry"):

- `runtime-test` only if a test you ran exercises it. Name the test as
  `tests/.../test_x.py::Class::method` and record the version it ran against.
- `human-review` only with a named human reviewer and a source URL with its access date.
  Agents do not count as human reviewers.
- Otherwise `unverified`. That is a normal state, not a failure.

```bash
python scripts/check_claims.py
```

## 7. Regenerate derived files and check the contract

Never hand-edit generated files (`INDEX.md`, the README stats block, `plugins/`,
`.claude-plugin/`, `docs/SKILL-GRAPH.md`, `schema/skill-frontmatter.schema.json`,
`schema/public-contract.json`).

```bash
python scripts/generate_index.py --fix
python scripts/generate_plugins.py --write
python scripts/generate_readme_stats.py --write
python scripts/build_dependency_graph.py --write
python scripts/generate_schema.py --write         # after changing schema/allowed_values.py
python scripts/check_generated.py                 # must report no drift
python scripts/check_contract.py                  # prints the current contract JSON, then one 'BREAKING vs lock:'
                                                  # or 'change vs lock:' line per difference (none if it matches)
python scripts/check_contract.py --check          # exit 1 on any difference from the lock (what CI runs)
python scripts/check_contract.py --write          # re-lock; refuses breaking changes without a major bump
```

Removing or renaming a skill, plugin or enum value is a **breaking** change. It needs a
major version and the deprecation steps in `docs/RELEASE.md`.

A new top-level file or directory must be added to `SHIPPED_FILES`/`SHIPPED_TREES` (or an
`EXCLUDED_*` rule) in `scripts/build_release.py`, otherwise `build_release.py` and
`tests/test_release_archive.py` fail with its path. Do not commit files that match an
`EXCLUDED_*` rule (logs, `*.bak`, caches); the build refuses them.

## 8. Run the tests and describe the change

```bash
python -m unittest discover -s tests -p "test_*.py"          # all of tests/ (test_install*.py take ~9 min;
                                                             #   tests/examples skip without their deps)
python scripts/quality_report.py --out quality-report.json   # what was and was not checked
```

Add a line under `## [Unreleased]` in `CHANGELOG.md` (Added / Changed / Fixed / Security /
Deprecated / Removed). Use the commit format `add(<domain>): <slug> skill`, or describe the
fix.

## Review checklist

A reviewer confirms each item by reading the change, not by trusting a passing check:

- [ ] Folder name = `name` = INDEX slug; `type` matches the INDEX tag.
- [ ] `description` triggers on the right requests, and its "NOT for" clause names the
      **right** sibling (CI only checks the clause exists).
- [ ] Section set matches the type; every Workflow step has code that does what the step
      says; non-runnable blocks are marked `# illustrative`.
- [ ] Gotchas are specific failure modes, not generic advice; heuristics are conditional;
      no number is stated without a source or a "measure it" qualifier.
- [ ] References are real and say why each is cited; no invented papers, versions or
      benchmarks.
- [ ] `version_constraints` say how each entry is known (executed against X, or a
      model-knowledge estimate).
- [ ] Example tests run the skill's own blocks, stub only what they must, and pin every
      fixed defect with a regression test.
- [ ] Eval cases include a non-happy-path case. Each case has an on-topic wrong answer
      caught by `must_not_include`, and a negation-heavy correct answer that passes.
- [ ] Routing cases cover the new skill in both splits, and a re-baseline (if any) is
      explained.
- [ ] Claims are recorded with the verification kind actually performed.
- [ ] Skills with side effects (deployment, deletion, external calls, tool access) have
      `risk_level` medium or higher and name the specific failure mode. Untrusted content
      reaching a model points to `ai-ml-security`.
- [ ] Generated files were regenerated, not hand-edited; the contract change is
      intentional; the CHANGELOG is updated.
- [ ] Nothing in the change claims more than was run: "tested", "verified" and
      "production-ready" appear only with the run that backs them.
