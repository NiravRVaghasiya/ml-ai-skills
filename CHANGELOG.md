# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project uses
[Semantic Versioning](https://semver.org/spec/v2.0.0.html) as defined for this
repository in `docs/RELEASE.md`.

## [Unreleased]

Summary of the Phase 1 and Phase 2 hardening work, of the audit-fix round that followed an
independent audit (2026-10-28), and of a remediation round on 2026-10-29/30. Entries
outside the remediation-round blocks come from the engineering reports and from runs on
Windows 11 / Git Bash / Python 3.13 on 2026-10-27 and 2026-10-28. Where a remediation-round
entry gives a newer figure, it replaces the older one. Before this round, only the CI
`examples` job's results had been observed on GitHub.

The remediation round's evidence is local only. Agent workstreams and the coordinating
session ran it on Windows 11 (Git Bash, CPython 3.13.2; some modules also on 3.12.10 and
3.10.22) and on WSL 2 Ubuntu 26.04. No GitHub Actions run was observed for any of this
round's work, so none of the new or changed CI jobs has run on GitHub
(`docs/KNOWN-ISSUES.md` #4). The entries cite targeted runs. On the final working tree the full
default suite (`python -m unittest discover -s tests -p "test_*.py"`) ran 1544 tests, OK
(skipped=233; baseline at 51743b0: 1017, OK, skipped=180). The CI unit-job module set (every `tests/test_*.py` except
`test_install*` and `test_release_archive`, plus `integration.test_gate`) also ran under CPython
3.12.10: 1008 tests, OK (skipped=34). In the remediation-round blocks, "blind",
"labeller" and "reviewer" always mean a model, not a person. For the real-model eval run,
see `docs/KNOWN-ISSUES.md` #1. The local disk filled up during the round. Runs that failed
with `ENOSPC` and were repeated are marked.

### Added

- **Executable example tests** (`tests/examples/`, `docs/EXAMPLE-TESTS.md`). The harness
  runs the fenced `python` blocks of the 26 skills that have runnable code, in document
  order, with injected inputs and documented stubs. The suite has 327 tests, all passing in
  the example venv on 2026-10-28. `ML_AI_SKILLS_EXAMPLE_COVERAGE` records which blocks ran.
  A run without dependencies skips; with `ML_AI_SKILLS_REQUIRE_EXAMPLE_DEPS=1` a missing
  dependency is a failure. Pinned versions are in `requirements-examples.txt`.
- **Behavioural eval framework** (`evals/SCHEMA.md`):
  - `scripts/eval_lib.py`, `scripts/validate_evals.py` (`--strict`, `--coverage`, `--json`,
    `--require-full-coverage`) and `scripts/run_evals.py`, with three modes:
    - offline: fixture self-tests, recorded responses, `--run-executable`;
    - an opt-in model mode that has never been run;
    - `--compare` for regression detection.
  - Coverage grew from 23 cases for 18 skills to 83 cases covering all 38 skills. Every
    skill has a `plan.json` and a non-happy-path case, and all 808 fixture self-tests
    pass (`validate_evals.py --strict`, 2026-10-28).
  - 25 skills have `kind: executable` cases.
  - No model output has been graded; uplift is unmeasured.
- **Routing benchmark** (`evals/routing/`, `scripts/eval_router.py`): 203 labelled cases
  (118 dev / 85 test) across seven categories, seven metrics, a baseline with history, and a
  zero-tolerance regression floor.
- **Freshness and claims tooling** (`docs/FRESHNESS.md`):
  - `schema/freshness_policy.json` with a validated pattern and review-clock policy;
  - `scripts/check_claims.py` and a structured claims registry, `docs/claims.json` (24
    claims, 22 of them `runtime-test` and each naming a test that exists);
  - `scripts/verify_online.py` with a time budget and circuit breaker;
  - `.github/workflows/freshness-scheduled.yml`.
- **Public contract lock** (`schema/public-contract.json`, `scripts/check_contract.py`) and a
  generated JSON Schema for frontmatter (`schema/skill-frontmatter.schema.json`,
  `scripts/generate_schema.py`).
- **CI and release** (`.github/workflows/ci.yml`, `release.yml`, `.github/dependabot.yml`):
  - staged jobs: lint, validate, unit tests on Python 3.10 and 3.13, unit tests with dev
    dependencies under coverage (84% floor), examples, offline evals with a routing gate,
    generated-artifact drift, installer tests on ubuntu and macOS, and plugin validation
    with a pinned `claude` CLI;
  - a reproducible release tarball with `SHA256SUMS` and a build-provenance attestation
    (`scripts/build_release.py`);
  - `scripts/audit_workflows.py`, `scripts/check_generated.py`, `ruff.toml`, `.shellcheckrc`,
    and pinned `requirements-dev.txt` / `requirements-examples.txt`.
- **`scripts/quality_report.py`**: a machine-readable quality report. It combines validator,
  eval, router, freshness and claims results with test outcomes (when run or ingested) and
  example-block coverage. Per-skill status keeps structural validity, executed examples,
  offline eval coverage and human review apart, and every check it did not run is listed
  with the reason. A skill counts as structurally valid only when the validator ran and
  returned JSON; collected test results record the git revision and each `SKILL.md`'s
  SHA-256, and ingested coverage whose block count no longer matches a skill is reported as
  `coverage-stale`. Claim-level human reviews are reported separately and never make a skill
  "human-reviewed". Tests are in `tests/test_quality_report.py`.
- **Documentation:**
  - `docs/INSTALLER.md` (installer contract and threat model);
  - `docs/COMPATIBILITY.md` (supported environments and the test behind each claim);
  - `docs/RELEASE.md` (semver rules, release steps, verification, deprecation policy);
  - `docs/KNOWN-ISSUES.md`;
  - `CONTRIBUTING.md` and `SECURITY.md`.
- **Remediation round (2026-10-29/30):**
  - **First real-model eval run** (`docs/evidence/model-run-2026-10-30.json`,
    `docs/KNOWN-ISSUES.md` #1). 84 cases x 2 arms through `--provider claude-cli --model
    sonnet`, an opus judge, one sample each: 336 calls, all ok, CLI-reported cost $6.13.
    Judge-dependent "correct": 0.583 with the skill vs 0.452 without (18 cases better, 7
    worse, 59 tied); regex structural pass 0.714 vs 0.667; critical errors 10 vs 11. The judge
    is not calibrated against people, so these are model judgements from a single sample.
  - **Eval model mode** (`scripts/eval_backends.py`, `scripts/eval_grading.py`,
    `docs/EVALUATION.md`). There are backends for the Anthropic Messages API, `claude-cli`
    and an offline `replay`. Grading is layered (regex structure, per-item rubric, optional
    judge) and combined by the documented `VERDICT_RULES`; a regex-only pass is
    `structural-pass`, never `correct`. A `--max-cost-usd` ceiling is required. It is checked
    before any call and on every call, and a paid call of unknown cost stops the run. It is
    not a hard cap. It applies to one invocation (`--resume` starts again from $0), and an
    `anthropic` call that retries after connection errors can overshoot it by up to
    `--max-retries` worst cases (`docs/EVALUATION.md`, KNOWN-ISSUES #1). Also
    `--dry-run`, `--samples`, `--run-dir` / `--resume`, explicit attempt statuses, and paired
    uplift with Wilson 95% intervals. Tested with offline fakes (`tests/test_run_evals.py`,
    `tests/test_eval_grading.py`). For the real-model run, see `docs/KNOWN-ISSUES.md` #1.
  - **Held-out grader set** (`evals/_grading/heldout/manifest.jsonl`; `run_evals.py
    --grade-heldout` / `--grade-labelled`): 249 answers for the 83 cases of the time. Blind
    model writers saw only each case's input and rubric. Two model labellers labelled every
    answer (agent-adjudicated, not human). The regex layer scored TPR 0.855
    [0.764, 0.915] (n=83) and TNR 0.916 [0.863, 0.949] (n=166), Wilson 95%. The labellers
    agreed with the writers on all 249 answers, so the set is probably easier than real
    model output (KNOWN-ISSUES #33). The dev and calibration manifests are empty.
  - **Eval fixtures and cases**: 32 `fail.dev-*`, 18 `pass.dev-borderline*` and 12
    `fail.fixprobe` fixtures, so every `must_not_include` pattern has a fail fixture that it
    catches (KNOWN-ISSUES #29). New executable case:
    `evals/data-pipelines/ds_partition_migration.yaml`. On 2026-10-30,
    `validate_evals.py --strict --strict-advisories --coverage` reported 84 cases, 880/880
    fixture self-tests and 0 advisories.
  - **Routing benchmark** (`evals/routing/benchmark.json`):
    - every case has `family`, `labellers` and `adjudication`, and optionally `label_review`.
      `eval_router.split_integrity_errors()` checks them;
    - 41 dev cases for documented failure modes, written by a model that was not blind to the
      router;
    - 115 cases from blind model writers (78 test, 37 dev), each labelled by a second blind
      model reviewer;
    - two blind model reviewers re-labelled the original 85 test cases: 83 agreed exactly,
      and 2 were adjudicated;
    - 359 cases in total: 217 dev, 142 test, 85 test families.
  - **Routing metrics and gate** (`scripts/eval_router.py`):
    - new metrics: `negative_false_activation_rate` (every case with an empty `expected`;
      gated), abstention rates, no-skill precision and recall, Wilson intervals, `per_class`,
      `confusion` and `n_families`;
    - `--gate` with `--base-sha`, `--event-name`, `--head-sha`, `--report-dir` and
      `--summary-file`;
    - value-pinned `accepted_regressions` in `evals/routing/baseline.json`;
    - `evals/routing/synonym-audit.json`, `tests/test_router_gate.py` and
      `tests/test_router_synonym_audit.py`;
    - the router also indexes the bold lead-ins of bullets (`headings`), and its `--json`
      output reports `domain` and `ignored_directives`.
  - **Skill export** (`scripts/export_skills.py`, `tests/test_export_skills.py`):
    - `--target agentskills-spec|skills-api|claude-ai-upload` writes into a new directory;
    - repo-only frontmatter keys move into `metadata`, and each move is listed in
      `export-report.json`;
    - links and traversal are refused, and `--zip` output is deterministic;
    - dry runs reported all 38 skills valid for each target, and the opt-in upstream checks
      (anthropics/skills `quick_validate.py`, skills-ref 0.1.1) passed locally on 2026-10-29;
    - no real upload was made.
  - **`data-pipelines` Workflow step 7**: the `customer_features` table contract, and a
    dry-run-first `ds` -> `as_of` migration (`migrate_ds_to_as_of`, `legacy_window_matches`,
    `migration_report`, the `read_customer_features` dual read, the Feast `timestamp_field`
    change). `TestDsToAsOfMigration` in `tests/examples/test_data_pipelines.py` (12 tests)
    passed locally against SQLite with a stubbed Airflow.
  - **Opt-in tests**:
    - `tests/integration/`, enabled per area with `ML_AI_SKILLS_INTEGRATION_<NAME>=1`. Once
      enabled, missing infrastructure fails instead of skipping;
    - `tests/test_claude_cli_integration.py`: the full plugin lifecycle on a temporary clone
      with an isolated config. It passed once on Windows with claude 2.1.312.1459 (an ASBX
      internal build), after a first attempt failed on a full disk.
  - **New default example tests** (not opt-in):
    `tests/examples/test_data_pipelines_object_store.py` and `tests/examples/test_cnn_vision.py`
    (its PyTorch half). Their opt-in counterparts are
    `tests/integration/test_s3_publish.py` (`ML_AI_SKILLS_INTEGRATION_S3=1`) and
    `tests/integration/test_tensorflow.py` (`ML_AI_SKILLS_INTEGRATION_TF=1`).
  - **Installer tests**: `tests/test_install_integrity.py`, `tests/test_install_signals.py`
    (SIGINT, SIGHUP, SIGTERM) and `tests/test_install_platform.py` (missing tools, mount
    points, a static bash 3.2/BSD check). On WSL 2 Ubuntu (bash 5.3.9, Python 3.14.4) all 116
    installer tests passed with 0 skipped, including real tmpfs and bind mounts. On Windows,
    7 skipped and 1 failed with `ENOSPC`; that test passed on re-run.
  - **Release**: `build_release.py --verify-archive TARBALL [--extract-to DIR] [--no-sums]`.
    It checks integrity only; authenticity needs `gh attestation verify`. New tests:
    `tests/test_release_archive.py` and `tests/test_release_gitattributes.py`.
  - **CI** (`.github/workflows/ci.yml`; none of it has run on GitHub yet):
    - an `all-required` job that fails unless every other required job succeeded;
    - a `deps` job, and Linux/CPython 3.13 locks in `constraints/` that every CI install
      uses with `-c`;
    - a `release-archive` job;
    - subprocess coverage (`.coveragerc`), which must measure at least 100 lines that only
      child processes ran;
    - `audit_workflows.py` checks for `continue-on-error`, swallowed failures and pipes
      without pipefail;
    - the `validate` job runs `validate_evals.py --strict-advisories` and
      `export_skills.py --target claude-ai-upload`;
    - the plugins job runs the claude CLI lifecycle test.
  - **Validator** (`scripts/validate_skills.py`, `scripts/skills_lib.py`):
    - new errors `placeholder-text` and `setext-heading`;
    - new warnings `capability-near-duplicate` and `description-angle-bracket`;
    - `skills_lib.scan_markdown`;
    - cross-checks against PyYAML, ruamel.yaml and markdown-it-py, which run where those
      packages are installed.
  - **Freshness and claims**:
    - `check_freshness.py --claims`;
    - `verify_online.py --breaker-on` and a per-request watchdog;
    - `expires_on` for acknowledgements;
    - `docs/claims.json` grows from 26 to 36 claims (26 `runtime-test`, 10 `unverified`).
      On 2026-10-30, `check_claims.py` reported 0 errors and 1 review-due finding.
  - **Quality report**:
    - `docs/reviews.json` holds per-skill human reviews. It has no records, because no
      per-skill human review has been done;
    - example tests are attributed to skills;
    - `--require-example-deps` and `--require-coverage-revision`;
    - example coverage records use schema v2, which adds git and SKILL.md provenance
      (`tests/examples/harness.py`).
  - `tests/examples/harness.py`: `slow()` and an opt-in watchdog,
    `ML_AI_SKILLS_EXAMPLE_TIMEOUT`. The CI `examples` job now sets it to 900 s; that job has
    not been observed on GitHub with it (KNOWN-ISSUES #4).

### Changed

- **README**: rewritten for first-time readers. It now opens with what a skill is, a
  two-command quick start and a domain overview. A short checked/not-checked table
  replaces the long Evidence and Limitations sections, and the maintainer tooling and
  project structure are in collapsed sections. Stale numbers were dropped (24 claims, a
  327-test run, "CI has not run"); the generated stats block and the links to
  `docs/KNOWN-ISSUES.md` carry the details. The CI badge now shows the live workflow status.
- **Router** (`scripts/router.py`, `docs/ROUTING.md`): rewritten from a keyword matcher into
  a deterministic lexical scorer. It uses Porter stemming, BM25-style IDF over skill
  profiles, per-field weights, a domain anchor, confidence gating and negation scoping.
  - Task accuracy over all splits went from 0.5419 to 0.8424 (test split 0.8000).
  - The false-activation rate on the `no-skill` category (plainly off-topic queries) went
    from 1.0 to 0.0455 (test 0.0). That metric does not cover adversarial off-topic queries:
    6 of the 10 adversarial test cases that need no skill (all homonyms) still
    select one (`docs/ROUTING.md`).
  - Weights were tuned on dev only.
  - The synonym map is partly fitted to dev and adds nothing on test (documented).
  - The interface (`route()`, `route_details()`, the CLI) is unchanged; `route()` can now
    return no hits.
- **Frontmatter parser and validator** (`scripts/skills_lib.py`,
  `scripts/validate_skills.py`): a documented YAML subset that diagnoses rather than guesses.
  Every finding has a stable code, file, line and fix. The validator enforces the Claude
  Agent Skills limits on `name` and `description`, and offers `--json`, `--root` and `--today`.
- **Installer** (`install.sh`, `uninstall.sh`, `docs/INSTALLER.md`):
  - manifest format 2 records a SHA-256 per installed `SKILL.md`, so local edits are kept
    unless `--force` (narrowed with `--skill`);
  - skills removed upstream are reported as orphans and removed only with `--prune`;
  - each skill is committed with two renames (old copy moved aside, new copy moved in) with
    rollback on failure or interruption, and a lock serialises runs;
  - documented exit codes: 0, 1, 2 usage, 3 lock held.
- **Skill content:** every code-bearing skill was audited by running its code, and the
  reference skills were checked against their cited sources. The per-skill findings are in
  `docs/SKILL-AUDIT.md`. Several skills now pin the API generation they were tested against:
  - scikit-learn 1.9: `FrozenEstimator`, no `SVC(probability=True)`, seeded `TargetEncoder`;
  - Transformers 5, TRL 1.16 and Airflow 3;
  - evidently 0.7 and MLflow aliases;
  - anthropic SDK 2.x, where `temperature` is not passed.
- **`data-pipelines` schema change (affects existing users):** the documented
  `customer_features` partition column is renamed from `ds` to `as_of`, and the window is now
  anchored on the run's `data_interval_end` (half-open `[end-30d, end)`, `event_timestamp` at
  the window end). A table or Feast source created from the earlier version of the skill
  needs a migration (rename the column, or keep the old name in your copy).
- The marketplace description derives its skill count instead of hardcoding "38".
- `generate_index.py --fix` and `generate_readme_stats.py --write` now always write LF line
  endings. Only Windows output changes; it was CRLF before
  (`tests/test_check_generated.py::TestGeneratorsWriteLF`).
- README stats and docs no longer describe evals as "structural sanity only", the router
  as keyword-based, or the plugin route as "live-verified". The test-count row separates
  the stdlib default suite from the optional example suite.
- `CLAUDE.md`: the description template no longer uses angle-bracket placeholders, which the
  `description-xml-tag` rule rejects. The Definition of Done now lists the eval, example,
  routing, claims and contract commands.
- **Remediation round (2026-10-29/30):**
  - **Router** (`scripts/router.py`, `docs/ROUTING.md`):
    - the synonym map was re-audited with a same-passage rule. 48 of 77 keys were removed
      (29 remain), including `nan`, `bot`, `assistant`, `language model` and `chatbot`;
    - the domain gate counts curated metadata phrases as anchors and ignores everyday senses
      ("model railway", "train station");
    - a deverbal-suffix rule recognises sentence-initial compounds;
    - six skill descriptions were reworded, based on dev-split failures only:
      `ai-ethics-fairness`, `ai-ml-security`, `rag-pipeline`, `agents-and-tools`,
      `llm-evaluation` and `training-deep-models`;
    - before tuning, 21 test cases that restated a dev request moved to dev.
  - **Routing results** (held-out test split, 142 cases, top_k 5, local run). Each figure is
    the new router first, then HEAD's router on the same skill metadata:
    - task accuracy 0.711 [0.632, 0.779] vs 0.655;
    - false activation over the 60 cases that expect no skill 0.200 vs 0.317;
    - recall 0.700 vs 0.720, and top-1 0.549 vs 0.573;
    - 12 cases are right only with the new router and 4 only with HEAD's. Exact McNemar
      p = 0.077, which is not significant (and families break its independence assumption);
    - `baseline.json` was re-recorded from an `eval_router.py` CLI run on 2026-10-30, and the
      old entry moved to `history`.
  - **Accepted routing regressions against the base commit**: on base 51743b0's benchmark,
    recall falls below its floor (0.8571 vs 0.8701), and so does top-1 (0.7727 vs 0.7879).
    The cases are amb-007, con-004 and mul-002. Both drops are listed in
    `accepted_regressions` with their exact values. Locally on 2026-10-30,
    `eval_router.py --gate --base-sha HEAD` exited 0 with two warnings. The drops were not
    tuned away, because those are test cases (KNOWN-ISSUES #32).
  - `test_current_floor_not_below_pre_improvement` now compares only baseline entries that
    share a `benchmark_sha256`. The pre-improvement keyword router (8328c60) was re-measured
    on the 359-case benchmark and recorded in `history`. Over all splits it gets task
    accuracy 0.410 and recall 0.725; the current router gets 0.780 and 0.802.
  - **CI**:
    - the routing step runs `eval_router.py --gate` against a resolved base commit (the PR
      base, a push's `before`, or the merge base). The contract check uses the same base;
    - a base that is missing after a force push is fetched by sha;
    - executable evals run with `--require-executed`, and the examples job fails if any
      example test is skipped;
    - the coverage floor rises from 84% to 86%;
    - Dependabot opens one PR per example pin, except for the `torch` and
      `transformers-tokenizers` groups;
    - `freshness-scheduled.yml` checks the scans' exit codes instead of discarding them with
      `|| true`;
    - `release.yml` installs ruff and shellcheck from the dev lock.
  - **Release archive** (`scripts/build_release.py`, `docs/RELEASE.md`,
    `docs/REPRODUCIBILITY.md`):
    - the archive is now a self-contained developer distribution. It also ships `tests/`,
      `evals/`, `.github/`, `_TEMPLATE/`, `constraints/`, `CLAUDE.md`, the requirements files
      and the tool configs;
    - the build runs `check_generated.py` first and writes its outputs atomically, with
      `SHA256SUMS` last. The gzip header is fixed. Unclassified files, links, submodules and
      non-portable names are refused;
    - `release.yml` requires an annotated tag with a GitHub-verified signature;
    - on one Windows machine, CPython 3.10.22, 3.12.10 and 3.13.2 built identical tar and
      tar.gz bytes for the same tree (in memory, because the disk was full, 2026-10-30).
      The tar.gz bytes match only with the same deflate implementation (zlib-ng gave
      different bytes). Other operating systems were not measured (KNOWN-ISSUES #14);
    - the slow archive gate has not passed end to end yet (KNOWN-ISSUES #24).
  - **Installer** (`install.sh`, `uninstall.sh`, `scripts/list_skills.py`,
    `docs/INSTALLER.md`):
    - manifest format 3 records a SHA-256 for every file in each installed skill folder. If
      any file was changed, deleted, renamed or added, the skill is kept unless `--force`, and
      the warning names the files;
    - a skill folder that is, or contains, a mount point is refused. Detection uses device ids
      and, where it is readable, `/proc/self/mountinfo`;
    - `openssl dgst -sha256` is a fallback hash tool;
    - file names with control characters are refused;
    - `list_skills.py` writes UTF-8 to pipes and files unless `PYTHONIOENCODING` is set, and
      lists the modified files.
  - **Evals**:
    - the report has `schema_version` 2 (provenance, layered verdicts, uplift), and
      `--compare` reads versions 1 and 2;
    - `eval_lib.HARNESS_VERSION` is 1.2.0;
    - several case YAMLs got rewritten, widened or new `must_not_include` patterns (see
      Fixed);
    - the `KNOWN_UNEXERCISED_MUST_NOT_INCLUDE` ratchet in `tests/test_evals.py` is gone,
      because its backlog is empty.
  - **Validator and template**:
    - `description-xml-tag` follows an HTML-tokenizer reading of "XML tag": it now also
      catches `<K,V>`, `<!-- -->` and `<?xml ?>`, and a lone `<` or `>` is only a warning;
    - `unbalanced-fence` means a fence that is never closed, including `~~~` fences;
    - section and step checks ignore headings inside fences and HTML comments;
    - `_TEMPLATE/SKILL.md` uses `TODO` placeholders;
    - `SECURITY.md` gives the private-reporting steps and has a single
      `Security contact: PENDING` line (KNOWN-ISSUES #23).
  - **Quality report** (`scripts/quality_report.py`), schema version 2:
    - `examples.status` is `executed-and-passed` only when full current coverage and passing
      attributed results share the SKILL.md sha256;
    - block coverage moved to `examples.coverage_status`;
    - several results files of one suite are merged;
    - `git_dirty` covers tracked files only, and `git_untracked_files` counts the rest;
    - the router metric directions come from `eval_router`.
  - **Freshness** (`scripts/check_freshness.py`, `scripts/verify_online.py`):
    - a STALE pattern fails CI only when its backing claim is `runtime-test` or
      `human-review`. Any other backing is reported as REVIEW_DUE;
    - PEP 440 `<V` / `>V` follow packaging 26.1 and later. A differential test against
      packaging 26.3 found 0 disagreements;
    - `verify_online.py` never starts a request with under 1 s of budget left, and caps each
      timeout at the remaining budget (KNOWN-ISSUES #19). Its breaker also counts 5xx and
      network failures;
    - twelve version lower bounds in skill `version_constraints` now cite release notes or
      source at release tags. None of those bounds was executed.
  - **Plugins**: `scripts/generate_plugins.py` copies each `SKILL.md` as raw bytes. The CLI
    plugin tests isolate HOME, USERPROFILE, APPDATA and the XDG dirs as well as
    CLAUDE_CONFIG_DIR, and drop provider credentials.
  - **Pins**:
    - `requirements-examples.txt` pins `wrapt==2.5.1` (pip had resolved the pre-release
      2.5.2rc1) and `fsspec==2026.7.0`;
    - `requirements-dev.txt` pins `packaging==26.3` and `markdown-it-py==4.2.0`;
    - `.gitignore` covers `dist/`, coverage data and `htmlcov/`.
  - **Git index**: the 23 tracked shebang files (`install.sh`, `uninstall.sh`,
    `bin/ml-ai-skills` and 20 scripts) are staged as 100755 (KNOWN-ISSUES #13). This is not
    committed yet. New shebang scripts such as `scripts/export_skills.py` need
    `git add --chmod=+x`.
  - **Breaking changes and migrations.** Nothing has been released, so these affect a
    checkout of this branch and downstream tooling:
    - **Installer manifest format 3**:
      - checkouts from before this change refuse it ("declares format 3");
      - formats 1 and 2 are still read, and the next `install.sh` run upgrades them;
      - to go back to an older checkout, run the current `uninstall.sh` first;
      - `stat` is now required;
      - uninstall keeps a format-1/2 entry whose folder holds more than `SKILL.md`, unless
        `--force`;
      - on Linux, a mount point inside a skill folder is refused even with `--force`.
    - **Release archive contract**:
      - the tarball's contents and `.tar.gz` hash change;
      - any unclassified file stops the build, and so does a tracked file that matches an
        `EXCLUDED_*` rule. Add the file to `SHIPPED_*` or `EXCLUDED_*`, or `git rm --cached` it;
      - `build_release.py` runs the generated check by default (skip it with
        `--skip-generated-check`);
      - `--verify-archive` needs the sibling `SHA256SUMS` (or `--no-sums`);
      - Windows device names, trailing dots and case-colliding paths are refused;
      - `release.yml` refuses lightweight or unsigned tags.
    - **Routing benchmark schema**:
      - every case in the shipped benchmark needs `family`, `labellers` and `adjudication`;
        `label_review` is optional;
      - a family may not span dev and test;
      - `validate_benchmark()` still treats these keys as optional, and `"version"` stays 1;
      - `baseline.json` gains `accepted_regressions`. Only an exact match (base benchmark
        sha256, metric and accepted value) becomes a warning. A further drop, another base or a
        malformed entry fails the gate.
    - **Router selections** change for requests that relied on removed synonym keys ("NaN
      values", "support bot", "language model", "chatbot", "held out"). `route()`,
      `route_details()` and `analyze_task()` stay compatible, and their result types gained
      fields with defaults.
    - **`eval_router.py`**:
      - new options `--gate`, `--base-sha`, `--event-name`, `--head-sha`, `--report-dir`,
        `--summary-file` and `--max-negative-false-activation`;
      - the JSON report gained keys;
      - in GitHub Actions the gate fails on an empty `ROUTING_BASE_SHA`. Locally it falls back
        to `HEAD^1`.
    - **`run_evals.py` model mode**, which had never been run before this round:
      - paid providers need `--max-cost-usd`;
      - `--provider anthropic` also needs `--price-input-per-mtok` and
        `--price-output-per-mtok`, and so does `claude-cli` with `--no-cli-budget-flag`;
      - a judge on another model needs `--judge-price-*`;
      - judge options without model mode, `--responses` or `--grade-*` exit 2;
      - a worst case above the ceiling is refused unless `--allow-partial-budget` is given;
      - `--judge-max-tokens` defaults to 2048;
      - `--require-executed`, `--slow-examples` and `--require-example-deps` without
        `--run-executable` exit 2;
      - `prompt_template_sha256` changed;
      - pass `--claude-cli-arg=<flag>` (with `=`) for values that start with `-`.
    - **`validate_evals.py --strict`**:
      - it now exits 1 when any `must_not_include` pattern is unexercised (no fail fixture
        fires it), and `--strict --json` reports `ok: false`. Migration: give each new pattern
        a fail fixture that it catches, or fix or remove the pattern;
      - a pattern was removed from `experiment-tracking/teammates_cannot_see_runs`, so that
        case's assertion ids shift from [1..3] to [0..2]. Re-baseline any `--compare` report
        made before this change;
      - not a validator rule: `tests/test_evals.py` now requires a fixture label that starts
        with `dev` to have the form `dev-(confident|partial|borderline)[-suffix]` on a fail
        fixture and `dev-borderline[-suffix]` on a pass fixture.
    - **Validator**:
      - an unfilled copy of `_TEMPLATE/SKILL.md` now fails `placeholder-text`;
      - these are now errors: a setext heading, an unclosed fence, `<K,V>` in a description,
        and unquoted `0o17`, `089`, `1e3` or `+.5`;
      - migration: fill in the TODOs, use `## Heading`, close fences, and quote such values;
      - all 38 skills pass with 0 errors and 0 warnings (2026-10-30).
    - **`data-pipelines` `ds` -> `as_of`**: if you built `customer_features` from an
      earlier version of the skill, follow Workflow step 7:
      - run `migrate_ds_to_as_of` as a dry run first. It maps `as_of = ds + 1 day` for
        Airflow 2 `@daily` tables (check this with `legacy_window_matches`);
      - it refuses to apply while `ds` is NOT NULL or in the primary key;
      - use `read_customer_features` during the transition, and change Feast's
        `timestamp_field`;
      - this is tested on SQLite only (KNOWN-ISSUES #25).
    - **Quality report JSON** `schema_version` 2: read `skills[].examples.coverage_status`
      instead of checking `examples.status == "all-runnable-blocks-executed"`. Coverage records
      without `skill_sha256` are `coverage-unattributable`.
    - **Freshness and claims**:
      - `check_freshness.py` exits 2 when `docs/claims.json` is missing or invalid;
      - the `verify_online.py` breaker opens on 5xx and network failures by default. For the
        old behaviour, pass `--breaker-on throttle,timeout`;
      - the claim id `bagdasaryan-shmatikov-2019-dp-disparate-impact` is now
        `bagdasaryan-2019-dp-disparate-impact`.
    - **CI**:
      - every pin bump must regenerate the Linux locks in `constraints/` in the same PR
        (commit the `dependency-locks` artifact);
      - the coverage step needs `.coveragerc` and coverage 7.10 or later;
      - branch protection should require "All required checks passed".
    - **Contributors**:
      - a new runtime `skipTest` in `tests/examples` must either fail under
        `ML_AI_SKILLS_REQUIRE_EXAMPLE_DEPS=1` or be classified in
        `tests/examples/test_harness.py`;
      - `python -m unittest discover -s tests` also finds `tests/integration/`. Its 20 opt-in
        tests skip by default.

### Fixed

- **experiment-tracking**: the serialization gotcha described MLflow 3.16.1, where
  `mlflow.sklearn.log_model`/`save_model` default to `serialization_format="skops"`. The
  pinned 3.17.2 defaults to `None`, resolved at call time to `"skops"` (`"cloudpickle"` on
  Databricks), so the CI `examples` job failed `test_serialization_defaults_match_the_gotcha`.
  The gotcha now describes 3.17.2. The test also logs a model without the argument and checks
  the format that was written. `test_experiment_tracking` and `test_ci_cd_for_ml` pass
  (22 tests) against mlflow 3.17.2 (Windows, Python 3.13.2, 2026-10-28).
- **Example dependencies**: the merged Dependabot `example-deps` bump (PR #3) left
  `requirements-examples.txt` unresolvable, so the CI `examples` job failed at
  `pip install`. pip reports one conflict at a time, and the file had three:
  - `pyarrow==26.0.0`: mlflow 3.17.2 requires `pyarrow<26`. Now 25.0.1.
  - `albucore==0.3.1`: albumentations 2.0.8 pins `albucore==0.0.24`. Back to 0.0.24.
  - `plotly==7.1.0`: evidently 0.7.23 requires `plotly<6`. Back to 5.24.1.

  `.github/dependabot.yml` ignores those ranges. The whole file resolves with
  `pip install --dry-run --ignore-installed` (Windows, CPython 3.13.2, 2026-10-28).

  The same bump took pandas from 2.2.3 to 3.1.0, and the next CI run had 2 errors:
  - **ml-monitoring**: evidently 0.7.23 (the latest release) fails to import on pandas 3.
    It imports `pandas.core.dtypes.common.infer_dtype_from_object`, which pandas 3.0
    removed, although its metadata allows any `pandas>=1.3.5`. pandas is back to 2.2.3, and
    Dependabot ignores `pandas>=3`. The skill's install line now adds `"pandas<3"`, with a
    new gotcha and a claim in `docs/claims.json` (unverified: from the CI traceback only).
  - **data-pipelines test**: pandas 3.1 wraps the failed INSERT's `DBAPIError` in
    `pandas.errors.DatabaseError`. `test_failed_insert_rolls_back_the_partition_delete`
    now accepts either type. The skill was already correct: `engine.begin()` rolls back
    on any exception.

  `test_ml_monitoring` no longer leaks its temp directory and environment patch when
  `setUpClass` fails. These test changes passed ruff and import cleanly, but no test was
  run locally: pandera and evidently are not installed there.
- **rag-pipeline**: `retrieve()` indexed the chunk store with every FAISS id. FAISS pads
  short results with `-1` (and returns all `-1` on an empty index), so a dict store raised
  `KeyError: -1` and a list store silently returned the last chunk. `-1` ids are now
  dropped. Also fixed: a NameError on an undefined `query`, unvalidated `overfetch=0`, the
  context budget ignoring joiners, a persisted index never checked against the running
  config, and the deprecated sentence-transformers dimension alias.
- **Workflows that could not run as written**, now executed end to end in `tests/examples`:
  - undefined names in `feature-engineering`, `recommender-systems`, `computer-vision`,
    `pytorch-patterns`, `ml-monitoring` and `nlp-tasks`;
  - APIs removed upstream: TRL `max_seq_length`, Transformers `evaluation_strategy` and
    the `summarization` pipeline, Airflow 3 `schedule_interval`, the evidently 0.7 imports,
    and anthropic SDK `temperature`;
  - `data-pipelines` SQL that failed on every connection `pandas.to_sql` supports.
- **Silent wrong results:**
  - `supervised-learning` shipped a hard-coded model regardless of the comparison, and
    ranked NaN CV scores arbitrarily;
  - the `hyperparameter-tuning` MedianPruner could never prune;
  - `explainability` SHAP shapes for random forests, and the KernelExplainer/Pipeline crash;
  - `computer-vision` double ImageNet normalisation and COCO xywh boxes fed as xyxy;
  - `time-series`: STL went all-NaN after partial interpolation, and MAPE exploded on zeros;
  - `ml-problem-framing`: the leakage check was an `assert` removed by `python -O`;
  - `ci-cd-for-ml`: the leakage test was a tautology;
  - `rag-evaluation`: nDCG exceeded 1 with duplicate ids, and MRR was deflated by
    unlabeled queries;
  - `data-preprocessing` dropped bool/string columns and encoded `None` as a category;
  - `training-deep-models`: the direction of the scheduler-granularity Gotcha was wrong.
- **Factual corrections in reference skills**:
  - self-attention is permutation-equivariant, not invariant;
  - ResNet degradation is attributed to optimisation difficulty, not vanishing gradients;
  - the four-fifths rule compares each group with the highest-rate group;
  - the impossibility results were attributed to the wrong papers;
  - the CLT needs finite variance;
  - t-closeness defends against skewness and similarity attacks;
  - DQN did not introduce experience replay;
  - RMSprop's square root was missing;
  - the bidirectional-RNN `h_n` gather rule.
- **Validator**: flow lists were read as strings, hyphenated keys were silently dropped,
  duplicate keys silently overwrote, escapes and inline comments were mishandled, and the
  code-block extraction did not dedent list-indented fences.
- **Router**: negated symptoms ("my network is not training", "why isn't my LSTM learning")
  and avoided problems ("split my data without leakage") were treated as exclusions;
  `eval_router.py --router PATH` crashed on dataclass modules.
- **Evals**: dozens of hard-assertion patterns accepted wrong answers or rejected correct
  ones. They were found by independent probes and fixed, and the probe answers were kept as
  fixtures. Rubric corrections: the `torch.load` default is version-dependent, temperature
  is not a universal control, the fairness criteria "cannot all hold at once" rather than
  being pairwise incompatible, and an agent setting `confirmed=True` itself is
  self-attestation, not consent.
- `docs/REPRODUCIBILITY.md` no longer presents `temperature=0` as a control every API offers.
- **Audit-fix round (2026-10-28):**
  - frontmatter parser (`scripts/skills_lib.py`): a `}` inside a single-line flow list made
    it loop forever, hanging the validator, router, eval validator and `list_skills.py`; it
    now terminates with a diagnostic. Plain scalars are right-stripped of spaces and tabs
    only (a trailing NBSP made `name: x<NBSP>` validate as `x`), and the block header `|-+`
    and the merge key `<<` as a value, which PyYAML rejects, are now rejected too;
  - **rag-pipeline**: a reply truncated at `max_tokens` passed the step-8 check and was
    served as verified; `generate_answer` now returns the `stop_reason` and step 8 rejects
    any answer that did not end normally. Duplicate detection keyed on `doc_id` across all
    tenants, so one tenant's document silently dropped another tenant's with the same id; it
    now keys on `(tenant, doc_id)`;
  - **data-preprocessing**: the `None` -> NaN mapping ran outside the persisted pipeline, so
    `None` and NaN got different predictions at serving time; it is now a pipeline step;
  - **data-pipelines**: the quality-gate task was created outside the DAG, so it got no
    `default_args` (no retries, no failure callback); it now passes `dag=dag`;
  - **ml-monitoring**: prediction drift used in-sample training scores as the reference,
    which makes PSI fire on undrifted traffic; the reference rows must now be held out;
  - `scripts/list_skills.py` crashed with `UnicodeEncodeError` on Windows when stdout was a
    pipe, Git Bash or a CI log; output now falls back to backslash escapes;
  - `uninstall.sh` rewrote a format-1 manifest as format 2 even when it removed nothing; it
    now keeps format 1;
  - an `unsupervised-learning` eval pattern for "scaling isn't necessary" had a missing
    space and could never match;
  - evals: the fixture self-test now reports a `must_not_include` pattern that no fail
    fixture fires; the `explainability` SHAP case no longer cites the old "additivity
    breaking" gotcha or accepts plain per-feature permutation importance as the fix;
  - `scripts/check_claims.py` crashed with `TypeError` when reporting a STALE pattern with no
    backing claim; `run_evals.py --responses` could not load a `--save-responses` file
    containing U+2028/U+2029/U+0085 (it now splits on `\n` only); the PEP 440 subset in
    `check_freshness.py` dropped the epoch from a `~=` prefix, so it never matched;
  - `scripts/list_skills.py` no longer tells the user to re-run `install.sh` for a format-2
    entry that `install.sh` already failed to prove unedited;
  - CI: the example gate now fails on a runnable `python` block that no example test
    executed (`scripts/check_example_coverage.py`); push runs are grouped per commit, so a
    newer push to `main` no longer cancels the run a tagged commit needs; the routing
    no-regression check against the parent commit no longer stops with a warning when a PR
    adds a skill (parent cases are carried over to the current skills, and a benchmark that
    still does not validate fails the step); `check_contract.py --check --base-ref HEAD^1`
    applies the major-bump rule against the base commit's lock, so hand-editing
    `schema/public-contract.json` in the same change no longer bypasses it;
  - skill content: `ci-cd-for-ml` gates the registry version it promotes and its example
    lockfile pins `mlflow==3.17.2` instead of 2.16.2; `model-deployment` no longer presents
    a rolling update as a canary; `experiment-tracking` states the MLflow 3.16.1 sklearn
    serialization defaults (`skops` for `log_model`, `cloudpickle` for autolog);
    `llm-evaluation` is `risk_level: medium` and flags its same-model judge;
    `prompt-engineering` no longer assumes every API accepts sampling parameters; and
    membership-inference and memorization requests now route to `data-privacy`, which owns
    that content, instead of `ai-ml-security`;
  - docs: the router's false-activation figure now says it covers only the `no-skill`
    category (6 of the 10 adversarial test cases that need no skill still select one); the
    README no longer calls the installer's two-rename swap "one rename step";
    `docs/RELEASE.md` and KNOWN-ISSUES #13/#24 match what `build_release.py` ships and the
    LF working tree; `docs/FRESHNESS.md` describes the CI `examples` job;
    `docs/ROUTING.md` no longer says `eval_router.py --router` crashes on dataclass modules;
    `evals/routing/README.md` no longer labels a loose threshold example as the CI gate; and
    `CONTRIBUTING.md` / `CLAUDE.md` list `generate_schema.py --write`.
- **Test isolation:** the MLflow example tests (`test_experiment_tracking.py`,
  `test_ci_cd_for_ml.py`) wrote model artifacts into `./mlruns` of whatever directory the
  suite ran from, because a `sqlite:///` tracking store defaults to a relative artifact
  root. They now run inside their temp dir, and the CI `examples` job fails if tests leave
  any file in the checkout.
- **Remediation round (2026-10-29/30):**
  - **CI `deps` job**: the lock-regeneration step now runs (`if: ${{ !cancelled() }}`) after
    the lock-consistency step fails, so a pin bump without its lock produces the
    `dependency-locks` artifact (#37; test
    `TestDependencyLocks.test_locks_are_regenerated_and_uploaded_after_the_consistency_check_fails`).
  - **`quality_report.py`**: example coverage recorded as `tests.examples.test_x` (tests run
    from the repository root) is now attributed (#40); `KNOWN_LIMITATIONS` updated (#39).
  - **Eval patterns**:
    - hard assertions that failed correct answers were narrowed in 12 places. Examples: a
      threat description in `indirect_prompt_injection`, "temperature=0 never guarantees",
      and "You might think you don't need model.eval() ..., but you do";
    - patterns now also catch wrong answers they used to miss, such as "overfitting isn't
      really a concern", "Setting temperature to 0 guarantees" and "It's fine to use all
      tickets up to today";
    - in a local comparison against HEAD, no tracked fixture changed outcome.
  - **Router**:
    - request-noun negations ("the deliverable is not fairness work") now exclude the named
      skill;
    - routing directives that name skill slugs are ignored;
    - the compound rule no longer fires on verb + object ("strip PII") or on past
      participles;
    - "how do I" is no longer a reference cue;
    - "the validation loss is not improving" routes to `training-deep-models`;
    - specific membership-inference and training-data-extraction phrasings route to
      `data-privacy`. Paraphrases without those terms still do not route (KNOWN-ISSUES #31).
  - **Installer**:
    - `install.sh --force` and `uninstall.sh --force` deleted files that sat behind a
      same-filesystem bind mount inside a skill folder, outside the target. This was observed
      under WSL with the previous scripts (exit 0). It is now refused, and a negative control
      showed the test fails without the check;
    - `install.sh` walked a same-named folder it had not installed before checking it;
    - `list_skills.py` now flags junction-linked folders on Python < 3.12 too, and no longer
      prints `\uXXXX` escapes on a legacy-codepage pipe (KNOWN-ISSUES #12, #30).
  - **Release**:
    - the tarball could not run its own validators, evals, `check_claims.py`,
      `check_generated.py` or `quality_report.py`, and its docs linked to files it did not
      ship. `build_release.py` now ships those files, but the fix is not confirmed: no run of
      `tests/test_release_archive.py` has fully passed. The last fast run passed 11 of 18
      tests, skipped 2 and failed 5 on tree state, and the slow gate has not been re-run
      since it last failed (KNOWN-ISSUES #24);
    - tracked files that matched an exclusion rule were silently dropped from the archive.
  - **Quality report**: with more than one examples results file, a failure could vanish from
    the totals, the hard failures and the exit code. Import failures were also dropped from
    the per-skill status.
  - **Validator**:
    - a heading or bullet inside a fence or a multi-line HTML comment could satisfy a
      required section;
    - an unclosed `~~~` fence passed;
    - plain values that YAML 1.2 reads as numbers were accepted as strings;
    - an unchanged copy of the template failed with a misleading `description-xml-tag` error
      (KNOWN-ISSUES #28).
  - **Plugin tests and export**:
    - the CLI-backed tests could skip without warning, because the `bash -lc` probe timed out
      after 10 s. The probe now tries `bash -c` first;
    - importing `tests/test_generate_plugins.py` no longer starts a subprocess;
    - `export_skills.py` no longer writes a frontmatter value containing `---`, which
      skills-ref cannot parse.
  - **CI**:
    - a regression in an earlier commit of a multi-commit push is now compared against the
      commit before the push. This was shown in a temp repo, not observed on GitHub;
    - the installer job's `bash --version | head -1` ran without pipefail.
  - **Skill content**:
    - `data-preprocessing` passed `stratify=y` unconditionally, which raises for a continuous
      target. A `TASK` switch (default `"classification"`) now limits stratification to
      classification, and a one-row class fails with an error that names it (KNOWN-ISSUES
      #16; 5 new tests in `tests/examples/test_data_preprocessing.py`);
    - `data-privacy` described Carlini et al. 2021 as "targeted prompting". It now describes
      sampling many generations from short or empty prompts, and it gives the Golle 2006
      figures (about 63% on 2000 census data, 61% on 1990 data);
    - `model-deployment` names PyTorch 2.6 as the release where `weights_only=True` became the
      default;
    - the `data-pipelines` "task-local files" gotcha said intermediates are keyed by `ds`; it
      now says `as_of`.

### Security

- **Installer path traversal**: `uninstall.sh` used manifest lines as paths, so a line such
  as `../victim` made it run `rm -rf` on a directory outside the target. `--skill` was not
  validated either. Slugs are now validated and never sanitised; invalid lines are ignored
  with a warning (`tests/test_install_security.py`).
- **Installer link handling and data loss**: symlinks in the source were copied verbatim, and
  a symlinked destination was followed or replaced. Re-running the installer silently
  overwrote local edits, and uninstall deleted them. Links are now refused, and edits are
  kept unless `--force`.
- **model-deployment**: the serving example had no authentication and called `joblib.load`
  on an unverified file, which can execute code from it. It now requires a bearer token,
  pins the artifact hash, and deserialises the verified bytes (closing a re-open gap). It
  also closes `/docs`, `/redoc` and `/openapi.json`, validates input shape and NaN, and
  separates liveness from readiness.
- **rag-pipeline**: retrieval had no tenant or ACL check, so another tenant's best match could
  reach the prompt. The ACL also failed open on a null value. The pipeline now uses one index
  per tenant, fails closed on a missing or null ACL, and keys the query hashes in its logs
  with an HMAC instead of a plain SHA-256.
- **ai-ml-security**: the `torch.load` guidance is version-aware (weights_only default since
  PyTorch 2.6; CVE-2025-32434 bypass in <= 2.5.1).
- **Example dependencies**: mlflow is pinned to 3.17.2. 3.16.1 has three published advisories:
  GHSA-wqmh-qccw-pqwf, GHSA-2wqp-q3c6-ccj5 and GHSA-j7qq-vr8c-xvw3.
- **CI hardening**:
  - read-only token by default, no `pull_request_target`, no secrets;
  - every action pinned to a full commit SHA (node24 runtimes), checkout without persisted
    credentials, and a timeout on every job;
  - a static audit for script injection, `scripts/audit_workflows.py`;
  - `pip-audit` on direct pins and on the installed environments;
  - a routing gate that compares against the parent commit's baseline, so a regression
    cannot lower its own bar.
- **Audit-fix round (2026-10-28):**
  - `scripts/run_evals.py` model mode: urllib's default redirect handler re-sends the
    `x-api-key` header on HTTP redirects, including to other hosts and to cleartext
    `http://`. The Messages API client now refuses every redirect, so a 3xx surfaces as a
    non-retryable HTTP error and the key is never forwarded;
  - installer: a manifest containing a NUL byte is refused as corrupt instead of being
    silently cleaned (bash `read` drops NULs, so `al\0pha` became the valid slug `alpha`);
    `--target` / `--project` values that look like options get a `./` prefix, so
    `--target -` no longer jumps to `$OLDPWD`; `bin/ml-ai-skills` resolves its real location
    through symlinks before locating `install.sh` / `uninstall.sh`, so a linked wrapper no
    longer runs whatever scripts sit next to the link;
  - `scripts/audit_workflows.py` now flags `github.ref_name` / `github.ref` (branch and tag
    names) used in `run:` as a warning.
  - `release.yml` no longer replaces the assets of an existing release with `--clobber`: a
    re-run re-uploads only when the rebuilt `SHA256SUMS` is identical, and a re-pointed tag
    with different bytes fails.
- **Remediation round (2026-10-29/30):**
  - `run_evals.py` passes every report, run-directory file, saved-responses file and stderr
    line through a redactor for secret-looking environment values. Reports record variable
    names, never values.
  - An `ANTHROPIC_API_KEY` with a trailing CR or LF leaked unredacted through the
    `ValueError` that `http.client` raises for a bad header value (found by the 2026-10-30
    security review). `run_evals.py` now refuses a key with surrounding whitespace or control
    characters and names only the variable, and `urllib_transport` turns the error into a
    `TransportError` without the value. Tests:
    `test_api_key_with_whitespace_or_control_characters_is_refused_without_echoing_it` and
    `test_rejected_header_value_never_appears_in_the_error` (`tests/test_run_evals.py`).
  - `ClaudeCliBackend.worst_case_cost` now returns None, because the CLI ignores
    `--max-tokens` and a price-based worst case understated a call, so a
    `--no-cli-budget-flag` run could overshoot the ceiling (same review). Test:
    `test_priced_cli_backend_claims_no_per_call_worst_case`. With `--no-cli-budget-flag`
    the ceiling is checked between calls only, so a single call can still exceed it.
  - Not done: `release.yml` uses no protected deployment environment, and no tag ruleset
    for `v*` is documented. Both are repository settings outside the tree, and they were
    not checked.
  - Installer: per-file manifest paths are never used as filesystem paths. On Linux,
    deleting an old skill copy can no longer reach outside the target through a bind mount.
  - `build_release.py --verify-archive` refuses unsafe members before it writes anything, and
    extracts through a staging directory. On Windows it no longer writes NTFS alternate data
    streams or loses files to device names. Release tags must be signed and GitHub-verified,
    and the tag is checked again between verify and publish.
  - `description-xml-tag`, which `export_skills.py` reuses, took quadratic time on a
    description with many unclosed `<letter` starts. In a local timing, 100k characters now
    take about 0.02 s instead of about 45 s.
  - The integration tests stay local. S3 runs against a moto server bound to 127.0.0.1, with
    real AWS profiles and credential files hidden. Docker publishes only on 127.0.0.1.
    Kubernetes uses only `--dry-run=server`, against a context the user names.
  - pip-audit 2.10.1 found no known vulnerabilities in the direct pins, both locks and the
    installed Linux example environment (WSL, 2026-10-29). Packages are not hash-checked
    (`docs/REPRODUCIBILITY.md`).

## [1.0.0]

Initial state (untagged: no `v1.0.0` tag exists): 38 skills across 8 domains, native
Claude Code plugin/marketplace packaging, portable `install.sh`/`uninstall.sh`,
stdlib-only validation tooling. Reconstructed from git history; no release artifacts were
published for this version.
