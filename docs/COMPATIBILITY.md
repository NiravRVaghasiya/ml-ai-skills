# Compatibility

This page lists where the skills are expected to work and, for each claim, the test that
backs it. Where no test backs a claim, the claim is marked **untested**. A test in this
repo can show that files are laid out and parsed correctly, that the `claude` CLI
accepts, installs and inventories the packaging, and that an exported skill passes the
published upload validators. **No automated test shows that Claude Code, or any other
agent, actually loads a skill at runtime, picks the right one, or does a better job
because of it.**

Support levels used below:

| Level | Meaning |
|---|---|
| **Supported, tested** | Covered by automated tests that were run, with the run recorded below |
| **Supported, partly tested** | Installation and file format are tested; runtime behaviour is not |
| **Format tested, upload untested** | The files pass the target's published validators, run locally; nothing was uploaded |
| **Documented, untested** | Expected to work, or known not to work, from vendor documentation only |
| **Not supported** | Not targeted and not tested; no claim is made |

## Summary

| Environment | Level | Backing |
|---|---|---|
| Claude Code plugin marketplace (`/plugin marketplace add`, `/plugin install`) | Supported, partly tested | `tests/test_generate_plugins.py`, opt-in `tests/test_claude_cli_integration.py` |
| Claude Code plain-file skills (`~/.claude/skills/`, `<project>/.claude/skills/`) via `install.sh` | Supported, partly tested | `tests/test_install.py`, `tests/test_install_security.py`, `tests/test_list_skills.py` |
| claude.ai skill upload, Claude Skills API, `package_skill.py`, `skills-ref`: canonical files | Known to be rejected (run locally for `package_skill.py`/`quick_validate.py` and `skills-ref`) | `tests/test_export_skills.py` (opt-in upstream classes) |
| The same paths, files from `scripts/export_skills.py` | Format tested, upload untested | `tests/test_export_skills.py` |
| Cursor, GitHub Copilot, OpenAI Codex, Kiro, other agents and editors | Not supported | none |

## Claude Code plugin marketplace

**What is packaged.** `scripts/generate_plugins.py` generates `.claude-plugin/marketplace.json`
and `plugins/<name>/`. There is one all-in-one plugin (`ml-ai-skills`) plus one plugin per
domain. Each packaged `SKILL.md` is a byte copy of the canonical `<slug>/SKILL.md` (read as
bytes, so line endings are kept). Only `SKILL.md` is packaged, as `install.sh` does.

