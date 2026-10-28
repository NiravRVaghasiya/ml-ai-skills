# Installer contract (`install.sh` / `uninstall.sh`)

`install.sh` copies this repo's `<slug>/SKILL.md` folders into a Claude Code skills
directory (`~/.claude/skills/` by default; `--project [DIR]` or `--target DIR` to pick
another). Re-running it is the update path. `uninstall.sh` removes what the
installer put there. `bin/ml-ai-skills` is a thin wrapper around both, and
`scripts/list_skills.py --target DIR` reports what is installed. All three read the
same manifest format, described below. The wrapper resolves symlinks to itself (or to
its `bin/` folder) before locating the checkout, and refuses to run anything from a
directory without `INDEX.md` and `schema/allowed_values.py`. So a
`~/bin/ml-ai-skills` symlink runs the checkout's scripts, not an `install.sh` that
happens to sit next to the link. This is tested in `tests/test_install.py::TestWrapper`;
the file-symlink case was skipped on the Windows test host, and only the linked-`bin/`
case ran there.

The scripts need only `bash` and standard tools: `cp`, `mkdir`, `mv`, `rm`, `sort`,
`date`, plus `sha256sum` (GNU coreutils) or `shasum` (macOS/Perl). They need no
Python and no network access. `git` is optional. `install.sh` uses it only to
upgrade old format-1 manifests (see [Manifest format](#manifest-format)).

Both scripts accept `--skill SLUG` (repeatable) to act on named skills only. The main
use is a targeted override such as `./install.sh --force --skill my-edited-skill`,
which leaves every other skill's protection in place.

## Threat model

The thing being protected is the user's files: everything outside `TARGET_DIR`, and
anything inside `TARGET_DIR` that the installer did not create or that the user has
changed since it was installed.

| Input | Trusted? | Why it matters |
|---|---|---|
| The manifest file in `TARGET_DIR` | **No.** Users hand-edit it, Windows editors add CRLF, and it can be corrupted or planted | Before this contract existed, a manifest line `../victim` made `uninstall.sh` run `rm -rf` on a directory *outside* the target |
| `--skill` arguments | **No.** They are user input | `--skill ../x` must not turn into a path |
| Folder names in the source checkout | **No.** A checkout can contain odd or planted names | They become paths under `TARGET_DIR` |
| Symlinks/junctions in the source or the target | **No** | Copying or following them can read or write outside the intended tree |
| The source checkout's file *contents* | Yes, they are what you asked to install | Out of scope: a malicious `SKILL.md` is a content-review problem, not an installer problem |
| A local attacker who can write `TARGET_DIR` *while a run is in progress* | Out of scope | Such an attacker could plant skills directly. The checks reduce but don't eliminate check-then-use races |

## Guarantees

Each guarantee below has an automated test in `tests/test_install_security.py`,
except where marked *(not automated)*.

1. **Slugs are validated, never sanitised.** Every manifest line, every `--skill`
   argument and every source folder name must match `^[a-z0-9]+(-[a-z0-9]+)*$`
   (C locale). How each kind of failure is handled:
   - An invalid manifest line is ignored with a warning, never used as a path, and
     dropped the next time the manifest is written.
   - An invalid `--skill` value aborts with exit code 2 before anything changes.
   - An invalid source folder is skipped with a warning.
   - A trailing CR (CRLF manifests) and surrounding whitespace are stripped before
     the line is validated.
   - A manifest that contains a NUL byte is refused as corrupt (exit 1, nothing
     changes). bash's `read` silently drops NUL, so `al<NUL>pha` would otherwise be
     cleaned into the valid slug `alpha`. `list_skills.py` refuses it too.
2. **Local edits are preserved.** The manifest records the sha256 of each installed
   `SKILL.md`.
   - On update, a managed skill whose current hash differs from the recorded one is
     kept, with a warning. `--force --skill SLUG` overwrites just that skill;
     `--force` alone overwrites all of them.
   - `uninstall.sh` likewise keeps modified skills unless you pass `--force`
     (optionally narrowed with `--skill`).
   - A missing `SKILL.md` counts as modified.
   - *Limitation:* only `SKILL.md` is hashed. If you add or edit other files inside a
     managed skill folder, that is not detected, and those files are replaced or
     removed along with the folder.
3. **Folders the installer did not create are never touched** unless you pass
   `--force`. With `--force` the folder is replaced and from then on tracked as
   managed.
4. **Skills removed upstream stay tracked.** If a managed skill is no longer in the
   source, it is left installed, kept in the manifest, and reported as an `orphan`.
   `--prune` removes it if it is unmodified; `--prune --force` removes it in any
   case. An orphan that is already gone from disk is simply dropped from the
   manifest.
5. **Each skill is swapped in with two renames, with rollback.** The install runs in three stages:
   - **Stage.** All selected skills are first copied into a staging area inside
     `TARGET_DIR`, on the same filesystem. If that copy fails, nothing in the target
     has changed yet.
   - **Commit.** Each skill is then committed with two renames: the old folder is
     moved aside into the lock directory, then the new folder is moved into place.
     If either rename fails, the old copy is moved back.
   - **Interrupted commit.** Rollback is armed *before* the first rename, and it
     decides what to do from what is on disk, not from how far the code got. This
     matters because bash runs a signal trap only after the running `mv` exits:
     - If the new copy has landed, its hash is recorded.
     - If the old copy was moved aside but not replaced, it is moved back.
     - If something else now occupies `TARGET_DIR/<slug>` (an explicit failure, or a
       folder that appeared mid-run), the old copy is *not* deleted. The lock
       directory is kept with the old copy in `.ml-ai-skills.lock/old/<slug>`, the
       error names that path, and the next run exits 3 until you inspect it.

     Further HUP/INT/TERM signals are ignored while the cleanup runs. For SIGTERM this
     is tested by `TestInterruptedCommit::test_sigterm_during_cleanup_is_ignored`: a
     SIGTERM while the cleanup renames the manifest into place leaves the exit status,
     the manifest and the lock release unchanged. HUP and INT are *(not automated)*.
   - **Clean up.** The old copies are deleted only after the run finishes, when the
     lock directory is removed.
6. **The manifest matches the disk.** It is written to a temp file and renamed over
   the old one. The `EXIT` trap also rewrites it after a partial failure. Only
   entries that still exist on disk are recorded, so the next run, `uninstall.sh` and
   `list_skills.py` see what is really installed.
7. **One run at a time.** The installer takes a lock by creating
   `TARGET_DIR/.ml-ai-skills.lock/` with `mkdir`, which is atomic on every tested
   platform. A second run fails immediately with exit code 3 and changes nothing.
   The lock directory records `pid=… started=…`.
   - *Stale lock* (left behind by a run that was killed with SIGKILL, a power loss,
     or similar): first check `.ml-ai-skills.lock/old/`. Any `<slug>` folder there is
     the previous copy of a skill that was being replaced. If `TARGET_DIR/<slug>` is
     missing, move that folder back. Then delete `.ml-ai-skills.lock/` and re-run.
   - *Signals:* SIGTERM delivered at three points of a commit was tested by
     `TestInterruptedCommit`, which uses a `PATH` wrapper around `mv`:
     - while the old copy is being moved aside;
     - right after the new copy lands (update);
     - right after the new copy lands (fresh install).

     In each case the run exits 143, the manifest matches the disk, and the lock is
     released. Ctrl-C (SIGINT) and SIGHUP go through the same trap path but are
     *(not automated)*. The lock is deliberately kept in one case: an old copy that
     could not be restored (see guarantee 5).
   - *A regular file at the lock path* is not a lock that any run could have
     created. It is reported as an error (exit 1), not as "lock held" (exit 3).
8. **Links are never followed or copied.** The following are refused, with a
   message and exit code 1:
   - a source skill folder that *is* a symlink, or that contains a symlink or
     junction or a non-regular file
   - a destination `TARGET_DIR/<slug>` that is a symlink or junction, or a managed
     folder that contains one
   - a symlinked manifest
   - a symlinked lock path

   The staged copy is re-checked before commit. `TARGET_DIR` itself *may* be a
   symlink, for example a dotfiles setup. It is resolved once, and the run then
   operates inside the resolved directory; the link itself is left in place
   (`TestLinks::test_full_cycle_through_a_linked_target_dir`, run with an NTFS
   junction on the Windows test host).
9. **Nothing is written outside `TARGET_DIR`.** The only exception is
   `mkdir -p TARGET_DIR` itself. All paths are `TARGET_DIR/<validated slug>`, the
   manifest, or the lock directory. The scripts never use `eval`, and they quote
   every expansion, so paths containing spaces, quotes or `$` work. A `--target`,
   `--project` or `--source` value that starts with `-` is a relative path (`./-x`),
   never a `cd`/`mkdir` option: `--target -` installs into `./-`, not into
   `$OLDPWD`. The scripts and `bin/ml-ai-skills` unset `CDPATH`, so an exported
   `CDPATH` cannot redirect a relative path either
   (`TestOptionLikePaths::test_exported_cdpath_is_ignored`,
   `TestWrapper::test_exported_cdpath_does_not_redirect_the_wrapper`).
10. **`--dry-run` writes nothing.** That includes the lock, the staging area, the
    manifest and the target directory. The test compares a full snapshot of the
    temp tree before and after a dry run.
11. **Refusal by design:**
    - `TARGET_DIR` equal to the source checkout is refused.
    - A target path that is a regular file is refused.
    - An unknown future manifest format (`# format: 3`) is refused rather than
      guessed at.
    - If neither `sha256sum` nor `shasum` exists, the scripts refuse to run instead
      of silently dropping guarantee 2 *(not automated: both tools were present on
      the test host)*.

## Manifest format

The manifest is `TARGET_DIR/.ml-ai-skills-manifest`. It is plain text with LF line
endings, and its entries are sorted by slug.

```text
# ml-ai-skills manifest — managed by install.sh/uninstall.sh, do not hand-edit
# format: 2
# source: /home/me/ml-ai-skills
# updated_at: 2026-10-27T19:48:19Z
alpha<TAB>3965929b60ad8defccac73ad2aacda5522e54f0c8b2230a502c6d1cb0b8e2144
beta<TAB>-
```

- **Format 2** (current): each line is `slug<TAB>sha256 of the installed SKILL.md`.
  A hash of `-` means "unknown". This is only kept for a format-1 entry that could
  not be verified.
- **Format 1** (legacy, no `# format:` line): each line is a bare slug. It is still
  read, and on the next `install.sh` run the manifest is upgraded to format 2. A
  format-1 entry has no install-time hash, so `install.sh` tries to prove each copy
  unedited. If it can, it records the copy's current sha256 and treats it as a
  normal managed skill:
  - **The copy equals the source's current `SKILL.md`.** Proven unedited.
  - **The source is a git checkout** (the documented `git pull && ./install.sh`
    update path) **and the copy's git blob id equals one that `<slug>/SKILL.md` has
    had anywhere in `HEAD`'s history.** Then it is an unedited earlier release.
    This uses two `git` processes in total (`git log --raw` and
    `git hash-object --no-filters`) and never writes to the repository.
  - **Anything else** stays `-`. It is kept and never overwritten or removed
    without `--force`, because it may hold your edits.

  *Known gap:* without git or a git checkout, for example with a downloaded archive,
  an installed copy that is merely an *older* release can't be told apart from an
  edited one. Such entries stay `-` after the upgrade. Re-running `install.sh` will
  not change that, and the messages no longer claim it will. The way out is
  per-skill: inspect the folder, then `./install.sh --force --skill SLUG` (or
  `./uninstall.sh --force --skill SLUG`). A copy byte-identical to a release but
  with git-normalised line endings (e.g. checked out with `core.autocrlf=true`) does
  not match its blob either. It is kept, which errs on the safe side. *(This
  follows from `--no-filters`. It was not tested.)*
- `uninstall.sh` will not remove a skill with an unknown hash unless you pass
  `--force` (narrow it with `--skill`). Its message tells a still-format-1 manifest
  ("run `install.sh` once") apart from a `-` that `install.sh` already failed to
  prove ("inspect it, then `--force --skill SLUG`"). `list_skills.py` makes the same
  distinction in its `[UNVERIFIED]` note. To keep that distinction true,
  `uninstall.sh` never upgrades the format. It rewrites the manifest only when it
  removed or dropped an entry, and it rewrites a format-1 manifest as format 1
  (no `# format:` line, bare slugs). Only `install.sh` upgrades to format 2.
  `install.sh`'s own "keep" message gives the reason per skill: it mentions the
  source's git history only for a copy that comparison actually covered, and a
  folder with no `SKILL.md` is reported as such rather than blamed on git
  (`TestManifestV1Upgrade::test_keep_reason_is_per_skill`).
- **All formats:** lines starting with `#` are metadata. Blank lines are ignored. If
  a slug appears more than once, the first entry wins.
- **Unknown formats:** a `# format:` value other than 1 or 2 is an error. Upgrade
  the checkout rather than let an old script misread a newer manifest.

## Exit codes

| Code | install.sh / uninstall.sh |
|---|---|
| 0 | Success. Protective skips (local edits, folders the installer didn't create, orphans) are reported as `WARN:` lines and in the summary counts. They are not errors |
| 1 | Error. Possible causes: a bad source checkout, no manifest found for uninstall, an unsafe link or file type, an unexpected regular file at the lock path, an I/O failure, an unknown manifest format, or no sha256 tool. Every skill that could be processed was processed, and the manifest matches the disk. In the one case where an old copy could not be restored, the lock directory is kept to preserve it (guarantee 5) |
| 2 | Usage error: an unknown flag, a missing flag argument, or an invalid `--skill` slug (either script). Nothing is changed |
| 3 | Another run holds the lock. Nothing is changed |

`list_skills.py --target` exits 1 when there is no manifest, the manifest is a
symlink, its format is unknown, or it contains a NUL byte. Otherwise it exits 0. The skills it flags appear
as `[MODIFIED]`, `[UNVERIFIED]`, `[MISSING]` and `[UNSAFE]`, followed by a count line.
`[UNSAFE]` covers everything the scripts refuse: a `TARGET_DIR/<slug>` that is a
symlink/junction or not a directory, or a folder containing a symlink/junction or a
non-regular file at any depth. Characters the console's encoding can't represent
(e.g. a non-Latin profile path on a cp1252 pipe under Git Bash) are printed as
`\uXXXX` escapes rather than crashing the listing.

## Test-only fault injection

`ML_AI_SKILLS_FAULT_INJECT` makes `install.sh` fail at a chosen point, so the rollback
paths can be exercised the same way on every OS:

- `stage`: makes the staging copy fail.
- `commit:<slug>`: makes the commit of `<slug>` fail *after* its old copy has been
  moved aside, which is the worst moment to fail.

The variable only ever causes a failure. It cannot redirect any path. Don't set it
outside tests.

Signal timing and "a folder appeared mid-commit" are tested without any hook in the
scripts. `tests/test_install_security.py::TestInterruptedCommit` puts a wrapper `mv`
first on `PATH`. The wrapper does the real move, then either sends SIGTERM to the
calling script or recreates the folder it just moved away.

`scripts/list_skills.py` parses the manifest with the same grammar as the scripts:
it strips only ASCII whitespace (C-locale `[:space:]`, so U+00A0 is *not* stripped)
and compares `# format:` as a string, so `02` is refused. A NUL byte anywhere is
refused, as in the scripts. `tests/test_list_skills.py` cross-checks the parsing, the
`[UNSAFE]` set and NUL refusal against `uninstall.sh --dry-run`.

## Platforms

**Tested:** Windows 11 with Git for Windows (MSYS2), `bash` 5.2.37, and Python 3.13.
That covers the full `tests/test_install.py`, `tests/test_install_security.py` and
`tests/test_list_skills.py` suites. On this host:

- Real symlinks could not be created without privilege (WinError 1314). The
  directory-link tests therefore ran with NTFS junctions, which Git Bash reports as
  symlinks.
- The three file-symlink tests were skipped (two in `tests/test_install_security.py`,
  and `TestWrapper::test_symlinked_wrapper_uses_the_real_checkout` in
  `tests/test_install.py`).

**Not run by us:**

- **Linux.** CI (`ubuntu-latest`) runs the installer tests, but no result from that
  run is recorded here.
- **macOS, and bash 3.2 in particular.** The scripts avoid bash-4-only features: no
  associative arrays, no `mapfile`, no `${x,,}`, no namerefs, and empty arrays are
  guarded under `set -u`. But they have not been executed on bash 3.2 or with BSD
  `cp`/`mv`/`shasum`.
- **WSL**, and filesystems where `TARGET_DIR/<slug>` is its own mount point. There,
  `mv` falls back to copy+delete, so the rename in guarantee 5 would no longer be a
  single atomic step.

**Performance note:** process creation was about 0.3 s per fork on the Windows test
host, so the scripts batch their hashing and staging into a few processes. A full
install of the library still does about two `mv` calls per skill, because each
rename is a separate step that has to be able to roll back.
