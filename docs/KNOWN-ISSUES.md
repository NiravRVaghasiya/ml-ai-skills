# Known issues

Everything in the High, Medium and Low lists is open: an unresolved defect, a claim that has
not been verified, or a gap that has not been tested. The list was rewritten on 2026-10-30
from the round-1 reports of 11 remediation workstreams and their adversarial reviews, the
round-2 integration of 82 cross-cutting requests, and the coordinator's own observations. It
was then re-checked against the working tree on that date. The reviewers, labelers and blind
writers named below are models, not people. Local results are not CI results: no GitHub
Actions run was observed for any of this work (#4). The work is not committed yet (#35). The
final working tree's full suite, `python -m unittest discover -s tests -p "test_*.py"`, ran
1544 tests, OK (skipped=233), on 2026-10-30 (Windows 11, CPython 3.13.2; 178 of the skips
are example dependencies absent from that interpreter, which CI's
`ML_AI_SKILLS_REQUIRE_EXAMPLE_DEPS=1` turns into failures). The CI unit-job module set also ran under CPython
3.12.10: 1008 tests, OK (skipped=34). This list is as
complete as that review made it, not a guarantee that nothing else is open. Items fixed before
2026-10-28 are recorded in `CHANGELOG.md`, not here.

Item numbers are stable identifiers that other docs cite; items added later are appended to
their severity section with the next free number, so numbers are not in order within a
section. A resolved item is removed and its number is not reused: #27 (a stale
`baseline.json` row in `evals/routing/README.md`) was resolved on 2026-10-28, and the items
resolved in this round are listed under "Resolved on 2026-10-30" at the end, with their fix
files, regression tests and evidence. Items whose severity changed in this round say so.

Severity is the harm if a user relies on the affected claim without knowing about the gap:

- **High**: a central guarantee someone might assume is not backed by any test.
- **Medium**: a real defect or gap with a workaround, or limited to one area.
- **Low**: a narrow edge case, a cosmetic issue, or a documented trade-off.

Limits of the test environment itself (no macOS, no GPU, no GitHub runners, DNS failures)
are listed separately under "Environment limits (not defects)".

