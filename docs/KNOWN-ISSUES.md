# Known issues

Everything below is open: an unresolved defect, a claim that has not been verified, or a
platform or integration that has not been tested. The list was compiled on 2026-10-28 from
the Phase 1 and Phase 2 engineering reports (their `unverified`, `known_limitations` and
documented-not-fixed items) and from review of those reports, re-checked against the
working tree on that date. It is as complete as that review made it, not a guarantee that
nothing else is open. Items that were fixed before that date are recorded in
`CHANGELOG.md`, not here.

Item numbers are stable identifiers that other docs cite; items added later are appended to
their severity section with the next free number, so numbers are not in order within a
section. A resolved item is removed and its number is not reused: #27 (a stale
`baseline.json` row in `evals/routing/README.md`) was resolved on 2026-10-28.

Severity is the harm if a user relies on the affected claim without knowing about the gap:

- **High**: a central guarantee someone might assume is not backed by any test.
- **Medium**: a real defect or gap with a workaround, or limited to one area.
- **Low**: a narrow edge case, a cosmetic issue, or a documented trade-off.

`python scripts/quality_report.py` reports the checks that a given run skipped
(`skipped_checks`) and repeats the high-severity items under `known_limitations`.

## High

1. **No eval case has been graded against real model output.** `scripts/run_evals.py` has a
   model mode, but it has never been run. Pass rates and with-skill vs without-skill uplift
   are **unmeasured**; every report says `baseline: unmeasured`. What *is* tested is that each
   case's pass/fail fixtures grade as intended under its regex hard assertions.
2. **The hard assertions are regexes, and no held-out estimate of their accuracy remains.**
   When independent reviewers wrote answers without seeing the patterns, the patterns missed
   many of them. Before fixes:
   - evals-a: 15/24 correct answers passed and 0/13 targeted on-topic wrong answers were caught;
   - evals-b: 5/16 wrong answers failed;
   - evals-c: 16/23 wrong answers passed every hard assertion;
   - evals-d: 13/16 wrong answers passed;
   - evals-e: 7/15 wrong answers were caught.

   All of those answers were then used to tune the patterns and are now fixtures, so they no
   longer measure anything held out. Expect real model outputs to find more misses. The
   rubric fields (`expected_behavior`, `must_not`) stay authoritative.
3. **Claude Code's runtime use of the skills is not tested.** The plugin tests stop at
   `claude plugin validate --strict` and the marketplace add/install/list/update return
   codes; the installer tests stop at file layout and frontmatter parsing. Whether Claude
   Code loads a skill, selects the right one for a task, or does better with it is
   **untested**. See `docs/COMPATIBILITY.md`.
4. **No GitHub Actions workflow has run.** `ci.yml`, `release.yml` and
   `freshness-scheduled.yml` were checked with actionlint, `scripts/audit_workflows.py`,
   PyYAML and local re-runs of individual scripts only. Untested as a result:
   - every Linux and macOS result (including bash 3.2 and BSD tools for the installer);
   - `pip install -r requirements-examples.txt` on Linux;
   - the public `@anthropic-ai/claude-code` CLI build pinned in CI (`CLAUDE_CODE_VERSION`) on a clean runner;
   - the release ancestry/CI-success check, `actions/attest-build-provenance`,
     `gh release create --verify-tag`;
   - the coverage floor (84%) on Linux; it was measured at 87% on Windows only;
   - whether Dependabot handles the `+cpu` torch pins and `--extra-index-url`;
   - the routing gate's assumption that `HEAD^1` of a pull-request merge commit is the base branch tip.
5. **Upload paths are expected to reject the skills.** claude.ai skill upload, the Skills API
   and `package_skill.py` accept only six frontmatter keys (Claude Code docs, read
   2026-10-27). Every skill here has about 14 more. Nothing strips them, and nothing tests
   these paths. See `docs/SKILL-SPEC.md`, known limitation 7.

## Medium

6. **Example tests show that code runs, not that the advice is right.** They use tiny
   synthetic data, random-weight models and stubbed or faked services (FAISS fakes, a fake
   Anthropic transport, stub Airflow, TestClient). Not executed anywhere:
   - GPU and CUDA paths (AMP with scaling, `pin_memory` on a GPU);
   - pretrained weights and the Hugging Face Hub, which did not resolve from the test machine;
     this affects the IMDB sort order, the NLI label spelling, `dslim/bert-base-NER` and
     `bart-large-cnn`;
   - real Airflow and Feast;
   - Docker, Kubernetes, TorchServe and `kubectl` commands;
   - the `s3://` publish path;
   - TensorFlow (the Keras `same`-padding claim comes from documentation only).
7. **Version lower bounds are mostly estimates.** Most `version_constraints` lower bounds
   (for example `scikit-learn>=0.20`, `pandas>=1.0`, `torch>=1.13`, `mlflow>=2.9`) come from
   model knowledge. Only the versions in `requirements-examples.txt`, or the local venv where
   they differ, were executed.
