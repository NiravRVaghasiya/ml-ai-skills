# Compatibility

This page lists where the skills are expected to work and, for each claim, the test that
backs it. Where no test backs a claim, the claim is marked **untested**. A test in this
repo can show that files are laid out and parsed correctly, and that the `claude` CLI
accepts and installs the packaging. **No automated test shows that Claude Code, or any
other agent, actually loads a skill at runtime, picks the right one, or does a better job
because of it.**

Support levels used below:

| Level | Meaning |
|---|---|
| **Supported, tested** | Covered by automated tests that were run, with the run recorded below |
| **Supported, partly tested** | Installation and file format are tested; runtime behaviour is not |
| **Documented, untested** | Expected to work, or known not to work, from vendor documentation only |
| **Not supported** | Not targeted and not tested; no claim is made |

## Summary

| Environment | Level | Backing |
|---|---|---|
| Claude Code plugin marketplace (`/plugin marketplace add`, `/plugin install`) | Supported, partly tested | `tests/test_generate_plugins.py` |
| Claude Code plain-file skills (`~/.claude/skills/`, `<project>/.claude/skills/`) via `install.sh` | Supported, partly tested | `tests/test_install.py`, `tests/test_install_security.py`, `tests/test_list_skills.py` |
| claude.ai skill upload, Claude Skills API, `package_skill.py` | Documented, untested (expected to **reject** these files) | `docs/SKILL-SPEC.md`, known limitation 7 |
| Cursor, GitHub Copilot, OpenAI Codex, Kiro, other agents and editors | Not supported | none |

## Claude Code plugin marketplace

**What is packaged.** `scripts/generate_plugins.py` generates `.claude-plugin/marketplace.json`
and `plugins/<name>/`. There is one all-in-one plugin (`ml-ai-skills`) plus one plugin per
domain. Each packaged `SKILL.md` is a byte copy of the canonical `<slug>/SKILL.md`.