| Contract line | Backing test | Status |
|---|---|---|
| **Discovery (marketplace).** `marketplace.json` has the required keys, every entry resolves to a plugin directory with `.claude-plugin/plugin.json`, and the entries are the all-in-one plugin plus every domain | `tests/test_generate_plugins.py::TestMarketplaceJson` | tested (stdlib, no CLI) |
| **References agree.** Entry names unique and ordered; every `source` is `./plugins/<name>` with no `..`; no plugin folder missing from the marketplace; marketplace, entry and `plugin.json` versions equal `VERSION`; entry and `plugin.json` descriptions match; each plugin ships exactly its planned skills (domain plugins only their domain); each plugin folder holds only `plugin.json` and `skills/<slug>/SKILL.md` | `::TestMarketplaceReferences` | tested (stdlib) |
| **Skill counts.** Every "N skills" claim in generated text equals the real count | `::TestNoHardcodedCounts` | tested (stdlib) |
| **Packaging integrity.** Every packaged `SKILL.md` equals its source, compared as text and as bytes; a CRLF source is copied as CRLF; a canonical skill folder holding a file the packaging would not ship fails (OS/build litter such as `.DS_Store`, `__pycache__/`, `*.pyc` is ignored) | `::TestNoDuplicateContent`, `::TestPluginManifests`, `::TestRenderDeterminism` | tested (stdlib). The drift tests fail on purpose while a skill edit has not been regenerated |
| **Drift detection.** `check()` reports a stale, a missing and an unexpected file in a scratch tree, and `write()` removes plugin folders it no longer generates | `::TestCheckDetectsDrift` | tested (stdlib) |
| **Schema acceptance.** `claude plugin validate . --strict` and `claude plugin validate plugins/<name> --strict` exit 0 | `::TestClaudeCliValidation` | tested when `claude` is found, skipped otherwise |
| **Installation.** In an isolated `CLAUDE_CONFIG_DIR`, `HOME`, `USERPROFILE`, `APPDATA` and XDG dirs: `plugin marketplace add <repo>`, `plugin install ml-ai-skills@ml-ai-skills`, `plugin install classical-ml@ml-ai-skills`, `plugin list` (both listed), `plugin update`, reinstall, all exit 0 | `::TestLocalMarketplaceInstall` | tested when `claude` is found; asserts return codes and `list` text only |
| **Lifecycle on a clean clone (opt-in).** `git clone` of HEAD into a temp dir; the clone's `generate_plugins.py --check`; `validate --strict` (marketplace and every plugin); `marketplace add`/`list`; install two plugins; `list` and `list --json`; every installed `SKILL.md` copy the CLI reports (`installPath`, `readFromFolder`) byte-identical to the clone's canonical file; `plugin details` reports `Skills (N)` and names every planned skill; `update`; `marketplace update`; `uninstall` (and the plugin leaves `list`); uninstalling again and installing an unknown plugin both fail; `marketplace remove` leaves no plugin installed; the developer's real config (`plugins/*.json`, `settings.json`, `settings.local.json`, `.claude.json`) never mentions the temp clone, after a positive control shows the isolated config's copies of those files do record it | `tests/test_claude_cli_integration.py` (`ML_AI_SKILLS_CLAUDE_CLI=1`; `ML_AI_SKILLS_CLAUDE_BIN` picks a binary) | tested on Windows only, runs below. No command sends a prompt; whether a CLI build makes background network requests was not checked |
| **Metadata interpretation.** Claude Code reads `name` and `description` and ignores the other frontmatter keys | Claude Code docs (re-read 2026-10-29); `name`/`description` limits are enforced by `scripts/validate_skills.py` | documented, not tested against Claude Code |
| **Skill loading.** Claude Code loads the installed `SKILL.md` into a session and offers it to the model | none. `plugin details` shows the CLI's inventory and token estimate, which is not the same as loading the skill into a session | **untested** |
| **Skill selection.** Claude Code picks the right skill for a task | none (the repo's own router benchmark, `evals/routing/`, measures `scripts/router.py`, not Claude Code) | **untested** |
| **Representative task execution.** A model with the skill loaded completes a task better than without it | none yet. `scripts/run_evals.py` model mode was first run against a real model on 2026-10-30 (claude-cli; `docs/EVALUATION.md`, "Model runs"); it measures the model with and without the skill in its system prompt, not Claude Code loading it | **untested** |

**Runs recorded.**

- 2026-10-28, Windows 11, Git Bash, Python 3.13.2, `claude 2.1.310.1372 (ASBX Claude Code,
  channel stable)`: `TestClaudeCliValidation` (2 tests) and `TestLocalMarketplaceInstall`
  (1 test) passed. Two drift tests in the same module failed because other skill edits had
  not yet been regenerated into `plugins/`.
- 2026-10-29, Windows 11, Git Bash, Python 3.13.2:
  - `tests/test_generate_plugins.py`, run in a clone of HEAD 51743b0 with this round's
    script and test changes copied in (so skills and `plugins/` agreed): **Ran 32, OK, no
    skips**, CLI `claude 2.1.312.1459 (ASBX Claude Code, channel stable)`. In the shared
    working tree the same module failed only its three drift tests, on skills other
    workstreams had edited and not regenerated.
  - Its three CLI tests also passed with the public **2.1.311** Windows build (below).
  - `tests/test_claude_cli_integration.py` with `ML_AI_SKILLS_CLAUDE_CLI=1`: **OK, 0 skipped
    subtests** with `claude 2.1.312.1459 (ASBX Claude Code, channel stable)` (321 s), and
    **OK** with the public `2.1.311 (Claude Code)` (164 s).
  - The public build was `@anthropic-ai/claude-code-win32-x64@2.1.311`, downloaded from
    registry.npmjs.org with its sha512 `integrity` checked, unpacked into a scratch
    directory and selected with `ML_AI_SKILLS_CLAUDE_BIN`. Only `--version` and `plugin`
    subcommands were run, with an isolated config.
- CI (`.github/workflows/ci.yml`, `plugins` job) installs `@anthropic-ai/claude-code` at the
  version pinned in `CLAUDE_CODE_VERSION` (2.1.311). That version exists on npm: published
  2026-10-26T19:47:57Z, `engines.node >=22.0.0`, and the dist-tags read on 2026-10-29 were
  `latest`/`next` 2.1.312 and `stable` 2.1.308. **The job itself has not been observed**,
  and the Linux build of 2.1.311 has not been run by us. The same job also runs the opt-in
  lifecycle test (`ML_AI_SKILLS_CLAUDE_CLI=1`, must not skip there); that step has not been
  observed either.

**CLI behaviour observed on 2026-10-29** (both builds above; a future release can change it):

- A local-folder marketplace is read in place. `plugin list` shows `Read from: <repo>/plugins/<name>`,
  and `plugin update` prints `nothing to update` and exits 0. **Updating from a GitHub
  or git source is untested**: it needs network access, and `marketplace add` refused a
  `file://` git URL (`Invalid marketplace source format`).
- `plugin install` still writes a cache copy under `<config>/plugins/cache/...`. Its
  `SKILL.md` files were byte-identical to the canonical files.
- `plugin details <plugin>` lists the skills (`Skills (6)  data-preprocessing, ...`) with an
  estimated token cost. The integration test skips that subtest on a build without it.
- `plugin validate --strict` passes with one informational "plugin directory" warning
  (`ICON_MISSING`), which the output says does not change the result.
- The ASBX wrapper prints `claude: info:` lines and writes its setup state into
  `CLAUDE_CONFIG_DIR`, including Bedrock provider settings (`awsCredentialExport`,
  `AWS_REGION`, a default model) in `settings.json`. This is why every test redirects that
  directory. Removing `ANTHROPIC_*`, `CLAUDE_CODE_USE_*` and `AWS_BEARER_TOKEN_BEDROCK`
  from the environment therefore does not leave the isolated CLI without a provider; the
  only guarantee is that no command the tests run sends a prompt. The tests also set
  `CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1`.
- A local-folder `marketplace add` is recorded in `settings.json` under
  `extraKnownMarketplaces` ("declared in user settings"), not only in `plugins/*.json`.

**Known differences between CLI builds.** Builds have disagreed on `plugin validate --json`
output and on whether `plugin install -y` is accepted. The default tests therefore assert
return codes (and `plugin list` text) only. The opt-in test also reads `plugin list --json`
and skips that subtest if it gets no JSON. A future CLI release can still break the cycle.
The CLI is found with `bash -c "command -v claude"`, falling back to a login shell. A login
shell took 13 s on the loaded Windows machine, longer than the old 10 s probe timeout,
which made every CLI test skip quietly.

## Claude Code plain-file skills (`install.sh`)

`install.sh` copies `<slug>/SKILL.md` folders into `~/.claude/skills/` (default),
`<project>/.claude/skills/` (`--project`) or any directory (`--target`). The full contract
(manifest, local-edit protection, `--force`, `--prune`, exit codes, threat model) is in
[`docs/INSTALLER.md`](INSTALLER.md).

| Contract line | Backing test | Status |
|---|---|---|
| **Installation.** Every source skill is installed and listed in the manifest; re-running updates in place; `--dry-run` changes nothing; uninstall removes only managed skills | `tests/test_install.py` | tested on Windows 11 / Git Bash (Phase 1, 2026-10-27, and 2026-10-30) and on Ubuntu under WSL 2 (2026-10-30); not run on macOS by us |
| **Safety.** Path traversal, symlinks/junctions, local edits, foreign folders, locking, interrupted commits | `tests/test_install_security.py`, `tests/test_install_integrity.py`, `tests/test_install_signals.py`, `tests/test_install_platform.py` | tested on Windows 11 / Git Bash and on Ubuntu under WSL 2 (2026-10-30, `tests/test_install*.py`: 116 tests; WSL OK with 0 skipped; Windows 7 skipped and 1 failure from a full disk that passed when re-run on its own; details in `docs/INSTALLER.md` "Platforms"); covers per-file edits, mount points (device ids plus `/proc/self/mountinfo`, real tmpfs and bind mounts under WSL), SIGINT/SIGHUP/SIGTERM and missing sha256/stat tools; macOS/bash 3.2 not run |
| **Discovery (file layout).** Each installed `SKILL.md` parses and its `name` equals its folder | `tests/test_install.py::TestCleanInstall::test_skill_md_discoverable_after_install` | tested. This is a *proxy*: it checks the files Claude Code reads, not that Claude Code reads them |
| **Inventory.** `scripts/list_skills.py --target DIR` reports what is installed and flags modified/unsafe entries | `tests/test_list_skills.py` | tested (also run on 2026-10-28 in the unit suite). Junctions are flagged on 3.10+ (tested with `mklink /J` on 3.10.22, 3.12.10 and 3.13; see `docs/INSTALLER.md`) |
| **Metadata interpretation, skill loading, selection, task execution** | none | **untested**, same as for the plugin route |

## Claude.ai upload, Skills API, `package_skill.py`, Agent Skills spec

### The rules, and where they come from

Read on 2026-10-29 by plain HTTP fetch (all returned HTTP 200):

| Source | What it says |
|---|---|
| https://code.claude.com/docs/en/skills (`.md` form) | Uploads to claude.ai, the Skills API and `package_skill.py` allow only `name`, `description`, `license`, `compatibility`, `metadata`, `allowed-tools`. Any other key fails with `Unexpected key(s) in SKILL.md frontmatter: …`. The same rules apply when a personal skill is enabled for claude.ai. `metadata` is a free-form map that Claude Code does not act on (a value that is not a map is dropped), and its keys should not reuse frontmatter field names such as `paths`. `compatibility` is at most 500 characters |
| https://platform.claude.com/docs/en/agents-and-tools/agent-skills/overview (`.md`) and `.../best-practices` | `name` and `description` required. `name` at most 64 characters, only lowercase letters, numbers and hyphens, no XML tags, no `anthropic`/`claude`. `description` non-empty, at most 1,024 characters, no XML tags |
| https://platform.claude.com/docs/en/build-with-claude/skills-guide (`docs.claude.com/en/api/skills-guide` redirects here) | The same `name`/`description` rules. `SKILL.md` at the upload root or at the top of a single enclosing folder; with the `skills-2025-10-02` beta header, files must sit in a top-level folder named after the skill. Total upload under 30 MB uncompressed. Zip or individual files |
| https://agentskills.io/specification | The six fields. `name` 1-64 characters, lowercase alphanumerics and hyphens, no leading, trailing or consecutive hyphen, equal to the folder name. `description` 1-1,024 characters. `compatibility` 1-500. `metadata` "a map from string keys to string values". `allowed-tools` a space-separated string (experimental) |
| anthropics/skills `skills/skill-creator/scripts/quick_validate.py` and `package_skill.py` (`main`, last changed in commit 1ed29a03, 2026-02-06; local sha256 `67cf5703…` and `1a33059b…`) | The six keys, checked with PyYAML. Kebab-case name of at most 64 characters. **Any `<` or `>` in the description is rejected**, a stricter rule than "no XML tags". `compatibility` at most 500. The frontmatter must match `^---\n(.*?)\n---`, so it needs LF line endings. The package is a zip named `<skill>.skill` holding `<skill>/…`, without `__pycache__/`, `node_modules/`, `*.pyc`, `.DS_Store` or a top-level `evals/` |
| agentskills/agentskills `skills-ref/src/skills_ref/validator.py` and `parser.py` (`main`, commit 547831f3), and PyPI `skills-ref` 0.1.1 (same `ALLOWED_FIELDS` and limits) | The six keys, parsed with **strictyaml**. NFKC-normalised name must equal the folder name. `metadata` values are coerced to strings |

claude.ai's own server-side validator is not published. The Claude Code docs say only that
claude.ai uploads accept the same six frontmatter fields as the Skills API and
`package_skill.py`, and that "the Agent Skills spec and the Skills API requirements define
everything else those paths validate". The ban on `<`/`>` comes from `quick_validate.py`
only (the spec and the platform docs say "no XML tags"); whether claude.ai enforces it is
unknown. The `claude-ai-upload` target applies it as a conservative choice.

skills-ref finds the frontmatter with `content.split("---", 2)`, so a literal `---` inside
any frontmatter value ends the frontmatter early and the file fails to parse.
`quick_validate.py` (`^---\n(.*?)\n---`) does not have this problem.

### The export (`scripts/export_skills.py`)

```
python scripts/export_skills.py --list-targets
python scripts/export_skills.py --target claude-ai-upload --out OUT [--zip] [--license MIT] [slug ...]
```

- **Targets.** `agentskills-spec` (spec and `skills-ref` rules), `skills-api` (adds reserved
  words, XML tags, 30 MB, folder layout) and `claude-ai-upload` (adds `quick_validate.py`'s
  ban on `<`/`>`, as a conservative choice, and LF-only frontmatter). Any other name exits 2
  and lists the valid ones.
- **Transformation.** `name` and `description` are copied with the same parsed value.
  Every other key moves into `metadata`: strings as-is, list keys as a JSON array string
  (`related: "[\"a\", \"b\"]"`). Values are written as double-quoted YAML scalars, with
  line-break-like and non-printable characters escaped. Every third consecutive `-` is
  written as the escape `\u002d`, so no literal `---` appears before the closing fence (skills-ref),
  and a check refuses any frontmatter that has one. Each move is printed and recorded
  in `OUT/export-report.json` with the source and output SHA-256 values. **No key is
  dropped.** `--license` adds the spec `license` field. Nothing is invented by default.
- **Body and files.** Every byte after the closing `---` line is copied unchanged, and so is
  every other regular file in the skill folder. The build artifacts `package_skill.py`
  leaves out are excluded and listed. A symlink, junction or non-regular file is refused,
  never followed. A BOM, invalid UTF-8 or frontmatter outside the repo's YAML subset is
  refused.
- **Safety.** `--out` must be new or empty, must not be the skills root and must not be
  inside a skill folder. Slugs must be lowercase-kebab names, so paths such as `../x` are
  rejected. Every skill is validated before anything is written: one failure means nothing
  is written (exit 1). Canonical `SKILL.md` hashes are compared before and after.
- **Zip.** `--zip` writes `OUT/<slug>.zip` holding `<slug>/…`, the layout both Skills API
  modes and `package_skill.py` use. Entries are sorted and carry a fixed timestamp, mode and
  creator, so repeated runs give identical bytes. A different zlib version may not.

**Round trip.** `export_skills.canonical_values()` turns an exported frontmatter back into
the canonical values (tested for every skill and target). Limits:

- An empty list key (`requires:` with no items, parsed as null) comes back as `[]`.
- The canonical text (block scalars, flow lists, quoting, comments) is not reproduced.
- A folded `description` keeps its trailing newline in the exported value.
- Keys added with `--license` have no canonical counterpart.

There is no reverse command. Edit the canonical folder and export again.

### What was verified, and what was not

| Claim | Evidence | Status |
|---|---|---|
| Canonical folders are rejected by `quick_validate.py` and `skills-ref` | 2026-10-29: both rejected the canonical folders, listing exactly the repo-specific keys | **verified locally** (opt-in `TestUpstreamQuickValidate`, `TestUpstreamSkillsRef`) |
| Every export has only allowed keys, a byte-identical body, string-to-string metadata, values that round-trip, deterministic zips, an unchanged canonical tree, and limits enforced (1,024/1,025-character description, 65-character name, name with a trailing newline, reserved word, XML tag, bare `>`, folder mismatch, malformed YAML, BOM, 30 MB, `---` inside a value), and reparse points classified (symlink, junction and name-surrogate tags refused; OneDrive/dedup tags simulated, not created) | `tests/test_export_skills.py`: 2026-10-29, Python 3.13.2, **Ran 40, OK, skipped=4**. The 4 skips: two symlink tests (Windows without the symlink privilege) and the two opt-in upstream classes; with the upstream variables set, skipped=2. Python 3.12.10: Ran 40, OK, skipped=6 (no PyYAML) | tested. The symlink tests have not run anywhere yet; the Windows junction test passed |
| All 38 exports pass the upstream tools | 2026-10-29, scratch venv (PyYAML 6.0.3, strictyaml 1.7.3, skills-ref 0.1.1): `quick_validate.py` 38/38, `skills-ref validate` 38/38, `package_skill.py` 38/38 (each `.skill` holds only `<slug>/SKILL.md`) | **verified locally** for these tool versions |
| A value containing `---` survives skills-ref | Review on 2026-10-29 found that an earlier version exported such a value with exit 0 and skills-ref then rejected the file. After the fix, the opt-in `TestUpstreamSkillsRef` and `TestUpstreamQuickValidate` also export a fixture with `---` in the description and in a `metadata` value; skills-ref validates it and reads both values back unchanged (2026-10-29, OK). With the escape disabled, three default tests fail and the export refuses the file | **verified locally** |
| claude.ai accepts an uploaded export | none | **unverified**: no upload was made |
| The Skills API (`POST /v1/skills`) accepts an export | none (needs an API key and a paid account) | **unverified** |
| Claude Code loads an exported skill and ignores the `metadata` map | Claude Code docs only | **untested** |

## Other agents and editors

**Not supported.** Cursor, GitHub Copilot, OpenAI Codex, Kiro and other agents are not
targeted and not tested. The files are plain Markdown with YAML frontmatter, so they can be
read as reference documents anywhere, but no claim is made about discovery, metadata
handling or behaviour in those tools.

## Platforms

| Platform | What was actually run |
|---|---|
| Windows 11 + Git for Windows (bash 5.2), Python 3.13.2 | Default unit suite (2026-10-28: 502 outcomes, 498 passed, 2 failed on plugin drift, 2 skipped), example suite in a venv (2026-10-28: 327 tests OK), claude-CLI plugin tests (2026-10-28, and 2026-10-29 with the internal 2.1.312 and public 2.1.311 builds), opt-in CLI lifecycle and export tests (2026-10-29), installer suites (Phase 1, 2026-10-27; 2026-10-30, see `docs/INSTALLER.md` "Platforms") |
| Python 3.10 (3.10.22, Windows) | Phase 2 CI workstream, 2026-10-27, stdlib unit suite minus `test_install*.py`: **Ran 455 tests, FAILED (failures=2, skipped=13)**. One failure is a real 3.10 defect: a junction-linked skill folder was not reported `[UNSAFE]` by `list_skills.py` ([KNOWN-ISSUES](KNOWN-ISSUES.md) #12; since fixed, see the Inventory row above). The other, `test_evals.RealRepoTests.test_no_validation_errors`, failed while another workstream was editing evals and was reported to pass on rerun. Not re-run since; the 3.10 interpreter was not available on 2026-10-29, when `tests/test_export_skills.py` ran on 3.12 and 3.13 only. 3.10 is the declared minimum, not a fully passing platform |
| Linux (`ubuntu-latest`) | **Only in CI configuration**; no run of the current jobs has been observed. Linux runs by us were on WSL 2 Ubuntu 26.04, not a GitHub runner: the installer suites (2026-10-30, see the WSL row), the unit suite under coverage with only `requirements-dev.txt` installed (2026-10-29, `docs/QUALITY-GATES.md` "Coverage"), and the dependency locks plus the example suite with CPython 3.13.18 (2026-10-29, `docs/REPRODUCIBILITY.md`) |
| macOS (`macos-latest`, system bash 3.2, BSD tools) | **Only in CI configuration.** Not executed by us; bash-3.2 safety is by code review only |
| WSL, network filesystems, `TARGET_DIR/<slug>` as its own mount point | WSL 2 Ubuntu: installer suites run 2026-10-30 (116 OK, 0 skipped); mount points refused, including same-filesystem bind mounts on Linux; network filesystems not run; macOS has only the device-id check (see `docs/INSTALLER.md` "Platforms") |

## Keeping this page honest

Update a row only with a run you observed: the command, the date, the platform, and the
result. If a CLI or platform changes and the tests were not re-run, leave the older date in
place rather than implying a fresh result. `python scripts/quality_report.py` lists the
checks it did not run (for example `claude-cli`, `github-actions`, `other-platforms`) under
`skipped_checks`.
