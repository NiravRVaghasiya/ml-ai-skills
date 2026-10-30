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
`date`, `stat` (GNU `stat -c` or BSD `stat -f`), plus one SHA-256 tool: `sha256sum`
(GNU coreutils), else `shasum` (macOS/Perl), else `openssl` (`openssl dgst -sha256 -r`).
They need no Python and no network access. `git` is optional. `install.sh` uses it only
to upgrade old format-1 manifests (see [Manifest format](#manifest-format)).

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
| Folder and file names in the source checkout | **No.** A checkout can contain odd or planted names | They become paths under `TARGET_DIR` |
| Symlinks/junctions in the source or the target | **No** | Copying or following them can read or write outside the intended tree |
| Mount points inside `TARGET_DIR` | **No** | A folder on another filesystem can't be swapped with one rename, and deleting it would delete files on that filesystem; deleting through a bind mount deletes files outside `TARGET_DIR` |
| The source checkout's file *contents* | Yes, they are what you asked to install | Out of scope: a malicious `SKILL.md` is a content-review problem, not an installer problem |
| A local attacker who can write `TARGET_DIR` *while a run is in progress* | Out of scope | Such an attacker could plant skills directly. The checks reduce but don't eliminate check-then-use races |

## Guarantees

Each guarantee below has an automated test in `tests/test_install_security.py`,
`tests/test_install_integrity.py`, `tests/test_install_signals.py` or
`tests/test_install_platform.py`, except where marked *(not automated)*.

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
2. **Local edits are preserved, in every file of a managed folder.** The manifest
   records the sha256 of every file in each installed skill folder (format 3), not
   only `SKILL.md`.
   - On update, a managed skill whose files differ from the record in any way (a file
     changed, deleted, renamed or added, hidden files included) is kept, with a warning
     that names the files (`changed: …; added: …; removed: …`). `--force --skill SLUG`
     overwrites just that skill; `--force` alone overwrites all of them.
   - `uninstall.sh` and `install.sh --prune` likewise keep such a skill unless you pass
     `--force` (optionally narrowed with `--skill`).
   - A missing `SKILL.md` counts as modified, and so does a `SKILL.md` that is now a
     directory.
   - A kept skill keeps its install-time record, so the edit is still detected on the
     next run.
   - *Limitation:* only files are recorded. An empty directory you add inside a managed
     folder is not detected, and is removed along with the folder (it holds no data).
   - *Entries from older manifests* (format 1 or 2) record `SKILL.md` at most. Such an
     entry counts as unmodified only if `SKILL.md` matches and every other file in the
     folder is byte-identical to the source's copy (`install.sh`). `uninstall.sh` has no
     source to compare with, so it keeps such a folder if it holds any file besides
     `SKILL.md`. See [Manifest format](#manifest-format).
3. **Folders the installer did not create are never touched** unless you pass
   `--force`. With `--force` the folder is replaced and from then on tracked as
   managed. Files and folders at the target that are not skill folders (a `README`, a
   foreign skill) are never modified. A same-named folder the installer did not create
   is skipped before anything below it is looked at: its files are not read or hashed,
   and a link or odd name inside it does not fail the run. With `--force`, such a folder
   is walked first, and if it holds a symlink/junction, a non-regular file or a name
   with a control character it is refused, not replaced
   (`TestPreExistingContent::test_unsafe_names_in_a_folder_we_did_not_install_are_not_examined`).
4. **Skills removed upstream stay tracked.** If a managed skill is no longer in the
   source, it is left installed, kept in the manifest, and reported as an `orphan`.
   `--prune` removes it if it is unmodified; `--prune --force` removes it in any
   case. An orphan that is already gone from disk is simply dropped from the
   manifest.
5. **Each skill is swapped in with two renames, with rollback.** The install runs in three stages:
   - **Stage.** All selected skills are first copied into a staging area inside
     `TARGET_DIR` (the lock directory), so on the same filesystem by construction. If
     that copy fails, nothing in the target has changed yet.
   - **Commit.** Each skill is then committed with two renames: the old folder is
     moved aside into the lock directory, then the new folder is moved into place.
     If either rename fails, the old copy is moved back.
   - **Interrupted commit.** Rollback is armed *before* the first rename, and it
     decides what to do from what is on disk, not from how far the code got. This
     matters because bash runs a signal trap only after the running `mv` exits:
     - If the new copy has landed, its hashes are recorded.
     - If the old copy was moved aside but not replaced, it is moved back.
     - If something else now occupies `TARGET_DIR/<slug>` (an explicit failure, or a
       folder that appeared mid-run), the old copy is *not* deleted. The lock
       directory is kept with the old copy in `.ml-ai-skills.lock/old/<slug>`, the
       error names that path, and the next run exits 3 until you inspect it.

     Further HUP/INT/TERM signals are ignored while the cleanup runs: a second signal
     while the cleanup renames the manifest into place leaves the exit status, the
     manifest and the lock release unchanged
     (`TestInterruptedCommit::test_sigterm_during_cleanup_is_ignored` for TERM,
     `TestSignalsDuringCommit::test_second_signal_during_cleanup_is_ignored` for INT
     and HUP).
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
   - *Signals:* SIGINT (Ctrl-C), SIGHUP and SIGTERM all go through the same trap and
     exit 130, 129 and 143. Tested, for each of the three signals:
     - delivered while the old copy is being moved aside, and right after the new copy
       lands (`TestInterruptedCommit` for TERM, `TestSignalsDuringCommit` for INT and
       HUP, via a `PATH` wrapper around `mv`);
     - delivered from outside, to the script only, while it waits for a child that a
       `PATH` shim holds: during staging (`cp`), during a commit, and during
       `uninstall.sh`'s move (`TestSignalsFromOutside`). bash runs the trap only once
       that child exits, and it does not kill the child itself. The tests check that
       nothing is cleaned up while the child is still running (the child is alive and
       the lock is in place), then release the child; afterwards the script has rolled
       back, the manifest matches the disk, the lock and staging directory are gone, and
       no child is left;
     - SIGINT to the whole process group, as a terminal's Ctrl-C sends it
       (`test_ctrl_c_to_the_process_group_while_staging`). Here the child is never
       released: it must die from the signal itself, and the script must exit 130
       within 30 s (the shim's own bound is 60 s). This is the only test of a child
       being terminated by the signal. It is skipped on Windows, where a native process
       cannot signal an MSYS process group.

     The signals are sent only to the PID of the script the test started. The lock
     is deliberately kept in one case: an old copy that could not be restored (see
     guarantee 5).
   - *A regular file at the lock path* is not a lock that any run could have
     created. It is reported as an error (exit 1), not as "lock held" (exit 3).
8. **Links and odd entries are never followed or copied.** The following are refused,
   with a message and exit code 1:
   - a source skill folder that *is* a symlink, or that contains a symlink or
     junction, a non-regular file, or a name with a control character (a TAB or
     newline would break the manifest's line format)
   - a destination `TARGET_DIR/<slug>` that is a symlink or junction, or a managed
     folder that contains one of the above
   - a symlinked manifest, or a manifest path that is not a regular file
   - a symlinked lock path

   The staged copy is re-checked before commit. `TARGET_DIR` itself *may* be a
   symlink, for example a dotfiles setup. It is resolved once, and the run then
   operates inside the resolved directory; the link itself is left in place
   (`TestLinks::test_full_cycle_through_a_linked_target_dir`, run with an NTFS
   junction on the Windows test host).
9. **Nothing is written outside `TARGET_DIR`.** The only exception is
   `mkdir -p TARGET_DIR` itself. All paths are `TARGET_DIR/<validated slug>`, the
   manifest, or the lock directory. The per-file paths in the manifest are only ever
   compared with what is on disk, never used as paths, so a hostile line
   (`alpha<TAB><hash><TAB>../../victim`) can at most make `alpha` look modified. The
   scripts never use `eval`, and they quote every expansion, so paths containing
   spaces, quotes or `$`, and skill files with such names, work. A `--target`,
   `--project` or `--source` value that starts with `-` is a relative path (`./-x`),
   never a `cd`/`mkdir` option: `--target -` installs into `./-`, not into
   `$OLDPWD`. The scripts and `bin/ml-ai-skills` unset `CDPATH`, so an exported
   `CDPATH` cannot redirect a relative path either
   (`TestOptionLikePaths::test_exported_cdpath_is_ignored`,
   `TestWrapper::test_exported_cdpath_does_not_redirect_the_wrapper`).
10. **`--dry-run` writes nothing.** That includes the lock, the staging area, the
    manifest and the target directory. The tests compare a full snapshot of the
    temp tree before and after a dry run; a dry run reports the same keep/refuse
    decisions as the real run.
11. **Refusal by design:**
    - `TARGET_DIR` equal to the source checkout is refused.
    - A target path that is a regular file is refused.
    - An unknown future manifest format (`# format: 4`) is refused rather than
      guessed at.
    - If none of `sha256sum`, `shasum` and `openssl` exists, the scripts refuse to run
      instead of silently dropping guarantee 2, and so do they without `stat`. Both
      refusals happen before anything is written, dry run included
      (`TestRequiredTools`, with a `PATH` that holds only shims for an allowlist of
      tools). The `shasum` and `openssl` fallbacks are tested the same way.
12. **Mount points are refused, not moved.** Before anything is moved aside or
    removed, the scripts check every folder to be replaced or removed in two ways:
    - **Device ids.** One `stat` call reads the device id of `TARGET_DIR`, the staging
      area, every such folder, and every directory inside them. A folder on another
      filesystem (it is a mount point, or contains one) is refused with exit 1 and left
      untouched; the rest of the run completes. `mv` could not rename it in one step
      (it may fall back to copy+delete), and deleting the old copy with the lock
      directory would delete files on that filesystem.
    - **The mount table**, where `/proc/self/mountinfo` is readable (Linux; Git Bash
      and Cygwin have one too). A folder that is, or has below it, a mount point listed
      there is refused the same way. This catches a *bind mount* of the same
      filesystem, which has the same device id and is invisible to the first check:
      without it, `--force` renamed such a folder into the lock directory and the
      cleanup deleted the bind-mounted files, which live outside `TARGET_DIR` (observed
      with the previous scripts under WSL 2; the device-id check alone did not stop it).
    - If the staging area were on another filesystem, or the device ids can't be read,
      the run stops before changing anything.
    - A fresh install moves nothing aside, so it needs neither check.
    - *Limitation:* without a mount table (macOS, the BSDs) only device ids are
      checked, so a same-device mount there (for example a BSD `nullfs` mount, if it
      reports the lower filesystem's device id) would not be detected. Not tested.

    Tested with a `stat` shim in GNU and BSD styles and with extra mount-table lines,
    including the `\040`/`\134` escapes (`TestMountPoints`), and on Linux, in an
    unprivileged mount namespace, with a real tmpfs and a real bind mount inside a
    skill folder (`test_real_tmpfs_mount_point_is_refused`,
    `test_real_bind_mount_on_the_same_filesystem_is_refused`; skipped where
    `unshare -rm` is not allowed, e.g. GitHub's Ubuntu 24.04 runners restrict
    unprivileged user namespaces).

## Manifest format

The manifest is `TARGET_DIR/.ml-ai-skills-manifest`. It is plain text with LF line
endings. Its lines are sorted by slug, and within a skill by file path, with the
skill line first.

```text
# ml-ai-skills manifest — managed by install.sh/uninstall.sh, do not hand-edit
# format: 3
# source: /home/me/ml-ai-skills
# updated_at: 2026-10-29T19:48:19Z
alpha<TAB>3965929b60ad8defccac73ad2aacda5522e54f0c8b2230a502c6d1cb0b8e2144
alpha<TAB>3965929b60ad8defccac73ad2aacda5522e54f0c8b2230a502c6d1cb0b8e2144<TAB>SKILL.md
alpha<TAB>9f86d081884c7d659a2feaa0c55ad015a3bf4f1b2b0b822cd15d6c15b0f00a08<TAB>references/notes.md
beta<TAB>-
```

- **Format 3** (current): each skill has a *skill line*, `slug<TAB>sha256 of SKILL.md`,
  and one *file line* per file in its folder, `slug<TAB>sha256<TAB>relative/path`
  (`/`-separated, hidden files included). The path is everything after the second TAB,
  so a name may contain spaces, `#`, `=` and non-ASCII characters; names with control
  characters are refused at install time. A skill line without file lines is an entry
  carried over from format 1 or 2 (see guarantee 2). A file line whose path is not a
  clean relative path, whose hash is malformed, that names a slug without a skill line,
  or that repeats a file of the same skill is ignored with a warning; the first line
  for a file wins.
- **Format 2** (previous): each line is `slug<TAB>sha256 of the installed SKILL.md`.
  A hash of `-` means "unknown". This is only kept for a format-1 entry that could
  not be verified. Still read. On the next `install.sh` run the manifest becomes
  format 3: every skill that run installs or updates gets file lines. A format-2 entry
  counts as unmodified when `SKILL.md` matches its hash and every other file in the
  folder equals the source's copy (in this library every skill currently has only a
  `SKILL.md`, so that is the common case). Otherwise it is kept, the warning names the
  files that differ, and it keeps its format-2 skill line until you
  `--force --skill SLUG` it. *(Known gap: unlike `SKILL.md` in format 1, other files
  are not checked against the source's git history, so an unedited extra file from an
  older release is kept as possibly edited. This errs on the safe side.)*
- **Format 1** (legacy, no `# format:` line): each line is a bare slug. It is still
  read, and on the next `install.sh` run the manifest is upgraded to format 3. A
  format-1 entry has no install-time hash, so `install.sh` tries to prove each copy's
  `SKILL.md` unedited (other files are then checked as for format 2). If it can, it
  records the copy's current sha256 and treats it as a normal managed skill:
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
- `uninstall.sh` will not remove a skill with an unknown hash, or a format-1/2 entry
  whose folder holds files besides `SKILL.md`, unless you pass `--force` (narrow it
  with `--skill`). Its message tells a still-format-1 manifest ("run `install.sh`
  once") apart from a `-` that `install.sh` already failed to prove ("inspect it,
  then `--force --skill SLUG`"). `list_skills.py` makes the same distinction in its
  `[UNVERIFIED]` note. To keep that distinction true, `uninstall.sh` never upgrades
  the format. It rewrites the manifest only when it removed or dropped an entry, and
  it rewrites it in the format it read: format 1 as format 1 (no `# format:` line,
  bare slugs), format 2 as format 2 (no file lines). Only `install.sh` upgrades.
  `install.sh`'s own "keep" message gives the reason per skill: it mentions the
  source's git history only for a copy that comparison actually covered, and a
  folder with no `SKILL.md` is reported as such rather than blamed on git
  (`TestManifestV1Upgrade::test_keep_reason_is_per_skill`).
- **All formats:** lines starting with `#` are metadata. Blank lines are ignored. If
  a slug appears more than once, the first skill line wins.
- **Unknown formats:** a `# format:` value other than 1, 2 or 3 is an error. Upgrade
  the checkout rather than let an old script misread a newer manifest.
- **Going back to an older checkout:** scripts from before format 3 refuse a format-3
  manifest ("declares format 3"). Run the current `uninstall.sh` first, or keep using a
  checkout that understands format 3.

## Exit codes

| Code | install.sh / uninstall.sh |
|---|---|
| 0 | Success. Protective skips (local edits, folders the installer didn't create, orphans) are reported as `WARN:` lines and in the summary counts. They are not errors |
| 1 | Error. Possible causes: a bad source checkout, no manifest found for uninstall, an unsafe link, file type or file name, a mount point, an unexpected regular file at the lock path, an I/O failure, an unknown manifest format, no sha256 tool, or no `stat`. Every skill that could be processed was processed, and the manifest matches the disk. In the one case where an old copy could not be restored, the lock directory is kept to preserve it (guarantee 5) |
| 2 | Usage error: an unknown flag, a missing flag argument, or an invalid `--skill` slug (either script). Nothing is changed |
| 3 | Another run holds the lock. Nothing is changed |
| 129 / 130 / 143 | Interrupted by SIGHUP / SIGINT / SIGTERM, after the rollback and cleanup of guarantee 5 |

`list_skills.py --target` exits 1 when there is no manifest, the manifest is a
symlink, its format is unknown, or it contains a NUL byte. Otherwise it exits 0. The
skills it flags appear as `[MODIFIED]` (naming the changed, added or removed files),
`[UNVERIFIED]`, `[MISSING]` and `[UNSAFE]`, followed by a count line. `[UNSAFE]`
covers everything the scripts refuse: a `TARGET_DIR/<slug>` that is a symlink/junction
or not a directory, or a folder containing a symlink/junction, a non-regular file or a
name with a control character, at any depth. Junctions are detected on every supported
Python: `os.path.isjunction` on 3.12+, the reparse tag from `os.lstat` before that
(tested with a real `mklink /J` junction on Python 3.10.22, 3.12 and 3.13 on Windows).

**Output encoding.** On an interactive console, `list_skills.py` keeps the encoding
Python chose for it: on Windows, Python 3.6+ writes to a console through the Unicode
console API (PEP 528), so no code page is involved; on POSIX it is the locale's encoding.
*(The console case was not tested; the tests run without a console.)* Written to a pipe or a file (which includes Git Bash's terminal on Windows), it writes
UTF-8, unless `PYTHONIOENCODING` names an encoding, which is then honoured. Characters
the chosen encoding can't represent are printed as backslash escapes (`\u65e5` for 日)
instead of failing (`tests/test_list_skills.py::TestCli`).

## Test-only fault injection

`ML_AI_SKILLS_FAULT_INJECT` makes `install.sh` fail at a chosen point, so the rollback
paths can be exercised the same way on every OS:

- `stage`: makes the staging copy fail.
- `commit:<slug>`: makes the commit of `<slug>` fail *after* its old copy has been
  moved aside, which is the worst moment to fail.

The variable only ever causes a failure. It cannot redirect any path. Don't set it
outside tests.

`ML_AI_SKILLS_TEST_MOUNTINFO` names a file of extra mount-table lines (the
`/proc/self/mountinfo` format), read *in addition to* the real mount table, so a
bind-mount refusal can be tested on hosts where no mount can be created (guarantee 12).
It can only add refusals: it cannot hide a real mount point or redirect any path.

Signal timing, "a folder appeared mid-commit", missing tools and device ids are
tested without any hook in the scripts, by putting shims first on `PATH`:

- a wrapper `mv` that does the real move, then signals the calling script or
  recreates the folder it just moved away (`TestInterruptedCommit`,
  `TestSignalsDuringCommit`);
- a shim that holds `cp` or `mv` until the test releases it, so the test can signal
  the script from outside (`TestSignalsFromOutside`);
- a `PATH` of `exec`-shims for an explicit list of tools, to remove `sha256sum`,
  `shasum`, `openssl` or `stat` (`TestRequiredTools`);
- a `stat` that reports another device id for chosen paths, or behaves like BSD
  `stat` (`TestMountPoints`).

`scripts/list_skills.py` parses the manifest with the same grammar as the scripts:
it strips only ASCII whitespace (C-locale `[:space:]`, so U+00A0 is *not* stripped)
and compares `# format:` as a string, so `02` is refused. A NUL byte anywhere is
refused, as in the scripts. `tests/test_list_skills.py` cross-checks the parsing, the
`[UNSAFE]` set, NUL refusal and the per-file `[MODIFIED]` decisions (and their file
lists) against `uninstall.sh --dry-run`.

## Platforms

**Tested:**

- **Windows 11 with Git for Windows (MSYS2), `bash` 5.2.37**, Python 3.13 (all
  installer suites) and Python 3.10.22 / 3.12.10 (`tests/test_list_skills.py`). Real
  symlinks could not be created without privilege (WinError 1314), so the
  directory-link tests ran with NTFS junctions, which Git Bash reports as symlinks.
  The file-symlink tests were skipped (two in `tests/test_install_security.py`, and
  `TestWrapper::test_symlinked_wrapper_uses_the_real_checkout` in
  `tests/test_install.py`), as were the control-character and process-group tests
  (Windows has neither) and the two real-mount tests (Linux only).
  On 2026-10-30 `tests/test_install*.py` ran 116 tests with 7 skipped. One test failed
  only because the disk was full (`No space left on device` from `sort`; the installer
  reported the failed manifest write). Re-run on its own, it passed.
  `tests/test_list_skills.py` ran 32 tests, OK, 1 skipped.
- **Linux: Ubuntu under WSL 2** (kernel 6.18, GNU bash 5.3.9, GNU coreutils, Python
  3.14.4), on 2026-10-30, from a copy of the working tree on the WSL filesystem:
  `tests/test_install*.py` ran 116 tests, OK, none skipped (so the file-symlink,
  control-character, process-group Ctrl-C, real tmpfs mount and real bind-mount tests
  all ran), and
  `tests/test_list_skills.py` 32 tests, OK, 2 skipped (the NTFS junction tests). This
  is a WSL 2 VM, not a GitHub runner, and WSL's Windows-drive mounts (`/mnt/c`) were
  not used as a target.

**Not run by us:**

- **GitHub's Linux runner.** CI (`ubuntu-latest`) runs the installer tests, but no
  result from that run is recorded here.
- **macOS, and bash 3.2 in particular.** The scripts avoid bash-4-only features: no
  associative arrays, no `mapfile`, no `${x,,}`, no namerefs, and empty arrays are
  guarded under `set -u`; `stat -c` always has a `stat -f` fallback.
  `tests/test_install_platform.py::TestPortability` checks this statically, with
  positive controls. But nothing has been executed on bash 3.2 or with BSD
  `cp`/`mv`/`shasum`/`stat`; the BSD `stat` path ran only through a shim.
- **Network filesystems.**
- **Python 3.11** for `list_skills.py`: no 3.11 interpreter was available on the test host
  (3.10 and 3.12 bracket it, and the junction fallback is the same code on both).

**Performance note:** process creation was about 0.3 s per fork on the Windows test
host, so the scripts batch their hashing, device-id checks and staging into a few
processes. A full install of the library still does about two `mv` calls per skill,
because each rename is a separate step that has to be able to roll back.
