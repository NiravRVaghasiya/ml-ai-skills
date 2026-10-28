# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project uses
[Semantic Versioning](https://semver.org/spec/v2.0.0.html) as defined for this
repository in `docs/RELEASE.md`.

## [Unreleased]

Summary of the Phase 1 and Phase 2 hardening work and of the audit-fix round that followed
an independent audit (2026-10-28). The numbers below come from the engineering reports and
from runs on Windows 11 / Git Bash / Python 3.13 on 2026-10-27 and 2026-10-28. No GitHub Actions workflow has run yet; see `docs/KNOWN-ISSUES.md`.

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

### Changed

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

### Fixed

- **experiment-tracking**: the serialization gotcha described MLflow 3.16.1, where
  `mlflow.sklearn.log_model`/`save_model` default to `serialization_format="skops"`. The
  pinned 3.17.2 defaults to `None`, resolved at call time to `"skops"` (`"cloudpickle"` on
  Databricks), so the CI `examples` job failed `test_serialization_defaults_match_the_gotcha`.
  The gotcha now describes 3.17.2. The test also logs a model without the argument and checks
  the format that was written. `test_experiment_tracking` and `test_ci_cd_for_ml` pass
  (22 tests) against mlflow 3.17.2 (Windows, Python 3.13.2, 2026-10-28).
- **Dependabot**: the grouped `example-deps` PR bumped `pyarrow` to 26.0.0, which pip could
  not resolve against the pinned mlflow 3.17.2 (`pyarrow<26`), so the CI install step failed.
  `.github/dependabot.yml` now ignores `pyarrow>=26`; the pin stays at 24.0.0.
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

## [1.0.0]

Initial state (untagged: no `v1.0.0` tag exists): 38 skills across 8 domains, native
Claude Code plugin/marketplace packaging, portable `install.sh`/`uninstall.sh`,
stdlib-only validation tooling. Reconstructed from git history; no release artifacts were
published for this version.
