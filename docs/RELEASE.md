# Release process

This page covers versioning, the release steps, how users check a release, deprecation,
and how pinned actions and dependencies are updated. The release workflow
(`.github/workflows/release.yml`) has **no recorded run on GitHub**: everything below about
it describes the workflow file, not an observed release.

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
   default branch or has no successful `ci.yml` push run on the default branch for that exact
   commit. Tag the tip of `main` after CI is green. If you tag too early, wait for CI and
   re-run the release workflow. If `main` was force-pushed and the CI base step cannot fetch
   the old tip, push a new commit: `release.yml` accepts only a successful push run of the
   tagged commit, so re-running the failed run or a `workflow_dispatch` run does not help
   (`docs/QUALITY-GATES.md` "Base commit").
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
   `release.yml` also runs the archive self-containment tests with the slow checks on
   (`ML_AI_SKILLS_RELEASE_SLOW=1 PYTHONPATH=tests python -m unittest -v test_release_archive`,
   10+ minutes: it runs `check_generated.py` and the stdlib unit suite from the extracted
   tarball). Not part of `release.yml`, but recommended before tagging:
   ```bash
   python scripts/quality_report.py --run-tests --out quality-report.json   # evidence summary
   python scripts/build_release.py --list                                   # review what ships
   ```
7. **Commit, merge to `main`, wait for CI, then push a signed, annotated tag** on that commit:
   ```bash
   git switch main && git pull --ff-only
   git tag -s v<VERSION> -m "ml-ai-skills <VERSION>"   # -s: signed and annotated
   git tag -v v<VERSION>                               # check the signature locally
   git push origin v<VERSION>
   ```
   The signing key (GPG, SSH or S/MIME) must be registered on your GitHub account, because
   `release.yml` accepts only a tag that GitHub reports as `verified`. A lightweight tag
   (`git tag v<VERSION>`) or an unsigned annotated tag is refused. Setup steps for a first
   release are in "The first tagged release" below.