8. **The example venv and CI pins differ for mlflow.** The local example venv has mlflow
   3.16.1, which has three published advisories. `requirements-examples.txt` pins 3.17.2. The
   runtime-test claim `mlflow-search-runs-includes-failed-runs` in `docs/claims.json` was
   observed on 3.16.1. An earlier report of the experiment-tracking tests passing on 3.17.2
   was wrong: CI failed the serialization-defaults test there. After the fix, the
   `test_experiment_tracking` and `test_ci_cd_for_ml` modules passed against 3.17.2 in a
   separate venv (2026-10-28). The serialization gotcha now describes 3.17.2, so on the local
   3.16.1 venv `test_serialization_defaults_match_the_gotcha` fails as expected.
9. **The router has known failure modes** (measured in `docs/ROUTING.md`):
   - The domain-word gate rejects clear ML requests that use none of its listed words, and
     admits off-topic text with two everyday ML words ("model railway ... train station").
   - Off-topic requests built around a homonym still activate skills. The headline
     false-activation rate (0.0 on test) counts only the `no-skill` category; 6 of the 10
     adversarial test cases that need no skill select one (6 of 19 over every test case with
     an empty `expected`).
   - The synonym map is partly fitted to the dev split: it adds nothing on test, and 30 keys
     each occur in exactly one dev query.
   - Negation scoping is rule-based. A negated request whose subject is an ordinary noun is
     read as a state.
   - Vocabulary absent from skill metadata (`loss`, `epoch`) routes nowhere: "validation
     loss not improving after epoch N" selects no skill.
   - Benchmark labels come from a single labeller, and the test split has 85 cases, so
     differences of a few points may be noise.
10. **Executable eval evidence for experiment-tracking needs an opt-in.**
    `tests/examples/test_experiment_tracking.py` skips unless `ML_AI_SKILLS_SLOW_EXAMPLES=1`
    (or `ML_AI_SKILLS_REQUIRE_EXAMPLE_DEPS=1`, which CI sets). A default
    `run_evals.py --run-executable` therefore reports `crashed_run_registered` as skipped, which
    is not evidence.
11. **Two STALE freshness patterns rest on unverified claims.** `tensorflow-contrib` and
    `openai-text-davinci` fail CI when they match, but their backing claims in
    `docs/claims.json` are `unverified`, so `check_claims.py` reports them REVIEW_DUE.
    TensorFlow is not installed anywhere these tests run, and openai.com did not resolve from
    the test machine.
12. **Installer gaps** (see `docs/INSTALLER.md`):
    - SIGINT and SIGHUP interruption is not automated; only SIGTERM is;
    - the no-sha256-tool refusal is untested;
    - only `SKILL.md` is hashed, so edits to other files in a managed folder are not detected;
    - WSL, and a `TARGET_DIR/<slug>` that is its own mount point (where `mv` is not atomic),
      are not tested;
    - on Windows with Python < 3.12, `scripts/list_skills.py` does not flag a junction-linked
      skill folder `[UNSAFE]`.
13. **`.gitattributes` is not committed yet.** It was still untracked on 2026-10-28, so the
    LF-only rule it sets is not in any commit. Until it is, a Windows clone with
    `core.autocrlf=true` would be expected to check text files out with CRLF, which
    `build_release.py` refuses (not tested on a fresh clone). The maintainer's working tree is
    no longer affected: on 2026-10-28 no path from `git ls-files -co --exclude-standard`
    contained CRLF, two local `build_release.py --out-dir` builds were byte-identical, and
    `test_build_release` ran 17 tests with no skips.
14. **Release reproducibility across machines is unproven.** Byte-identical tarballs were
    shown only for repeated builds on one machine. The per-file `RELEASE-MANIFEST.sha256`
    does not depend on zlib; the tarball hash may.

23. **No private security contact.** `SECURITY.md` has a `TODO(maintainer)` placeholder instead
    of a contact address, so there is no private channel for reporting a vulnerability until the
    maintainer fills it in. Nothing was invented in its place.
24. **The release tarball is not self-contained.** `tests/`, `evals/`, `.github/` and
    `CLAUDE.md` are not in `build_release.py --list` (`SECURITY.md` and `CONTRIBUTING.md` are).
    In a tarball built and extracted on 2026-10-28, `eval_router.py`, `validate_evals.py`,
    `run_evals.py`, `check_claims.py`, `generate_readme_stats.py --check` (so
    `check_generated.py`) and `quality_report.py` failed or reported hard failures, and the
    shipped `README.md`, `CONTRIBUTING.md` and docs link to the missing files. Details:
    `docs/RELEASE.md`, "What ships".
