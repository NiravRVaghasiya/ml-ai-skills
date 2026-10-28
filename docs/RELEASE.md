# Release process

This page covers versioning, the release steps, how users check a release, deprecation,
and how pinned actions and dependencies are updated. The release workflow
(`.github/workflows/release.yml`) has **not yet run on GitHub**: everything below about it
describes the workflow file, not an observed release.

## Versioning

The single version stamp is `VERSION` (currently `1.0.0`). `scripts/generate_plugins.py`
copies it into `.claude-plugin/marketplace.json` and every `plugins/<name>/.claude-plugin/plugin.json`,
and the release tag must be `v<VERSION>`.

The repo follows [Semantic Versioning 2.0.0](https://semver.org/spec/v2.0.0.html). "Public
contract" means everything a user or a tool can depend on by name. It is locked in
`schema/public-contract.json` and checked by `scripts/check_contract.py` (see
`docs/SKILL-SPEC.md`, "Public contract lock").

| Change | Bump | Enforced by |
|---|---|---|
| Remove or rename a skill slug | **major** | `check_contract.py --write` refuses it without a major bump |
| Remove or rename a plugin, or remove a skill from a plugin | **major** | same |
| Rename the marketplace | **major** | same |
| Remove an enum value (`domain`, `level`, `type`, `lifecycle`, `risk_level`, `evidence_level`) | **major** | same |
| Add a required frontmatter key, promote an optional key to required, remove or demote a required key, remove an optional key | **major** | same |
| Add a skill, plugin, enum value or optional key | minor | `check_contract.py --check` fails until re-locked |
| Mark a skill `lifecycle: deprecated` | minor | review (see below) |
| Content fixes inside a skill, new eval cases, tooling and docs changes | patch (or minor if user-visible behaviour of the tooling changes) | review |

Changes that are not in the contract lock but can still break someone are reviewed by hand
and called out in `CHANGELOG.md`: a skill's `type` changing between workflow and reference, a
`capabilities` tag being removed (it changes routing), or a script's CLI flag or exit code
changing.

## Release steps

1. **Start from a green `main`.** `release.yml` refuses a tag whose commit is not on the
   default branch or has no successful `ci.yml` push run for that exact commit. Tag the tip of
   `main` after CI is green. If you tag too early, wait for CI and re-run the release workflow.
2. **Bump `VERSION`** following the table above.
3. **Move the `[Unreleased]` entries in `CHANGELOG.md` under `## [<VERSION>] - <YYYY-MM-DD>`**
   and leave an empty `## [Unreleased]` heading. `check_contract.py` and `release.yml` both
   require a `## [<VERSION>]` heading.
4. **Regenerate every generated artifact**, then confirm nothing drifted:
   ```bash
   python scripts/generate_plugins.py --write
   python scripts/generate_index.py --fix
   python scripts/generate_readme_stats.py --write
   python scripts/build_dependency_graph.py --write
   python scripts/generate_schema.py --write
   python scripts/check_generated.py          # must report no drift
   ```
5. **Re-lock the contract:** `python scripts/check_contract.py --write`. It refuses breaking
   changes unless `VERSION`'s major is higher than the locked major, and refuses a `VERSION`
   lower than the locked one.
6. **Run the gates locally.** These are exactly the commands `release.yml`'s `verify` job
   runs (ruff and shellcheck come from `requirements-dev.txt`):
   ```bash
   ruff check .
   shellcheck install.sh uninstall.sh bin/ml-ai-skills
   python scripts/audit_workflows.py --strict
   python scripts/validate_skills.py --warnings
   python scripts/generate_schema.py --check
   python scripts/check_contract.py --check
   python scripts/validate_evals.py --strict --require-full-coverage
   python scripts/check_claims.py
   python scripts/check_freshness.py
   python scripts/check_generated.py
   python scripts/run_evals.py --out eval-report.json
   # unit tests by module list, tests/test_install*.py excluded (as release.yml does):
   PYTHONPATH=tests python -m unittest $(cd tests && ls test_*.py | grep -v '^test_install' | sed 's/\.py$//')
   ```
   Not part of `release.yml`, but recommended before tagging:
   ```bash
   python scripts/quality_report.py --run-tests --out quality-report.json   # evidence summary
   python scripts/build_release.py --list                                   # review what ships
   ```
7. **Commit, merge to `main`, wait for CI, then tag:** `git tag v<VERSION> && git push origin v<VERSION>`.
8. **The workflow takes over** (`release.yml`):
   - `verify` (read-only token): checks the tag is on the default branch with a green CI run,
     `VERSION` equals the tag, the CHANGELOG section exists; re-runs the lint/validate gates,
     offline evals and the stdlib unit tests; builds the tarball twice with
     `SOURCE_DATE_EPOCH` set to the tag commit time and requires identical bytes.
   - `publish` (the only job with write scopes): rebuilds on a fresh runner, requires the same
     SHA-256 as `verify`, creates a build-provenance attestation
     (`actions/attest-build-provenance`), and uploads `ml-ai-skills-<VERSION>.tar.gz` and
     `SHA256SUMS` to the GitHub release, with notes taken from the CHANGELOG section. If the
     release already has assets, it re-uploads (`--clobber`) only when the existing
     `SHA256SUMS` is byte-identical to the rebuilt one; otherwise it fails before attesting,
     so a re-pushed tag cannot silently replace published artifacts.

`release.yml` does not re-run the example, installer or plugin jobs itself. It relies on the
green `ci.yml` push run for the same commit.

## What ships

`python scripts/build_release.py --list` prints the exact file list. The tarball contains the
skill folders, `plugins/`, `.claude-plugin/`, `install.sh`, `uninstall.sh`, `bin/`, `scripts/`,
`schema/`, `docs/`, `INDEX.md`, `README.md`, `CHANGELOG.md`, `SECURITY.md`, `CONTRIBUTING.md`,
`LICENSE` and `VERSION`. It does not contain `tests/`, `evals/`, `.github/`, `_TEMPLATE/`,
`CLAUDE.md` or the requirements files. Inside the
tarball, `ml-ai-skills-<VERSION>/RELEASE-MANIFEST.sha256` lists the SHA-256 of every other file.
The builder refuses files with CRLF line endings.

Because `tests/` and `evals/` are not shipped, part of the tarball does not work on its own.
Observed on 2026-10-28 by building the tarball (`SOURCE_DATE_EPOCH=1700000000 python
scripts/build_release.py --out-dir ...`, twice, byte-identical), extracting it into an empty
directory and running each script there with `python -I`:

- **Work from the tarball:** `validate_skills.py`, `router.py`, `list_skills.py`,
  `check_freshness.py`, `check_contract.py --check`, `generate_schema.py --check`,
  `generate_index.py --check`, `build_dependency_graph.py --check` (each exited 0).
  `install.sh --target DIR` installed all 38 skills and `uninstall.sh --target DIR` removed
  them (both exited 0).
- **Fail or degrade without `evals/` or `tests/`:** `eval_router.py` (no benchmark),
  `validate_evals.py` and `run_evals.py` (no cases), `check_claims.py` (claims name test files
  that are not shipped), `generate_readme_stats.py --check` (it discovers `tests/`; ImportError)
  and therefore `check_generated.py`, and `quality_report.py` (it runs the above, so it reports
  hard failures and no eval coverage).
- **Dangling links:** the shipped `README.md`, `CONTRIBUTING.md` and docs link to
  `CLAUDE.md`, `.github/workflows/ci.yml`, `evals/` and `tests/`, none of which ship. Use the
  repository at the release tag for those.

## How users verify a release

```bash
sha256sum -c SHA256SUMS                         # the tarball matches the published checksum
gh attestation verify ml-ai-skills-<VERSION>.tar.gz --repo NiravRVaghasiya/ml-ai-skills
tar -xzf ml-ai-skills-<VERSION>.tar.gz
cd ml-ai-skills-<VERSION> && sha256sum -c RELEASE-MANIFEST.sha256   # every extracted file
```

What these do and do not show:

- The checksum shows the file is the one the release job uploaded.
- The attestation shows it was built by this repository's `release.yml` from the tagged
  commit. It does not show that the content is correct or safe; see `SECURITY.md`.
- Rebuilding from the tag (`SOURCE_DATE_EPOCH=$(git log -1 --format=%ct v<VERSION>) python
  scripts/build_release.py --require-clean --expect-version v<VERSION>`) should give the same
  bytes. Identical output was checked only for repeated builds on the same machine; across
  machines it also depends on the zlib version, so compare `RELEASE-MANIFEST.sha256` (which
  does not depend on compression) if the tarball hashes differ.

Plugin-marketplace users do not receive the tarball: `/plugin marketplace add
NiravRVaghasiya/ml-ai-skills` reads the repository itself. Pin a tag in your own tooling if
you need a fixed version.

## Deprecation policy

1. Set `lifecycle: deprecated` in the skill's frontmatter and add a sentence near the top of
   its `## Overview` naming the successor skill (`[[successor-slug]]`). There is no
   `supersedes` field (see `docs/SKILL-SPEC.md`).
2. Release that change in a **minor** version and note it in the CHANGELOG under
   "Deprecated".
3. Keep the deprecated skill, unchanged apart from fixes, for **at least one further minor
   release**.
4. Remove it only in a **major** release. `check_contract.py --write` refuses the removal
   otherwise. The CHANGELOG entry under "Removed" names the successor.

The same steps apply to plugins, enum values and optional frontmatter keys. The router does
not yet treat `deprecated` specially; a deprecated skill can still be selected.

## Updating pinned actions and dependencies

- **GitHub Actions** are pinned to full commit SHAs with a `# vX.Y.Z` comment.
  Dependabot (`.github/dependabot.yml`) opens a weekly grouped PR that updates both. To bump
  by hand, resolve the tag with `git ls-remote https://github.com/<owner>/<action>
  'refs/tags/vX.Y.Z^{}' 'refs/tags/vX.Y.Z'` (use the peeled `^{}` line when present), check
  `runs.using` in that commit's `action.yml` (node24 or later), update the SHA and the comment
  in every workflow, then run `python scripts/audit_workflows.py --strict`. The full procedure
  is in the header of `.github/workflows/ci.yml`.
- **Python tools and example dependencies** are pinned in `requirements-dev.txt` and
  `requirements-examples.txt`; Dependabot proposes weekly bumps (dev tools and example
  dependencies are grouped separately). Before merging an example-dependency bump, run the
  example suite (`tests/examples`) against the new pins. Whether Dependabot handles the
  `+cpu` torch pins and the extra index URL has not been checked.
- **The Claude Code CLI** version (`CLAUDE_CODE_VERSION` in `ci.yml`) is not covered by
  Dependabot. Bump it by hand after checking `https://registry.npmjs.org/@anthropic-ai/claude-code`.
- CI runs `pip-audit` on the direct pins and on the installed dev and example environments,
  so a newly published advisory can turn CI red without any repo change. That is intended.

## Compatibility policy

- The supported environments and the tests behind each are in `docs/COMPATIBILITY.md`. A
  release does not widen that list unless a test for the new environment was run.
- Python tooling targets Python 3.10 and later (`ruff.toml` targets py310; CI is configured to
  run the unit suite on 3.10 and 3.13, but has not run yet). The only observed 3.10 run had one
  real failure, on Windows (`docs/COMPATIBILITY.md`, `docs/KNOWN-ISSUES.md` #12).
- A skill's frontmatter stays readable by the stdlib parser in `scripts/skills_lib.py`
  (the supported YAML subset in `docs/SKILL-SPEC.md`). A new syntax form is a breaking change
  for that parser and needs a deliberate spec change.
- The installer's manifest format is versioned (`# format: 2`). Older formats are upgraded on
  the next install; an unknown newer format is refused rather than guessed. A new manifest
  format is at least a minor release, and must keep reading the previous one.