8. **The workflow takes over** (`release.yml`):
   - `verify` (read-only token): reads the tag from the GitHub API and requires an annotated
     tag with a GitHub-verified signature, pointing at the checked-out commit; checks the
     commit is on the default branch with a green `ci.yml` push run there, `VERSION` equals
     the tag, the CHANGELOG section exists; re-runs the lint/validate gates, offline evals, the
     stdlib unit tests and the archive self-containment tests; builds the tarball twice with
     `SOURCE_DATE_EPOCH` set to the tag commit time (the first build also refuses
     generated-artifact drift), requires identical bytes, and runs
     `build_release.py --verify-archive` on the result.
   - `publish` (the only job with write scopes): checks the tag still points at the verified
     commit, rebuilds on a fresh runner (with the OIDC token-request variables removed from the
     build's environment), requires the same SHA-256 as `verify`, creates a build-provenance
     attestation (`actions/attest-build-provenance`), and uploads `ml-ai-skills-<VERSION>.tar.gz`
     and `SHA256SUMS` to the GitHub release, with notes taken from the CHANGELOG section. If the
     release already has assets, it re-uploads (`--clobber`) only when the existing
     `SHA256SUMS` is byte-identical to the rebuilt one; otherwise it fails before attesting,
     so a re-pushed tag cannot silently replace published artifacts.

`release.yml` does not re-run the example, installer or plugin jobs itself. It relies on the
green `ci.yml` push run for the same commit. It has been checked locally with
`scripts/audit_workflows.py --strict` and actionlint (with shellcheck), not run on GitHub.

## Pre-release external actions

These need repository settings or credentials, so no script or workflow here can do them.
Check them before pushing a release tag:

- **Security contact.** While `SECURITY.md` still says `Security contact: PENDING`, there is no
  published private contact (`docs/KNOWN-ISSUES.md` #23). Enable GitHub
  private vulnerability reporting (steps in `SECURITY.md`, "Maintainer configuration")
  and replace that line as its `MAINTAINER-CONFIG` comment describes: a monitored address or URL, or
  `none: use private vulnerability reporting`. Never put a guessed address there.
- **Tag signing key** registered on the GitHub account that pushes the tag ("The first tagged
  release", step 2); `release.yml` refuses a tag GitHub does not report as verified.
- **A green `ci.yml` push run on `main`** for the commit being tagged (release step 1).
- **Server-side release controls.** Every release gate lives in the tagged commit's own
  `release.yml`, so anyone with write access can push a `v*` tag on unreviewed code whose
  `release.yml` skips the gates, and get an attested release. Two repository settings close this.
  First, a tag ruleset on `refs/tags/v*` that limits who may create, move or delete those tags.
  Second, a protected deployment environment (for example `release`) with required reviewers.
  The `publish` job does not name an environment yet, so this second control also needs an
  `environment:` line added to that job in `release.yml`. Nothing in this repository records
  either setting as done; the repository settings were not checked.

## The first tagged release

No release tag exists yet (`docs/KNOWN-ISSUES.md` #21), and `CHANGELOG.md` calls 1.0.0 the
"initial state (untagged)". No tooling here creates tags or releases; the maintainer does it
once by hand:

1. **Pick the version.** Do not tag the historical 1.0.0 commit: it predates the current
   `release.yml` and `build_release.py`, and has no green `ci.yml` push run, so the workflow
   would refuse it. Release the current `main` as the next version instead (minor or major
   per the table above; `check_contract.py --write` says which) and follow "Release steps".
   1.0.0 stays an untagged, unpublished entry in `CHANGELOG.md`; the first tagged release
   is the first verifiable one.
2. **Register a signing key on GitHub** (Settings > SSH and GPG keys). For an SSH key:
   `git config --global gpg.format ssh` and
   `git config --global user.signingkey ~/.ssh/id_ed25519.pub`. For GPG: `gpg --list-secret-keys
   --keyid-format=long`, then `git config --global user.signingkey <KEYID>` and upload
   `gpg --armor --export <KEYID>`. The tag's tagger email must be a verified email on that
   GitHub account, or GitHub reports the signature as unverified.
3. **Local verification of SSH-signed tags** needs an allowed-signers file:
   `echo "<email> $(cat ~/.ssh/id_ed25519.pub)" > ~/.config/git/allowed_signers` and
   `git config --global gpg.ssh.allowedSignersFile ~/.config/git/allowed_signers`; then
   `git tag -v v<VERSION>` prints `Good "git" signature`.
4. **Push the tag, then confirm what GitHub recorded** (any clone, read-only):
   ```bash
   gh api repos/NiravRVaghasiya/ml-ai-skills/git/ref/tags/v<VERSION> --jq '.object'   # type must be "tag"
   gh api repos/NiravRVaghasiya/ml-ai-skills/git/tags/<sha from above> \
     --jq '{commit: .object.sha, verified: .verification.verified, reason: .verification.reason}'
   ```
   `verified` must be `true` and `commit` the `main` commit you meant to release. Then watch
   the `release` workflow run; it repeats these checks.
5. **Record the release history** so later readers can trace it: the CHANGELOG section for
   the version, the tag (`git tag -v`), the workflow run URL, and the attestation
   (`gh attestation verify ml-ai-skills-<VERSION>.tar.gz --repo NiravRVaghasiya/ml-ai-skills`
   prints the workflow, commit and run that built it). Remove item #21 from
   `docs/KNOWN-ISSUES.md` once a tag and its release exist, citing the run.

If a tag is pushed by mistake, delete it before any release is published (`git push origin
:refs/tags/v<VERSION>` and `git tag -d v<VERSION>`). After a release is published, never re-point
its tag: bump the version instead (the workflow refuses to replace published assets).

## What ships

The tarball is a **self-contained developer distribution**: extracted anywhere (no git
needed), it can run the same validators, evals, generators' `--check` modes and stdlib unit
tests as a checkout of the tag. `python scripts/build_release.py --list` prints the exact file
list.

- **Contents:** every file git would commit (`git ls-files --cached --others
  --exclude-standard`). Every tracked file ships; `release.yml` also requires a clean tree, so
  the release is exactly the tagged commit's files. That is the skill folders, `plugins/`, `.claude-plugin/`, `bin/`,
  `scripts/`, `schema/`, `docs/`, `tests/` (including `tests/examples/`), `evals/`
  (including `evals/routing/`), `.github/` (workflows and `dependabot.yml`), `_TEMPLATE/`,
  `constraints/`, and the top-level files `install.sh`, `uninstall.sh`, `INDEX.md`,
  `README.md`, `CHANGELOG.md`, `CLAUDE.md`, `CONTRIBUTING.md`, `SECURITY.md`, `LICENSE`,
  `VERSION`, `requirements-dev.txt`, `requirements-examples.txt`, `ruff.toml`, `.coveragerc`,
  `.shellcheckrc`, `.gitattributes` and `.gitignore`.
- **Never shipped:** `.git/`, caches (`__pycache__/`, `.ruff_cache/`, `.pytest_cache/`, ...),
  editor and agent state (`.claude/`, `.vscode/`, ...), build output (`dist/`, the `--out-dir`
  if it is inside the tree), virtualenvs, `validate_out.json`, logs and other `.gitignore`d
  files. The rules are `EXCLUDED_*` in `scripts/build_release.py`; in a git checkout they apply
  only to untracked files.
- **Fail closed:** a file that is neither in `SHIPPED_FILES`/`SHIPPED_TREES` (or a skill
  folder) nor excluded stops the build with its path, so a new top-level file is never shipped
  or dropped silently. A *tracked* file that matches an `EXCLUDED_*` rule (say a committed
  `*.log` fixture) also stops the build: untrack it or narrow the rule. Symlinks, submodules,
  special files and text files with CRLF line endings are refused, and so are path names that
  do not extract the same everywhere: characters outside `[A-Za-z0-9._@+-/]` (no spaces, no
  `:`), a component ending in `.`, Windows device names (`nul`, `CON.md`, `com1`, ...), and two
  paths that differ only in case or where one is a file and the other a directory.
- **Generated artifacts must be current:** the build runs `scripts/check_generated.py` first and
  refuses any drift (`--skip-generated-check` exists for local iteration; the release workflow
  uses it only for rebuilds of a commit the first build already checked).
- **Manifest:** `ml-ai-skills-<VERSION>/RELEASE-MANIFEST.sha256` lists the SHA-256 of every other
  file, sorted by path.

Checked on 2026-10-29 (Windows 11, CPython 3.13.2) on a fresh clone of commit `51743b0` with this
change's `build_release.py` overlaid: the archive (1,180 files) was built, verified and safely
extracted with `build_release.py --verify-archive ... --extract-to`, and every command below
exited 0 when run from the extracted tree with `python -I` and git discovery blocked
(`GIT_CEILING_DIRECTORIES`): `validate_skills.py --warnings`, `validate_evals.py --strict
--coverage` and `--strict --require-full-coverage`, `eval_router.py --split test`,
`run_evals.py --out FILE`, `check_claims.py`, `check_freshness.py`, `generate_schema.py
--check`, `check_contract.py --check`, `generate_index.py --check`, `generate_plugins.py
--check`, `generate_readme_stats.py --check`, `build_dependency_graph.py --check`,
`quality_report.py --out FILE` and `check_generated.py` (its no-git fallback walks the tree).
`tests/test_release_archive.py` repeats the fast ones in ci.yml's `release-archive` job (the
`unit` job leaves the module out); with `ML_AI_SKILLS_RELEASE_SLOW=1`, as in release.yml's
`verify` job, it adds `check_generated.py` and the stdlib unit suite from the extracted tree.
Neither job has been observed running on GitHub. Not yet observed: a fully green automated `ML_AI_SKILLS_RELEASE_SLOW=1` run on a clean committed tree.
The last one (same day) passed the unit suite from the archive but failed
`check_generated.py` from the archive on README test-count drift that did not reproduce by
hand; the suspected cause is that `generate_readme_stats.py` counts tests by importing them,
and an import-time subprocess probe can time out under load.

**Outside the archive contract** (they need git history or network, so use a clone of the tag):

- `check_contract.py --check --base-ref <base commit>` and the routing gate's no-regression
  check in `ci.yml` (they read the base commit; `docs/QUALITY-GATES.md` "Base commit");
- `check_generated.py --in-place` (it compares `git status` before and after);
- the tests that need a git checkout of this repository (`tests/test_release_gitattributes.py`'s
  clone and index-mode checks), which skip with that reason in an extracted tree;
- the example suite needs `requirements-examples.txt` installed, and `verify_online.py`,
  `validate_skills.py --check-links` and the plugin CLI tests need network or the `claude` CLI,
  exactly as in a checkout.

## How users verify a release

```bash
sha256sum -c SHA256SUMS                         # the tarball matches the published checksum
gh attestation verify ml-ai-skills-<VERSION>.tar.gz --repo NiravRVaghasiya/ml-ai-skills
tar -xzf ml-ai-skills-<VERSION>.tar.gz
cd ml-ai-skills-<VERSION> && sha256sum -c RELEASE-MANIFEST.sha256   # every extracted file
```

Without `sha256sum` (for example on Windows), or to extract with checks against hostile
archives, use the builder from a checkout of the tag or from a previous release:

```bash
python scripts/build_release.py --verify-archive ml-ai-skills-<VERSION>.tar.gz --extract-to DIR
```

It requires the sibling `SHA256SUMS` and checks the tarball against it (`--no-sums` skips
that, and the summary then says `SHA256SUMS: not checked`). Before writing anything it refuses
absolute paths, `..`, backslashes, drive letters, links, devices and FIFOs, unexpected modes,
the non-portable names the builder refuses (spaces, `:`, trailing `.`, Windows device names),
duplicate or case-colliding names, a path that is both a file and a directory, and files not
listed in the manifest, and it verifies every SHA-256. It then writes into a staging
directory inside the empty `DIR`, checks that the files on disk are exactly the archive's,
and renames the tree into place; on any failure it removes what it wrote.

This is an **integrity** check only: `RELEASE-MANIFEST.sha256` and a `SHA256SUMS` downloaded
next to the tarball are written by whoever built them. Authenticity comes from
`gh attestation verify` above.

What these do and do not show:

- The checksum shows the file is the one the release job uploaded.
- The attestation shows it was built by this repository's `release.yml` from the tagged
  commit. It does not show that the content is correct or safe; see `SECURITY.md`.
- Rebuilding from the tag (`SOURCE_DATE_EPOCH=$(git log -1 --format=%ct v<VERSION>) python
  scripts/build_release.py --require-clean --expect-version v<VERSION>`) should give the same
  uncompressed tar stream and the same `RELEASE-MANIFEST.sha256`; the `.tar.gz` bytes are the
  same only with the same deflate implementation. Compare the manifest (or `gzip -dc ... |
  sha256sum`) if the tarball hashes differ. The exact guarantee and what was measured are in
  `docs/REPRODUCIBILITY.md`, "Release archive".

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
  `requirements-examples.txt`. Dependabot proposes weekly bumps: dev tools grouped for
  minor/patch; torch/torchvision and transformers/tokenizers grouped; one PR per pin for every
  other example dependency. Each bump must regenerate the matching lock in `constraints/`
  (download the `ci.yml` deps job's `dependency-locks` artifact). Before merging an example-dependency bump, run the
  example suite (`tests/examples`) against the new pins. Whether Dependabot handles the
  `+cpu` torch pins and the extra index URL has not been checked.
- **The Claude Code CLI** version (`CLAUDE_CODE_VERSION` in `ci.yml`) is not covered by
  Dependabot. Bump it by hand after checking `https://registry.npmjs.org/@anthropic-ai/claude-code`.
- CI runs `pip-audit` on the direct pins, both locks and the installed dev and example environments,
  so a newly published advisory can turn CI red without any repo change. That is intended.

## Compatibility policy

- The supported environments and the tests behind each are in `docs/COMPATIBILITY.md`. A
  release does not widen that list unless a test for the new environment was run.
- Python tooling targets Python 3.10 and later (`ruff.toml` targets py310; CI is configured to
  run the unit suite on 3.10 and 3.13, but no result of that job is recorded yet). The only observed 3.10 run had one
  real failure, on Windows (`docs/COMPATIBILITY.md`, `docs/KNOWN-ISSUES.md` #12).
- A skill's frontmatter stays readable by the stdlib parser in `scripts/skills_lib.py`
  (the supported YAML subset in `docs/SKILL-SPEC.md`). A new syntax form is a breaking change
  for that parser and needs a deliberate spec change.
- The installer's manifest format is versioned (`# format: 3`, per-file hashes; formats 1 and
  2 are still read). Checkouts from before format 3 refuse a format-3 manifest. Older formats are upgraded on
  the next install; an unknown newer format is refused rather than guessed. A new manifest
  format is at least a minor release, and must keep reading the previous one.