`python scripts/quality_report.py` reports the checks that a given run skipped
(`skipped_checks`) and gives a short summary of open limitations under `known_limitations`.
That summary was brought up to date on 2026-10-30 (#39); where the two disagree, this file
is the one to trust.

## High

1. **Real model output has been graded once, and the headline uplift rests on a model judge.**
   Re-severity: stays High until the judge is checked against people (#36), because a reader
   could take the uplift below as an established effect.

   The run (2026-10-30, authorized by the user): `python scripts/run_evals.py --provider
   claude-cli --model sonnet --judge-provider claude-cli --judge-model opus --allow-network
   --max-cost-usd 40 --claude-cli-arg=--bare --timeout 300 --run-dir <tmp> --save-responses
   <tmp> --out <tmp>`. 84 cases x 2 arms (with-skill: `SKILL.md` as the system prompt;
   without-skill: the same base instruction without it), one sample each, plus one opus judge
   call per answer: 336 calls, all ok, none incomplete, CLI-reported cost $6.13. The CLI was
   the internal build 2.1.312.1459 with Bedrock authentication; it reported the answering
   model as claude-sonnet-5-5. Provenance: git 51743b0 plus this round's uncommitted tree
   (`git_dirty: true`); every `SKILL.md` and case sha256 in the report still matched the tree
   after the run. The report is stored as `docs/evidence/model-run-2026-10-30.json`
   (local temp paths redacted, the offline `fixture_self_tests` block removed; the original's
   sha256 is recorded in `stored_copy`).

   | metric (n = 84 per arm) | with skill | without skill |
   |---|---|---|
   | correct (combined verdict; needs the judge's rubric pass) | 49 (0.583 [0.477, 0.683]) | 38 (0.452 [0.350, 0.559]) |
   | judge rubric pass (`subjective`) | 67 (0.798) | 51 (0.607) |
   | regex structural pass (`hard`) | 60 (0.714 [0.610, 0.800]) | 56 (0.667 [0.561, 0.758]) |
   | critical error (a `must_not` violation) | 10 (0.119 [0.066, 0.205]) | 11 (0.131 [0.075, 0.219]) |
   | disputed (regex and judge disagree) | 13 | 10 |

   Intervals are Wilson 95%. Paired by case, "correct" improved in 18 cases, got worse in 7
   and tied in 59 (delta +0.131); an exact two-sided sign test on the 25 untied cases gives
   p = 0.043. That is one sample per case, one answering model, one judge, and no correction
   for the several metrics reported, so it is not evidence of a general effect. Critical
   errors did not measurably change (6 better, 7 worse). Cases that got worse with the skill:
   attention-mechanisms/flashattention_complexity_misconception, cnn-vision/conv_arithmetic,
   data-pipelines/train_serve_skew, data-preprocessing/missing_data_edge_case,
   fine-tuning-llms/small_dataset_overfitting, rag-pipeline/prompt_injection_in_docs,
   rag-pipeline/stale_persisted_index; they have not been reviewed.

   What remains open:
   - the judge is not calibrated against people (#36), so "correct" and the rubric pass are
     model judgements; the judge and the answering model come from the same model family;
   - one sample per case: no estimate of run-to-run variance;
   - the with-skill arm puts `SKILL.md` in the system prompt, so it says nothing about how
     Claude Code loads or selects skills (#3); isolation between the arms is unverified (#34);
   - claude-cli cannot set `max_tokens` or temperature; a claude-cli call has no known
     worst-case cost, so it is admitted while spend is below the ceiling, and the CLI's own
     `--max-budget-usd` is the only bound on that call (how the CLI enforces it was not
     tested); the ceiling applies per invocation (`--resume` starts again from $0);
   - the model mode's offline tests (`PYTHONPATH=tests python -m unittest tests.test_run_evals
     tests.test_eval_grading`: 121 tests, OK, skipped=1) use fakes only; the `anthropic`
     backend has never made a real call.
3. **Claude Code's runtime use of the skills is not tested.** More of the packaging path is
   tested now, but no test sends a prompt. `tests/test_generate_plugins.py` covers marketplace
   references, byte-level copies and drift. The opt-in `tests/test_claude_cli_integration.py`
   runs marketplace add/install/list/update on a temp clone, with a leak check of the real
   `settings.json` and `.claude.json` that a positive control shows is not vacuous. It passed
   once on 2026-10-30 with the internal CLI 2.1.312.1459 (172 s, no skips). With the public
   2.1.311 build it passed only before this round's changes; it was not rerun because the
   disk was full. CI's plugins job now runs it (not observed, #4). The model-backed eval (#1)
   does not close this item: it does not use Claude Code's skill discovery or selection.
   Whether Claude Code loads a skill, selects the right one for a task, or does better with
   it is **untested**. See `docs/COMPATIBILITY.md`.
4. **No GitHub Actions result has been observed for this round's work.** The only CI results
   recorded are from the `examples` job on 2026-10-28 (Linux, Python 3.13.17): one run failed
   at `pip install -r requirements-examples.txt` (ResolutionImpossible), and a later one ran
   338 tests with 2 errors caused by pandas 3.1.0. Both failures were fixed (`CHANGELOG.md`),
   but no passing run of any job has been observed. This round changed `ci.yml`,
   `release.yml`, `freshness-scheduled.yml` and `dependabot.yml` substantially. All of it was
   checked only locally: `python scripts/audit_workflows.py --strict` (0 errors, 0 warnings),
   actionlint 1.7.12, PyYAML parsing, `tests/test_audit_workflows.py` (111 tests with
   `tests/test_check_contract.py`, OK) and local re-runs of the scripts each step calls. Not
   observed as a result:
   - any job result, including the new `all-required` aggregator, the `deps` lock job, the
     `release-archive` job, the examples job's `--require-executed` and no-skip steps, the
     plugins job's claude CLI lifecycle step and the validate job's export dry run;
   - the shared "Resolve the base commit" step on real push and pull-request payloads,
     including its fetch-by-sha fallback after a force push (it assumes a public repository
     whose object GitHub still serves). The routing gate now takes its base from this step
     instead of assuming `HEAD^1`;
   - the Linux locks in `constraints/` on ubuntu-latest. They were resolved on WSL Ubuntu
     26.04, whose glibc is newer;
   - the coverage floor (86%) and the subprocess-coverage guard on a runner (#22);
   - the public `@anthropic-ai/claude-code` CLI pinned in CI (`CLAUDE_CODE_VERSION` 2.1.311)
     on a clean runner;
   - the `release.yml` tag checks, `actions/attest-build-provenance` and
     `gh release create --verify-tag` (#21);
   - Dependabot with ungrouped example pins and the two coupled groups (torch with
     torchvision, transformers with tokenizers). Every pip bump stays red until someone
     commits a regenerated lock. The `deps` job now regenerates and uploads it even after the
     consistency step fails (#37, fixed 2026-10-30, not observed). An albumentations bump that needs a
     newer albucore has no automated path.

   Branch protection still has to be pointed at "All required checks passed" (a maintainer
   setting). CI also depends on files that are not committed yet (#35).

## Medium

2. **The regex hard assertions misgrade about 1 in 7 correct answers and 1 in 12 wrong
   answers on a held-out set.** Re-severity: High -> Medium, because a held-out estimate now
   exists. `evals/_grading/heldout/manifest.jsonl` holds 249 answers: for each of 83 cases,
   one correct or borderline-correct answer, one confidently incorrect and one partially
   correct. They were written by 6 blind model writers who saw only the case input and rubric,
   never the patterns, the skills or the repo; their tool-use transcripts show no repository
   file was read. The eval-fixtures workstream finished tuning before the set was written and
   never saw it. `python scripts/run_evals.py --grade-heldout` (re-run 2026-10-30):
   - 12 of 83 correct answers fail some hard assertion (TPR 0.855, Wilson 95% [0.764, 0.915]);
   - 14 of 166 wrong answers pass every hard assertion (TNR 0.916, [0.863, 0.949]).

   Limits of this estimate:
   - every label is a model label (two model labelers per answer, `agent-adjudicated`); no
     answer has a human label (#36);
   - the set is probably easier than real model output (#33);
   - each skill has 1 to 5 cases (most have 2), so 3 to 15 held-out answers, and per-skill
     rates mean little (for example, both correct agent-evaluation answers fail);
   - `data-pipelines/ds_partition_migration`, the 84th case, was added after the set was
     written and has no held-out answers;
   - the set must never be used for tuning; once a pattern is tuned against it, it no longer
     measures anything held out.

   The rubric fields (`expected_behavior`, `must_not`) stay authoritative. The round-1
   reviewer answers were used for tuning and are now fixtures, so they are not held out.
5. **Real upload paths are untested.** Re-severity: High -> Medium, because an export path
   now exists and is tested against the upstream validators. claude.ai skill upload, the
   Skills API and `package_skill.py` accept only six frontmatter keys (Claude Code docs, read
   2026-10-27); every skill here has about 14 more. `scripts/export_skills.py` now writes
   copies for three targets (agentskills-spec, skills-api, claude-ai-upload). It moves every
   extra key into `metadata` without dropping any, and refuses links, path traversal and a
   literal `---` in a frontmatter value. `tests/test_export_skills.py` ran 40 tests, OK
   (skipped=4), on CPython 3.13.2. With quick_validate.py and skills-ref 0.1.1 enabled, it
   passed with only the 2 symlink tests skipped. CI's validate job dry-runs the
   claude-ai-upload target (not observed, #4). Still not done:
   - nothing was uploaded to claude.ai or `POST /v1/skills` (that needs an account or API key
     and costs money). claude.ai's validator is unpublished, so whether it bans `<` and `>`
     is unknown; the export bans them as a conservative choice;
   - whether Claude Code loads an exported skill is untested (#3);
   - the symlink tests have never run, because the Windows test machine lacks the privilege;
     OneDrive and dedup reparse points are tested with simulated stat results only.

   See `docs/SKILL-SPEC.md`, known limitation 7.
6. **Example tests show that code runs, not that the advice is right.** They use tiny
   synthetic data, random-weight models and stubbed or faked services (FAISS fakes, a fake
   Anthropic transport, stub Airflow, TestClient). This round added an opt-in suite,
   `tests/integration/`, gated by `ML_AI_SKILLS_INTEGRATION_<NAME>=1`
   (`tests/integration/gate.py`). Unset, a class skips and names the variable; set, a missing
   module, binary or device is a failure. Executed once each, locally, by the examples
   workstream:
   - TensorFlow 2.21.0: the Keras `same`-padding claim (5 tests, OK);
   - a real Feast 0.67.0 round trip (1 test, OK);
   - the `s3://` publish path against moto (2 tests, OK). The test asserts which client pandas
     used: by default pyarrow's native S3 filesystem, and s3fs only in a forced fallback case.

   Written but never executed, because the infrastructure is missing: Hugging Face Hub
   downloads (DNS failed; this still affects the IMDB sort order, the NLI label spelling,
   `dslim/bert-base-NER` and `bart-large-cnn`), spaCy `en_core_web_sm`, GPU/CUDA AMP and
   `pin_memory`, Docker build and run, a Kubernetes server dry run, and Airflow 3
   `dags test`. Not covered by any test: TorchServe, real Claude or OpenAI calls, pretrained
   torchvision weights, bf16 and 4-bit fine-tuning. CI runs only `integration.test_gate` (the
   gate's own self-tests), none of the opt-in classes. The S3 route check reads the private
   pandas helper `_get_path_or_handle`; if a pandas upgrade removes it, the opted-in test
   fails with a message saying so.
7. **Most version lower bounds are still estimates.** The claims workstream inventoried all
   66 `version_constraints` entries and documented 12 lower bounds from release notes or
   source at the release tag (for example torchvision 0.13, SQLAlchemy 2.0, NumPy 1.17, and
   torch 1.13, 2.3 and 2.6). `docs/FRESHNESS.md` defines executed, documented and estimate.
   An agent did those readings; no person reviewed them, and no documented bound was
   executed. Still estimates: pytest>=7; scikit-learn >=1.0, >=1.2 and >=1.4; pandas >=1.0,
   >=1.5 and >=2.0; mlflow>=2.9; wandb>=0.13; shap>=0.45; optuna>=3.0; onnxruntime>=1.16;
   albumentations>=2.0; apache-airflow>=3.0; prophet>=1.0; scikit-surprise>=1.1;
   anthropic>=2.0. Only the versions in `requirements-examples.txt` were executed, and no full
   run has used the current pins: the last full-pins venv had wrapt 2.5.2rc1 (the pin is now
   2.5.1) and was installed without the Linux lock.
9. **The router has known failure modes** (measured in `docs/ROUTING.md`). This round revised
   the router (a domain-evidence gate, a stricter synonym audit, compound and negation rules,
   indexed bullet lead-ins) and applied 6 skill description edits written from dev failures
   only. The benchmark grew to 359 cases (217 dev, 142 test). 78 of the test cases were
   written by blind model writers who saw only skill names, domains and one-line summaries,
   and each was labelled again by a second blind model reviewer. The original test cases were
   labelled by two blind model reviewers (83 of 85 exact agreement, 2 adjudicated).

   On the held-out test split at top_k 5 (re-run 2026-10-30): task accuracy 0.711 (101/142,
   95% [0.632, 0.779]), recall 0.700, top-1 0.549, false activation over all 60 cases with an
   empty `expected` 0.200 (12/60, [0.118, 0.318]), forbidden-selection rate 0.106. HEAD
   51743b0's router on the same split and skill metadata: task accuracy 0.655, recall 0.720,
   top-1 0.573, false activation 0.317 (19/60). So recall and top-1 are lower than HEAD's.
   The task-accuracy gain is not significant: 12 test cases are right only with the new
   router and 4 only with HEAD's (exact McNemar p=0.077, which assumes independent cases;
   cases in one family are not).

   Fixed (probes run on 2026-10-30): "my model railway layout needs a new train station"
   and "train my puppy to sit" select nothing, and "the validation loss is not improving
   after epoch 3" selects training-deep-models (so does the same sentence without "after
   epoch 3"). `test_known_issue_examples` in `tests/test_router_regressions.py` pins the
   railway and validation-loss requests. `tests/test_router.py` pins a different puppy
   request, "How do I train my puppy to stop biting?", as having no domain anchor; the
   "sit" phrasing and the shortened validation-loss sentence are probes only. Still failing:
   - Requests that share no words with any skill text route nowhere: fraud, cancellations
     and electricity demand (dev par-046, par-048, par-049), "my data has NaN values" (the
     `nan` synonym was removed by the audit), "My loss is not decreasing." and "How many
     epochs should I train for?".
   - Off-topic text still activates skills: "my baby has a sleep regression" selects
     llm-evaluation; "Set up a GitHub Actions workflow to deploy my static website" selects
     model-optimization and ci-cd-for-ml; "Debug this Python IndexError" selects
     python-for-ml.
   - Synonyms: 29 of the 77 original keys survive a rule that requires skill prose to use
     the key in the same sense (`evals/routing/synonym-audit.json`, recomputed by
     `tests/test_router_synonym_audit.py`). That is evidence of usage, not proof of
     equivalence, and the kept keys were first written after reading dev queries.
   - Negation scoping is list-based (a fixed set of request nouns); dev con-025 still misses
     explainability.
   - Dev labels are mostly single-labeller, and the router was tuned on dev; only the test
     split is held out.
   - Term-free privacy-attack and poisoning requests route poorly (#31), and the gate accepts
     two exact regressions against the base commit (#32).
13. **Stronger line-ending rules and the executable bits are not committed.** HEAD 51743b0
    already has the one-line `* text=auto eol=lf` rule. `tests/test_release_gitattributes.py`
    shows that a `core.autocrlf=true` clone of committed HEAD has no CRLF and is accepted by
    `build_release.py` (7 tests, OK, re-run 2026-10-30). Not committed yet:
    - the stronger `.gitattributes` (explicit `text eol=lf` rules per file type and explicit
      `binary` patterns) is a working-tree change only;
    - 23 shebang files (`install.sh`, `uninstall.sh`, `bin/ml-ai-skills` and 20
      `scripts/*.py`) were 100644 in the index. Their 100755 mode is staged with
      `git update-index --chmod=+x`, not committed. Until it is, a fresh Linux or macOS
      clone is expected to check them out without the executable bit (git's normal
      behaviour, not tested here), so `./install.sh` would need `bash install.sh`;
    - the untracked `scripts/export_skills.py` has a shebang and needs `git add --chmod=+x`.

    Not tested: a real GitHub clone on Windows with a global `core.autocrlf=true`, and Linux
    or macOS checkouts.
23. **No private security contact.** `SECURITY.md` still says "Security contact: PENDING". It
    now gives a reporting order (GitHub private vulnerability reporting first, then a
    configured contact, then GitHub's documented fallback) and the maintainer's steps, and
    `docs/RELEASE.md` lists the contact as a pre-release external action. Whether private
    vulnerability reporting is enabled on GitHub was not checked (no credentials). Nothing was
    invented in the contact's place. Closing this item needs the maintainer.
25. **The `data-pipelines` `ds` -> `as_of` migration is tested on SQLite only.** The
    documented `customer_features` partition column was renamed `ds` -> `as_of` (and the
    window re-anchored on `data_interval_end`). Workflow step 7 now provides the migration: a
    dry run by default, blocker checks (a NOT NULL `ds`, or a primary key on it), per-partition
    fills that delete nothing and can be re-run, validation queries, a dual read, a
    deployment order and rollback notes. `TestDsToAsOfMigration` in
    `tests/examples/test_data_pipelines.py` (12 tests) passed with the example pins and
    `ML_AI_SKILLS_REQUIRE_EXAMPLE_DEPS=1`, a mutation run killed all 6 mutations, and the new
    eval case `data-pipelines/ds_partition_migration` covers the misconception. Not covered:
    - Postgres, MySQL and warehouse DDL types and transactions, and nullability or
      primary-key reflection on those dialects;
    - a UNIQUE constraint or index that includes `ds` is not treated as a blocker. That is
      harmless where NULLs are distinct (Postgres by default, MySQL, SQLite), but it would
      block the new DAG's inserts on SQL Server or with Postgres `NULLS NOT DISTINCT`, and the
      skill does not say so;
    - real Airflow (stubbed) and real Feast for this step (the `timestamp_field` change is
      prose only);
    - the `ds + 1 day` mapping is an assumption for Airflow 2 `@daily` (checkable with
      `legacy_window_matches`), and the report's gap check assumes `@daily`.
33. **The held-out grading set is probably easier than real model output.** Both model
    labelers agreed with the writer's intended label on all 249 answers (166 fail, 83 pass,
    0 unresolved). That suggests the wrong answers are clear-cut. The writers were asked to
    produce a given kind of answer, which is not the same distribution as the answers a model
    gives when it is trying to help; real wrong answers may be subtler (partly right,
    hedged). Expect the regex error rates in #2 to be higher on real output. Next step: label
    a sample of real answers from the model run (#1) by hand and grade the regexes against
    them.
34. **Isolation between the two claude-cli arms is intended, not verified.** The model run
    (#1) calls `claude -p` with `--bare`, `--tools ""`, `--disable-slash-commands`,
    `--strict-mcp-config` and `--no-session-persistence`, in a fresh temporary working
    directory; only the with-skill arm gets `SKILL.md` in its system prompt. No test or live
    check has shown that nothing else reaches either arm: user-level settings, hooks,
    plugins, rules files, CLAUDE.md files or memory on the machine that runs it, or the CLI's
    own system prompt. In the CLI build inspected, `--bare` is documented to skip hooks,
    plugin sync and CLAUDE.md auto-discovery; that was not observed. Context that leaks into
    both arms would bias uplift toward zero; context that leaks into one arm could bias it
    either way. Another CLI build may behave differently from the internal 2.1.312.1459 build
    used. See `docs/EVALUATION.md`, "Backends".
35. **This round's work is not committed.** On 2026-10-30, `git status` listed 256 changed
    paths, 92 of them untracked (including `docs/evidence/`). Tests and CI need several of the untracked files:
    `scripts/eval_backends.py`, `scripts/eval_grading.py`, `scripts/export_skills.py`,
    `tests/integration/` and 12 other new test modules, the Linux locks in `constraints/`,
    `.coveragerc`, `docs/EVALUATION.md`, `docs/reviews.json`, `evals/_grading/`,
    `evals/routing/synonym-audit.json`, 68 new eval fixtures and 1 new case YAML. CI installs
    with `-c constraints/...` and needs `.coveragerc`, so it cannot pass until they are
    committed. The 100755 index modes are staged only (#13). Also still to do:
    - `git add --chmod=+x scripts/export_skills.py` (and any other new script with a shebang)
      when committing;
    - after committing, re-run the gates on the clean tree: the 2026-10-30 runs below were all
      on the dirty tree. Generated files had no drift then (`check_generated.py`: 6
      generators, no drift), the slow release-archive gate passed (#24) and the full suite
      ran 1544 tests, OK (skipped=233).
36. **The model judge is not calibrated against people.** The full run (#1) grades the rubric
    fields (`expected_behavior`, `must_not`) with a model judge (claude-cli, opus). No item in
    `evals/_grading/` has a human label, so `run_evals.py` reports `against_humans: not
    measured`, and the held-out labels in #2 are model labels too. A judge-graded "correct"
    rate or uplift is therefore what the judge concludes, not a measured correctness rate.
    Next step: human labels for a sample of real answers, then `--grade-labelled` against
    them.
## Low

12. **Installer gaps.** Re-severity: Medium -> Low, because what remains is narrow. Fixed this
    round, with tests that passed on Windows 11 / Git Bash (116 installer tests, 7 skipped;
    one failure caused by the full disk passed when re-run alone) and on WSL 2 Ubuntu (bash
    5.3.9, Python 3.14.4: 116 tests, OK, 0 skipped):
    - SIGINT and SIGHUP interruption (`TestSignalsDuringCommit`, `TestSignalsFromOutside`
      and a process-group Ctrl-C test in `tests/test_install_signals.py`);
    - the no-sha256-tool refusal, plus an openssl fallback (`TestRequiredTools` in
      `tests/test_install_platform.py`);
    - every file in a managed folder is hashed (manifest format 3,
      `tests/test_install_integrity.py`);
    - on Linux, a skill folder that is or contains a mount point listed in
      `/proc/self/mountinfo` is refused, including a same-filesystem bind mount
      (`TestMountPoints`; a real `mount --bind` under `unshare -rm` on WSL);
    - `scripts/list_skills.py` flags junctions on Python < 3.12 (`TestJunctionsBefore312`,
      run on 3.10.22);
    - a same-named folder the installer did not create is skipped before it is examined.

    What remains:
    - macOS and the BSDs have no mount table, so only device ids are checked there. A
      same-device mount under `TARGET_DIR/<slug>` (for example nullfs, if it reports the
      lower device id) would not be detected, and `--force` could then delete through it,
      as it did on Linux before this round's fix. Untested; documented in
      `docs/INSTALLER.md`;
    - empty directories are not recorded in the manifest;
    - Git for Windows' openssl refuses file names outside the ANSI code page, so such a run
      is refused (the safe direction; not tested with such names);
    - the from-outside signal tests show that bash waits for its child; only the POSIX
      process-group test shows a child terminated by the signal;
    - network filesystems and WSL `/mnt/c` were not tested as targets.

    Nothing was executed with bash 3.2 or BSD tools (see "Environment limits").
14. **Release archive bytes are reproducible only within one deflate implementation.**
    Re-severity: Medium -> Low, because `docs/REPRODUCIBILITY.md` now states this as the
    contract. The uncompressed tar stream and `RELEASE-MANIFEST.sha256` are built to be
    reproducible; the `.tar.gz` bytes match only with the same deflate implementation
    (zlib-ng 2.2.5 gave different bytes). On one Windows machine, CPython 3.13.2, 3.12.10 and
    3.10.22 (zlib 1.3.1, 1.3.1 and 1.3.2) gave identical tar and tgz hashes for the same tree
    (in memory, 2026-10-30). No Linux, macOS or GitHub-runner build was observed, and CPython
    3.11 and 3.14+ were not available, so reproducibility across operating systems is not
    claimed.
15. **Validation edge cases.** Fixed this round, with tests: implicit YAML 1.2 numbers are
    diagnosed (a PyYAML differential corpus and an optional ruamel.yaml cross-check); the
    PEP 440 subset in `check_freshness.py` follows packaging >= 26.1 for `>` and `<` (0
    disagreements on the generated matrix, including the 4 earlier reviewer cases); setext
    headings inside list items are detected (0 differences from markdown-it-py 3.0.0 on 46
    hand-picked cases and all 39 SKILL.md files; on 105,000 seeded random documents, 0 false
    headings but 820 missed ones); near-duplicate capability tags give a warning. What
    remains:
    - the description `<tag>` check is an interpretation; Anthropic's exact rule is not
      published. Conservative false positives remain on purpose (`List<int>` and
      `a<b and c>d` are errors), and `<a < b>` is now only a warning;
    - Claude Code's own YAML parser and version are undocumented. Unquoted YYYY-MM-DD dates
      and the YAML 1.1 spec's `y`/`n` booleans are deliberately not diagnosed;
    - the ruamel.yaml cross-check skips wherever ruamel.yaml is missing, CI included (it is
      not in the dev lock, so it is not pinned);
    - Markdown checks are still not a full CommonMark parser (`docs/SKILL-SPEC.md` known
      limitation 6): a setext heading can be missed, never wrongly reported, under a
      single-line table row, an HTML line, a quote's lazy text or a rule-like lazy line;
    - capability tags that are synonyms in different words (fine-tuning vs adaptation) are
      not detected;
    - PEP 440 agreement is measured only on the generated matrix plus the listed odd strings;
      environment markers and `===` are out of scope.
17. **Reference links that could not be read from the test machine.** Each was re-checked
    another way (Crossref DOI metadata, the DeepMind-hosted Nature PDF, documentation sources
    on GitHub, the Anthropic cookbook); the method and its limits per reference are in
    `docs/SKILL-AUDIT.md`. No reference was replaced. The rendered pages were not read:
    - the LSTM 1997 PDF host (`bioinf.jku.at`) still has an expired TLS certificate;
    - `dl.acm.org` and `doi.org` return HTTP 403 to scripted clients;
    - `huggingface.co`, `spinningup.openai.com` and `anthropic.com` failed DNS locally, and
      OpenAI's API reference and deprecations pages could not be reached;
    - Springer and Nature pages served bot challenges.

    Confirming them needs a person with a browser.
18. **Some claims rest on an agent's reading, not a person's.** An agent read the primary
    sources for these claims and recorded them in `docs/claims.json` as `unverified`, since
    an agent's reading is not human review: the `torch.load(weights_only=True)` history
    (added in 1.13, default in 2.6), the target network first appearing in the 2015 Nature
    DQN paper, the Golle 2006 figures (63% on 2000 census data, 61% on 1990), details of the
    Carlini 2021 and Abadi 2016 papers, the Bagdasaryan 2019 author list (the NeurIPS
    proceedings list three authors and arXiv two; the skill cites "et al."), and OpenAI's and
    Anthropic's `strict: true` naming. The removal releases of `sklearn.cross_validation` and
    `preprocessing.Imputer` were not re-checked this round.
20. **Slow example test files.** Per-module wall times, with the full pins and
    `ML_AI_SKILLS_REQUIRE_EXAMPLE_DEPS=1`, one interpreter per module, on the shared Windows
    machine under load: nlp_tasks 525 s, computer_vision 464 s, fine_tuning_llms 317 s,
    llm_evaluation 227 s, rag_pipeline 212 s; the rest 1.5-170 s; 2857 s summed. The target
    is about 30 s. Load variance is large (the MLflow pair took 47 s, then 169 s). Every
    module's top-level imports are already stdlib-only, and `OMP_NUM_THREADS=1` gave no
    measurable gain. There is no timing after any change, and none on an idle machine or on
    Linux. An opt-in watchdog, `ML_AI_SKILLS_EXAMPLE_TIMEOUT` in `tests/examples/harness.py`,
    now dumps every thread's traceback and exits on timeout, but nothing sets it, CI
    included, and arming it replaces any other faulthandler timer.
21. **No release tag exists.** `git tag` was still empty on 2026-10-30, so `CHANGELOG.md`
    calls 1.0.0 the "initial state (untagged)". No release artifacts were published for
    1.0.0. `release.yml` now refuses lightweight or unsigned tags, and `docs/RELEASE.md`,
    "The first tagged release", gives the maintainer's steps. The maintainer has to sign and
    push the first tag; until then, treat the first tagged release as the first verifiable
    one.
22. **Subprocess coverage is only partly measured.** `.coveragerc` now sets `parallel = True`
    and `patch = subprocess`, so scripts the tests launch as subprocesses are measured, and
    the CI floor rose from 84% to 86%. Measured locally on commit 51743b0: 86.96% -> 89.46% on
    Windows and 86.96% -> 89.43% on WSL. The integration round measured 91% for CI's
    unit-with-dev-deps module set, in a run that had 6 failing tests (999 tests, failures=6,
    skipped=16; all 6 outside the CI workstream's files), so that figure is from a red run.
    A pre-combine guard requires at least 100 lines measured only in child processes; that
    number comes from the before/after reports, not from the guard's own count on a full
    run, and the guard assumes the largest data file is the parent's. Still not measured:
    children started with a fresh environment, and script copies in temp trees.
    `.coveragerc` is untracked (#35), and nothing was observed on a runner (#4).
31. **Term-free privacy-attack and poisoning requests route poorly.** The benchmark now
    asserts the owners: membership inference and training-data extraction select
    `data-privacy`; poisoning and backdoors select `ai-ml-security`. The test split has 3
    such cases, written by blind model writers. The 6 membership-inference and extraction
    cases among the blind-written ones were adjudicated by the coordinator by the skills'
    NOT-for clauses, which the writers had not seen. On 2026-10-30
    (`eval_router.py --split all --show-cases`), phrasings that name the attack route
    correctly, but term-free ones mostly do not:
    - test par-039 (doctored crowd-sourced labels) selects training-deep-models and
      llm-evaluation; test hw0-014 (fake interactions injected into a recommender) selects
      only recommender-systems. Test hw0-011 (training-data extraction) passes;
    - dev par-054 and par-057 select nothing; dev par-056 selects model-deployment,
      cnn-vision, computer-vision, pytorch-patterns and agents-and-tools but not
      ai-ml-security; dev hw0-010 selects nlp-tasks (forbidden) and fine-tuning-llms.
32. **The routing gate accepts two exact regressions against the base commit.** On base
    51743b0's 85-case benchmark, the revised router has recall 0.8571 (floor 0.8701) and
    top-1 0.7727 (floor 0.7879). amb-007, con-004 and mul-002 got worse; par-040, mul-014,
    adv-009, adv-012 and adv-026 got better; net, that is one expected-skill selection and one
    top-1 hit fewer. The base benchmark is not fully held out: 21 of its 85 test cases are now
    dev cases that the router was tuned on (`docs/ROUTING.md`), including two of the improved
    ones, par-040 and adv-026. The three cases that got worse are still test cases. So the net
    loss of one selection and one top-1 hit may understate the regression on unseen requests
    (`docs/ROUTING.md` and the `reason` text say so). `accepted_regressions`
    in `evals/routing/baseline.json` records each one: the base benchmark's sha256, the
    metric, the floor, the exact accepted value, the cases and the reason. The gate turns
    only an exact match into a `::warning::`; a further drop, another base benchmark or a
    malformed entry still fails (4 tests in `tests/test_router_gate.py`).
    `python scripts/eval_router.py --gate --base-sha HEAD` exited 0 with 2 warnings
    (coordinator run, 2026-10-30). The trade-off is deliberate, but it weakens the
    base-commit check for this one change. The entries apply only while the base benchmark
    is 51743b0's. Not observed in CI (#4).
38. **Release gates run only inside the tagged commit's own `release.yml`.** From the
    security review on 2026-10-30 (a model review, confirmed by a second model pass, rated
    low). Anyone with write access can push a `v*` tag on a commit whose `release.yml` has
    weaker checks, and get an attested release of unreviewed code. `release.yml` has no
    `environment:` on its publish job, and nothing in the repo configures a tag ruleset.
    Closing this needs GitHub settings that only the maintainer can make: a tag ruleset on
    `refs/tags/v*` (only maintainers create, move or delete tags; no force-move or
    delete), and a protected `release` deployment environment with required reviewers, used
    by the publish job. The signed-tag checks in #21 do not cover this; `docs/RELEASE.md`
    does not list it yet.

24. **The release archive's self-containment gate has passed only locally, on an uncommitted tree.**
    Re-severity: Medium -> Low.
    `build_release.py` takes its file list from git: every tracked file ships, plus untracked
    files that are not ignored, and an unclassified file stops the build. So `tests/`,
    `evals/`, `.github/` and `CLAUDE.md` now ship. On 2026-10-30 (coordinator, Windows 11,
    CPython 3.13.2, final working tree):
    - `ML_AI_SKILLS_RELEASE_SLOW=1 PYTHONPATH=. python -m unittest -v test_release_archive
      test_release_gitattributes test_build_release` (from `tests/`): 72 tests, OK
      (skipped=1: creating a symlink needs a Windows privilege), 970 s. This builds the real
      archive, extracts it and runs the documented checks from the extracted tree;
    - two `build_release.py --out-dir` builds were byte-identical (tarball sha256
      `d5e7e86c...`, 2,215,663 bytes); `--verify-archive --extract-to` matched all 1285
      per-file SHA-256s; from that extraction `validate_skills.py --warnings`,
      `validate_evals.py --strict --coverage --require-full-coverage` and
      `--strict-advisories`, `eval_router.py --split test`, `run_evals.py --grade-heldout`,
      `check_claims.py`, `check_generated.py`, `generate_plugins.py --check`,
      `generate_readme_stats.py --check`, `check_freshness.py` and `quality_report.py`
      all exited 0.

    Two leftover `router-report*.json` files (gate reports written to the checkout root) first
    stopped the build as unclassified; they are now in `.gitignore`. What remains: the tree
    was dirty (the work is not committed, #35), and the CI `release-archive` job and
    `release.yml` have not run (#4). Details: `docs/RELEASE.md`, "What ships".

## Environment limits (not defects)

These are limits of where and how things were run, not known defects in the code. Each one
means the affected behaviour is **unverified** there.

- **GitHub Actions:** no run of any workflow was observed for this round's work (#4).
- **macOS, bash 3.2 and BSD tools** (cp, mv, shasum, stat): never executed. The installer
  has only static portability checks (`TestPortability`), and the CI macOS job has not been
  observed.
- **Linux:** only WSL 2 Ubuntu 26.04 on the maintainer's machine, not ubuntu-latest: the
  installer tests (bash 5.3.9, Python 3.14.4) and the example pins and lock (CPython
  3.13.18). On ubuntu-latest, the real-mount installer tests will probably skip, because
  AppArmor restricts unprivileged user namespaces.
- **Python versions:** most runs used CPython 3.13.2 and 3.12.10 on Windows. 3.10.22 ran for
  the `list_skills.py` and `build_release.py` tests only; the eval runner and router were
  checked on 3.10 by grammar only, and the export was not run on 3.10 (the registered 3.10
  interpreter was missing). 3.11 was not available anywhere.
- **GPU and CUDA:** none on any test machine (#6).
- **Network:** `huggingface.co`, `spinningup.openai.com`, `anthropic.com` and OpenAI's
  documentation failed DNS or could not be reached; `dl.acm.org` and `doi.org` return 403
  to scripts; Springer and Nature serve bot challenges (#17). No `verify_online.py` run was
  made against the real network.
- **Windows privileges and console:** no symlink privilege, so the symlink tests skip (#5
  and the grading symlink-escape test). Interactive-console output of `list_skills.py` was
  not tested.
- **Claude Code CLI:** the internal build 2.1.312.1459 (Bedrock authentication) was used this
  round. The public 2.1.311 build that CI pins was not rerun after this round's changes, and
  no Linux CLI build was observed (#3).
- **Heavy dependencies:** TensorFlow, Feast and moto/s3fs were installed only in scratch
  venvs; no CI job installs TensorFlow. Docker, Kubernetes, Airflow and spaCy models were
  not available (#6).
- **Disk:** drive C: reached 0 MB free during the round. Several runs failed with ENOSPC
  and passed when re-run, and some re-measurements were stopped (for example the coverage
  guard's own count, #22). Agents' scratch directories could not be deleted.
- **Load:** timings come from a shared, loaded machine (#20).

## Resolved on 2026-10-30

These items were removed from the lists above. Their numbers are not reused. "Evidence"
names the runs that back each one; runs without a date were made by the workstream named.
On 2026-10-30 the test modules holding the regression tests for #11, #19, #26 (quality
report part), #28, #29 and #30 ran together on Windows, CPython 3.13.2, from `tests/` with
`PYTHONPATH=.` and bare module names: `test_check_freshness`, `test_check_claims`,
`test_verify_online`, `test_list_skills`, `test_validate_skills`, `test_validator_mutations`,
`test_skills_lib`, `test_evals`, `test_quality_report`, `test_doc_consistency` and
`test_router_gate`: 459 tests, OK (skipped=8). Run from the repository root as `tests.*`
modules, one `test_quality_report` test failed (#40, since fixed). `examples.test_data_preprocessing` and
`examples.test_harness` (#16, #10): 27 tests, OK.

- **#8 The example venv and CI pins differed for mlflow.**
  - Fix: `requirements-examples.txt` pins mlflow 3.17.2, installed in CI with
    `constraints/examples-linux-cp313.txt`; the `mlflow-search-runs-includes-failed-runs`
    claim in `docs/claims.json` now applies to 3.17.2.
  - Regression tests: `test_best_run_ignores_failed_runs` and
    `test_serialization_defaults_match_the_gotcha` in
    `tests/examples/test_experiment_tracking.py`; `TestDependencyLocks` in
    `tests/test_audit_workflows.py`.
  - Evidence: examples workstream, mlflow 3.17.2 venv on Windows (CPython 3.13.2,
    `ML_AI_SKILLS_SLOW_EXAMPLES=1 ML_AI_SKILLS_REQUIRE_EXAMPLE_DEPS=1`):
    `test_experiment_tracking` plus `test_ci_cd_for_ml` ran 22 tests, OK, 0 skipped, twice
    (the second run by the reviewer). ci-deps workstream, WSL Ubuntu 26.04 with the lock
    (round 1): `test_experiment_tracking` 9/9 OK, `tests/examples` 351 OK, 0 skips.
    `python scripts/check_claims.py` on 2026-10-30: 0 errors.
  - Residual: the maintainer's global Windows Python still has mlflow 3.16.1 (three
    published advisories), where `test_serialization_defaults_match_the_gotcha` fails as
    documented. The lock is unverified on ubuntu-latest (#4).
- **#10 Executable eval evidence for experiment-tracking needed an opt-in and was silently
  skipped.**
  - Fix: `scripts/run_evals.py` reports a skip as "skipped (not evidence)", adds
    `--slow-examples`, `--require-example-deps` and `--require-executed` (which exits 1 on any
    skip), and keeps the last 4000 characters of a failing test's output.
    `tests/examples/harness.py` has `slow()` and `slow_examples_enabled()`, now used by the
    MLflow example modules. The pendulum check in `tests/examples/test_data_pipelines.py`
    fails instead of skipping under `ML_AI_SKILLS_REQUIRE_EXAMPLE_DEPS=1`. The ci.yml
    examples job runs `run_evals.py --run-executable --require-executed`.
  - Regression tests: `ExecutableOptInTests` and `ExecutableRunnerTests` in
    `tests/test_run_evals.py`; `TestNothingSkipsAtImportUnderTheCiEnvironment` and
    `TestRuntimeSkipsAndTheCiJobUnderTheRequireEnv` in `tests/examples/test_harness.py`;
    `test_slow_examples_run_under_either_variable` in `tests/test_quality_report.py`.
  - Evidence: on 2026-10-30, `python scripts/run_evals.py --run-executable --slow-examples
    --require-executed --case experiment-tracking/crashed_run_registered` exited 0 with both
    listed tests passed and 0 skipped (Windows, CPython 3.13.2, mlflow 3.16.1, about 11
    minutes under load). The eval-runner workstream showed that without the opt-in both
    tests are reported as skipped (not evidence) and `--require-executed` exits 1. The
    examples workstream's mutation run showed that removing the REQUIRE variable from
    ci.yml, or adding an unclassified runtime skip, fails `test_harness`.
  - Residual: a default local `--run-executable` still skips the slow tests, by design, and
    says they are not evidence. The CI steps were not observed (#4). That run used mlflow
    3.16.1, not the pinned 3.17.2.
- **#11 Two STALE freshness patterns rested on unverified claims.**
  - Fix: `scripts/check_freshness.py` keeps a STALE verdict only when its backing claim is a
    `runtime-test` or `human-review` claim with status current or review-due
    (`STALE_BACKING_VERIFICATION`, `STALE_BACKING_STATUS`); otherwise the match is reported
    as REVIEW_DUE with a NOTE. `scripts/check_claims.py` uses the same constants. The
    tensorflow-contrib claim is now a runtime test.
  - Regression tests: `test_stale_level_requires_a_verified_backing_claim`,
    `test_cli_fails_only_for_verified_stale_patterns` and `TestTensorflowContribGone` in
    `tests/test_check_freshness.py`; `test_stale_pattern_backed_by_unverified_claim_is_review_due`
    in `tests/test_check_claims.py`.
  - Evidence: on 2026-10-30, `python scripts/check_freshness.py` exited 0 with 0 STALE and a
    NOTE that `openai-text-davinci` is reported as REVIEW_DUE, and `python
    scripts/check_claims.py` reported 36 claims, 0 errors, 1 review-due finding. Claims
    workstream: `TestTensorflowContribGone` OK in a tensorflow-cpu 2.21.0 venv.
  - Residual: the text-davinci retirement is still unverified (a person has to read OpenAI's
    deprecations page), so that pattern reports REVIEW_DUE instead of failing CI. The
    tensorflow-contrib verdict rests on one local run; no CI job installs TensorFlow.
- **#16 `data-preprocessing` assumed classification.**
  - Fix: `data-preprocessing/SKILL.md` step 2 has a `TASK` switch (default
    `"classification"`). Classification stratifies after a guard that raises a ValueError
    naming any single-row class; `TASK = "regression"` splits without stratifying. A new
    Gotcha covers the stratification requirements.
  - Regression tests in `tests/examples/test_data_preprocessing.py`:
    `test_classification_split_preserves_class_proportions`,
    `test_singleton_class_raises_an_error_naming_it`,
    `test_continuous_target_splits_and_trains_as_regression`,
    `test_test_split_smaller_than_class_count_raises` and
    `test_quantile_bin_stratification_from_gotcha_runs`.
  - Evidence: skill-fixes workstream, pinned venv with `ML_AI_SKILLS_REQUIRE_EXAMPLE_DEPS=1`:
    43 tests OK across the data-preprocessing and data-pipelines modules. The reviewer's
    mutation run killed "unconditional `stratify=y`" and "rare-class guard disabled".
  - Residual: the user has to set `TASK`; it is not detected automatically, on purpose (an
    integer-coded regression target looks multiclass to sklearn). Not executed on pandas 3.
- **#19 `verify_online.py` budget nuances.**
  - Fix: `scripts/verify_online.py` never starts a request with less than 1 s of budget
    left, caps each timeout at the remaining budget, runs each request under a watchdog,
    classifies breaker failures (throttle, server error, timeout, network) with
    `--breaker-on`, and gives each acknowledgement its own `expires_on` and an injectable
    clock.
  - Regression tests in `tests/test_verify_online.py`: `TestBudgetBound`,
    `TestCircuitBreakerClasses`, `test_explicit_expiry_date_and_injectable_clock` and
    `test_reviewer_worst_case_stays_inside_budget`.
  - Evidence: claims workstream, 101 tests with the freshness and claims modules, OK on
    CPython 3.13, 3.12 and a venv without packaging. The reviewer judged #19 resolved.
  - Residual: an abandoned request keeps its daemon thread and socket until the socket gives
    up; the 2 s watchdog grace is a constant; no run was made against the real network.
- **#26 `quality_report.py` limitations.**
  - Fix: per-skill human review records in `docs/reviews.json` (a skill is "reviewed" only
    with a full, approved record for its current SKILL.md sha256); example tests attributed
    by `SKILL`/`SKILLS` declarations, a static scan and the harness's recorded module, with
    `SKILLS = ()` as an explicit exclusion (now set in `tests/examples/test_harness.py`);
    coverage records (schema v2) with skill sha256, git revision, tracked-file dirty state
    and an untracked-file count; results documents of one suite merged, and outcomes split
    into current, stale and untied. Files: `scripts/quality_report.py`,
    `scripts/check_example_coverage.py`, `tests/examples/harness.py`.
  - Regression tests in `tests/test_quality_report.py`: `TestHumanReview`,
    `TestAttribution`, `TestEvidenceProvenance`, `TestGitRevisionOnlyForTheTreeItself` and
    `TestPerSkillStatus`; `test_v2_records_with_provenance_pass_and_bad_provenance_fails` in
    `tests/test_check_example_coverage.py`.
  - Evidence: quality workstream, a clean clone of 51743b0 with its files: 85 tests OK on
    CPython 3.13.2 and 3.12. Integration (code group): 192 tests OK, including
    `tests/test_quality_report.py` in full.
  - Residual: the review record's reviewer field is not authenticated, and no human review
    is recorded, so `skills_human_reviewed` is 0. A module that covers several skills
    attributes all its outcomes to each of them, and outcomes are tied per module, not per
    block. Records without a git revision are accepted unless `--require-coverage-revision`
    is given. CI does not run `quality_report.py`.
- **#28 Template placeholders tripped the validator.**
  - Fix: `_TEMPLATE/SKILL.md` uses TODO placeholders, and `scripts/validate_skills.py` has a
    `placeholder-text` error whose message names the template, so an unfilled copy no longer
    gets the misleading `description-xml-tag` error.
  - Regression tests: `TestPlaceholderText` (3 tests) in `tests/test_validate_skills.py`;
    `test_unfilled_template_copy` in `tests/test_validator_mutations.py`.
  - Evidence: validation workstream, 176-test run of its modules (1 expected failure at the
    time, waiting on a `docs/RELEASE.md` edit that has since landed).
  - Residual: none reported.
- **#29 30 `must_not_include` patterns had never matched any fixture.**
  - Fix: 21 patterns got a fail fixture that catches them, 8 were rewritten after a correct
    answer showed them unsound, and 1 was removed. 15 case YAMLs and 62 fixture files changed
    or were added. `scripts/validate_evals.py --strict` now fails on advisories, and
    `tests/test_evals.py` requires zero advisories instead of freezing a list.
  - Regression tests in `tests/test_evals.py`:
    `test_every_must_not_include_pattern_is_exercised`,
    `test_every_fixture_grades_as_labelled`,
    `test_dev_fixtures_use_known_labels_and_fail_on_must_not_include`,
    `test_cli_strict_modes_pass` and
    `test_must_not_include_that_no_fail_fixture_triggers_is_an_advisory`.
  - Evidence: before the change, `validate_evals.py --strict-advisories` exited 1 with 30
    advisories (coordinator baseline at 51743b0). On 2026-10-30,
    `python scripts/validate_evals.py --strict --coverage --require-full-coverage` exited 0:
    880/880 fixture self-tests OK, 0 warnings, 0 advisories.
  - Residual: a pattern that fires on one fixture says nothing about its recall on real
    output (#2). The guards are phrase lists whose trade-offs are recorded in each case's
    notes. Assertion indices and case hashes changed, so `run_evals.py --compare` against an
    older base report needs a re-baseline.
- **#30 `list_skills.py` printed `\uXXXX` escapes on a legacy-codepage pipe.**
  - Fix: `scripts/list_skills.py` writes UTF-8 to pipes and files unless `PYTHONIOENCODING`
    names an encoding, which is then honoured (unencodable characters become escapes); a
    console keeps Python's encoding.
  - Regression tests in `tests/test_list_skills.py`: `test_pipe_output_is_utf8_by_default`
    and `test_explicit_pythonioencoding_is_honoured_with_escapes`.
  - Evidence: installer workstream, `tests/test_list_skills.py` 32 tests OK on CPython
    3.10.22, 3.12.10 and 3.13.2.
  - Residual: interactive-console output was not tested.

Partly fixed items stay in their lists with what is now fixed stated in place: #12 (four of
its five gaps), #15 (four of its five bullets), #5, #9, #13, #22, #24, #25 and #31.

- **#37 The `deps` job did not upload a regenerated lock when a pin was bumped.**
  - Root cause: the regeneration step had no `if:`, so GitHub's default `success()` skipped it
    after the lock-consistency step failed, which is exactly the bumped-pin case.
  - Fix: `if: ${{ !cancelled() }}` on "When requirements changed, they resolve without the
    locks; upload regenerated locks" in `.github/workflows/ci.yml`.
  - Regression test: `tests/test_audit_workflows.py`
    `TestDependencyLocks.test_locks_are_regenerated_and_uploaded_after_the_consistency_check_fails`.
  - Evidence: coordinator, 2026-10-30: `TestDependencyLocks` 5 tests OK;
    `python scripts/audit_workflows.py --strict` 0 errors, 0 warnings.
  - Residual: not observed on GitHub (#4).
- **#39 `quality_report.py`'s `known_limitations` list was out of date.**
  - Fix: `KNOWN_LIMITATIONS` in `scripts/quality_report.py` (same ids) now describes the
    model run (#1), the held-out regex estimate (#2), the 142-case test split with model
    label review (#9) and the WSL runs.
  - Regression test: none beyond `tests/test_quality_report.py` (74 tests, OK); the text is
    prose and this file remains the authority.
- **#40 Example coverage was not attributed when tests ran from the repository root.**
  - Root cause: `quality_report._coverage_module` accepted `test_x` and `examples.test_x`
    but not `tests.examples.test_x`, so records from `python -m unittest tests.examples...`
    were unattributed, and one `test_quality_report` test failed under that invocation.
  - Fix: strip a leading `tests.` in `scripts/quality_report.py`.
  - Regression test: `tests/test_quality_report.py`
    `TestHarnessCoverageProvenance.test_module_names_from_every_invocation_style_are_attributed`.
  - Evidence: coordinator, 2026-10-30: `tests.test_quality_report` (from the repo root) and
    `test_quality_report` (from `tests/`): 74 tests, OK both ways.