| Contract line | Backing test | Status |
|---|---|---|
| **Discovery (marketplace).** `marketplace.json` has the required keys, every entry resolves to a plugin directory with `.claude-plugin/plugin.json`, and the entries are the all-in-one plugin plus every domain | `tests/test_generate_plugins.py::TestMarketplaceJson` | tested (stdlib, no CLI) |
| **Schema acceptance.** `claude plugin validate . --strict` and `claude plugin validate plugins/<name> --strict` exit 0 | `tests/test_generate_plugins.py::TestClaudeCliValidation` | tested when `claude` is on PATH, skipped otherwise |
| **Installation.** In an isolated `CLAUDE_CONFIG_DIR`: `plugin marketplace add <repo>`, `plugin install ml-ai-skills@ml-ai-skills`, `plugin install classical-ml@ml-ai-skills`, `plugin list` (both names listed), `plugin update`, reinstall, all exit 0 | `tests/test_generate_plugins.py::TestLocalMarketplaceInstall::test_marketplace_add_install_update_reinstall_cycle` | tested when `claude` is on PATH; asserts return codes and the `list` output only |
| **Packaging integrity.** Each plugin ships only real skills, the all-in-one plugin ships every skill, and every packaged `SKILL.md` is byte-identical to its source | `tests/test_generate_plugins.py::TestPluginManifests`, `::TestNoDuplicateContent`, `::TestRenderDeterminism` | tested (stdlib). These fail on purpose while a skill edit has not been regenerated |
| **Metadata interpretation.** Claude Code reads `name` and `description` and ignores the other frontmatter keys | Claude Code docs (read 2026-10-27, see `docs/SKILL-SPEC.md`); `name`/`description` limits are enforced by `scripts/validate_skills.py` | documented, not tested against Claude Code |
| **Skill loading.** Claude Code loads the installed `SKILL.md` and offers it to the model | none | **untested** |
| **Skill selection.** Claude Code picks the right skill for a task | none (the repo's own router benchmark, `evals/routing/`, measures `scripts/router.py`, not Claude Code) | **untested** |
| **Representative task execution.** A model with the skill loaded completes a task better than without it | none (`scripts/run_evals.py` has a model mode, but it has never been run; uplift is unmeasured) | **untested** |

**Runs recorded.**

- 2026-10-28, Windows 11, Git Bash, Python 3.13.2, `claude 2.1.310.1372 (ASBX Claude Code,
  channel stable)`: `TestClaudeCliValidation` (2 tests) and `TestLocalMarketplaceInstall`
  (1 test) passed. Two drift tests in the same module failed because other skill edits had
  not yet been regenerated into `plugins/`.
- CI (`.github/workflows/ci.yml`, `plugins` job) installs `@anthropic-ai/claude-code` at the
  version pinned in `CLAUDE_CODE_VERSION` (2.1.311 at the time of writing) and fails if the
  CLI tests skip. **That job has not been run by us**, so no public-CLI result is recorded here.

**Known differences between CLI builds.** Builds have disagreed on `plugin validate --json`
output and on whether `plugin install -y` is accepted. The tests therefore assert return
codes (and `plugin list` text) only. A future CLI release can still break the cycle.

## Claude Code plain-file skills (`install.sh`)

`install.sh` copies `<slug>/SKILL.md` folders into `~/.claude/skills/` (default),
`<project>/.claude/skills/` (`--project`) or any directory (`--target`). The full contract
(manifest, local-edit protection, `--force`, `--prune`, exit codes, threat model) is in
[`docs/INSTALLER.md`](INSTALLER.md).

| Contract line | Backing test | Status |
|---|---|---|
| **Installation.** Every source skill is installed and listed in the manifest; re-running updates in place; `--dry-run` changes nothing; uninstall removes only managed skills | `tests/test_install.py` | tested on Windows 11 / Git Bash (Phase 1, 2026-10-27); not run on Linux/macOS by us |
| **Safety.** Path traversal, symlinks/junctions, local edits, foreign folders, locking, interrupted commits | `tests/test_install_security.py` | as above; SIGINT/SIGHUP and the no-sha256-tool path are not automated (see `docs/INSTALLER.md`) |
| **Discovery (file layout).** Each installed `SKILL.md` parses and its `name` equals its folder | `tests/test_install.py::TestCleanInstall::test_skill_md_discoverable_after_install` | tested. This is a *proxy*: it checks the files Claude Code reads, not that Claude Code reads them |
| **Inventory.** `scripts/list_skills.py --target DIR` reports what is installed and flags modified/unsafe entries | `tests/test_list_skills.py` | tested (also run on 2026-10-28 in the unit suite). On Windows with Python < 3.12 a junction-linked skill folder is not flagged `[UNSAFE]` (see `docs/KNOWN-ISSUES.md`) |
| **Metadata interpretation, skill loading, selection, task execution** | none | **untested**, same as for the plugin route |

## Claude.ai upload, Skills API, `package_skill.py`

**Documented, untested.** The Claude Code skills documentation (read 2026-10-27) says these
upload paths accept only the frontmatter keys `name`, `description`, `license`,
`compatibility`, `metadata` and `allowed-tools`, and fail with `Unexpected key(s) in SKILL.md
frontmatter` on anything else. Every skill here carries about 14 repo-specific keys, so
**expect these paths to reject the files as shipped**. Stripping the extra keys, or moving them
under `metadata`, should get past that check, but nothing in this repo does that or tests it.
See `docs/SKILL-SPEC.md`, "Known limitations" item 7.

## Other agents and editors

**Not supported.** Cursor, GitHub Copilot, OpenAI Codex, Kiro and other agents are not
targeted and not tested. The files are plain Markdown with YAML frontmatter, so they can be
read as reference documents anywhere, but no claim is made about discovery, metadata
handling or behaviour in those tools.

## Platforms

| Platform | What was actually run |
|---|---|
| Windows 11 + Git for Windows (bash 5.2.37), Python 3.13.2 | Default unit suite (2026-10-28: 502 outcomes, 498 passed, 2 failed on plugin drift, 2 skipped), example suite in a venv (2026-10-28: 327 tests OK), claude-CLI plugin tests (2026-10-28), installer suites (Phase 1, 2026-10-27) |
| Python 3.10 (3.10.22, Windows) | Phase 2 CI workstream, 2026-10-27, stdlib unit suite minus `test_install*.py`: **Ran 455 tests, FAILED (failures=2, skipped=13)**. One failure is a real 3.10 defect: a junction-linked skill folder is not reported `[UNSAFE]` by `list_skills.py` ([KNOWN-ISSUES](KNOWN-ISSUES.md) #12). The other, `test_evals.RealRepoTests.test_no_validation_errors`, failed while another workstream was editing evals and was reported to pass on rerun. Not re-run in this round; 3.10 is the declared minimum, not a fully passing platform |
| Linux (`ubuntu-latest`) | **Only in CI configuration.** Not executed by us |
| macOS (`macos-latest`, system bash 3.2, BSD tools) | **Only in CI configuration.** Not executed by us; bash-3.2 safety is by code review only |
| WSL, network filesystems, `TARGET_DIR/<slug>` as its own mount point | Not run; see `docs/INSTALLER.md` "Platforms" |

## Keeping this page honest

Update a row only with a run you observed: the command, the date, the platform, and the
result. If a CLI or platform changes and the tests were not re-run, leave the older date in
place rather than implying a fresh result. `python scripts/quality_report.py` lists the
checks it did not run (for example `claude-cli`, `github-actions`, `other-platforms`) under
`skipped_checks`.