25. **`data-pipelines` schema change needs a migration.** The documented `customer_features`
    partition column was renamed `ds` -> `as_of` (and the window re-anchored on
    `data_interval_end`). A table or Feast source built from the earlier version of the skill
    has the old column; no migration is provided.

## Low

15. **Validation edge cases.**
    - The stdlib YAML parser is checked for parity with PyYAML (YAML 1.1) only.
    - The description `<tag>` check is an interpretation; Anthropic's exact rule is not published.
    - Markdown checks are line- and regex-based (`docs/SKILL-SPEC.md` known limitation 6).
    - `capability-collision` catches identical tags only.
    - The PEP 440 subset in `check_freshness.py` differed from the `packaging` library on 4
      reviewer cases, all a `>V-pre` specifier against a `V+local` candidate.
16. **`data-preprocessing` assumes classification.** `stratify=y` raises for continuous
    targets and for classes with a single member. It was left unchanged to keep the
    gold-standard diff minimal.
17. **Reference links that could not be confirmed from the test machine.**
    - The LSTM 1997 PDF host (`bioinf.jku.at`) has an expired TLS certificate.
    - `dl.acm.org` and `doi.org` return HTTP 403 to scripted clients.
    - `huggingface.co`, `spinningup.openai.com`, `anthropic.com` and `platform.openai.com`
      failed DNS locally, probably a local DNS issue.
    - Springer and Nature pages served bot challenges and were confirmed through Crossref
      metadata only.
18. **Claims from model knowledge, not re-checked.** These come from the skill audits:
    - the release that first made `torch.load(weights_only=True)` the default;
    - the removal releases of `sklearn.cross_validation` and `preprocessing.Imputer`;
    - the target network first appearing in the 2015 Nature DQN paper;
    - Golle 2006 figures (no number is stated);
    - some details of the Carlini 2021, Bagdasaryan 2019 and Abadi 2016 papers;
    - OpenAI's `strict: true` naming.
19. **`verify_online.py` budget nuances.** A request already in flight can overrun the budget
    by up to `--timeout`. The circuit breaker counts only throttled responses.
    Acknowledgements lapse on a fixed clock.
20. **Slow example test files.** Several files take 50-165 s on the shared Windows machine,
    against a ~30 s target. Most of that time is import cost, measured under load and not on
    an idle machine.
21. **No release tag exists in the local clone.** `git tag` was empty in the maintainer's
    clone on 2026-10-28, so `CHANGELOG.md` now calls 1.0.0 the "initial state (untagged)". No
    release artifacts were published for 1.0.0. Tag it, or treat the first tagged release as
    the first verifiable one.
22. **Coverage of in-process code only.** The CI coverage floor measures code imported by the
    tests; scripts launched as subprocesses are not measured, so CLI-only paths are
    under-counted.
26. **`quality_report.py` limitations.**
    - `human_review` is claim-level only (`claim-level-review-only` when a current
      `human-review` claim exists); there is no per-skill review record, so
      `skills_human_reviewed` is 0 by construction.
    - Example test outcomes are attributed to a skill by module name
      (`tests/examples/test_<slug>.py`); a differently named module is not attributed.
    - Coverage jsonl from `tests/examples/harness.py` records no git revision or content hash.
      Ingested coverage is tied to the current tree only by each skill's python block count
      (`coverage-stale` on mismatch), so an edit that keeps the block count is not detected
      unless the coverage record carries a `skill_sha256`. Results written by
      `--collect-tests` do record the revision and per-skill SHA-256.
    - Block coverage alone does not show that the example tests' assertions passed; the
      `example-tests-failing` status needs example test results as well.
28. **Template placeholders trip the validator.** `_TEMPLATE/SKILL.md`'s description uses
    `<do X>` / `<phrase 1>` placeholders, which match the `description-xml-tag` rule if a new
    skill keeps them. `CLAUDE.md`'s example no longer does.
29. **30 `must_not_include` patterns have never matched any fixture.** The
    `eval_lib.unexercised_must_not_include` lint is advisory only. The 30 current patterns are
    frozen in `tests/test_evals.py` `KNOWN_UNEXERCISED_MUST_NOT_INCLUDE`, so a new
    unexercised pattern fails the tests, but no fixture proves these 30 can fire. Next step:
    add a fail fixture per pattern (or fix it), then run `validate_evals.py --strict-advisories`
    in CI.
30. **`list_skills.py` on a legacy-codepage pipe prints `\uXXXX` escapes.** On Windows
    (cp1252 stdout under Git Bash), characters outside the code page in paths or display names
    are now escaped instead of crashing. Set `PYTHONUTF8=1` for readable output.
31. **No routing benchmark cases for privacy attacks.** Membership-inference and
    training-data-extraction queries should select `data-privacy` (which owns them); poisoning
    should select `ai-ml-security`. No benchmark case asserts this yet.
