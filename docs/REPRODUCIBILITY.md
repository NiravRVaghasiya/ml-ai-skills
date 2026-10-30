# Reproducibility policy

Applies to any skill whose Workflow trains/evaluates a model. Be precise about
what reproducibility claim is actually being made — overclaiming here is one of
the most common ways ML guidance misleads.

## What a fixed seed does and does not guarantee

Setting `random_state=42` / `torch.manual_seed(0)` etc. makes a **single run on
the same code, same library versions, same hardware, same backend** reproduce
identical results. It does **not** guarantee reproducibility:

- **across different library versions** — an algorithm's internal random-number
  consumption order can change between releases even with the same public API.
- **across different hardware** (CPU vs. GPU, different GPU models) — floating-
  point operations are not strictly associative, and parallel reduction order
  differs by device/kernel, so summed/averaged results can differ in the last
  few bits, which compounds over many training steps.
- **across different backend configurations** — e.g. cuDNN's non-deterministic
  algorithms (enabled by default in many setups for speed) pick different
  kernels run-to-run unless deterministic mode is explicitly enabled (which
  usually costs performance).
- **for any inherently distributed/async computation** — multi-worker
  `DataLoader`s, multi-GPU data-parallel training, and asynchronous data
  pipelines can process examples in a different order run-to-run even with a
  fixed seed, unless you also fix worker seeding and disable relevant async paths.

**Precise claim to make:** "seeding makes THIS run reproducible on THIS
environment," never "seeding guarantees reproducibility."

The same applies to hosted LLMs, with one more caveat: a sampling setting such as
`temperature=0` does not guarantee bit-for-bit identical outputs, and on some current
APIs and SDKs it is not available at all. The `llm-evaluation` skill says the current
Claude Messages API rejects sampling parameters. The repo has runtime-tested only that
anthropic SDK 2.3.0 raises `TypeError` for `temperature=`; see
`anthropic-sdk-rejects-temperature` in `docs/claims.json`. Treat run-to-run variation as
something to measure: pin the model ID, log the request parameters, and repeat calls to
estimate the noise floor. `evals/llm-evaluation/temperature_determinism_misconception.yaml`
is the eval case built around this misconception.

## What to specify, per skill, when reproducibility matters

- **Random seeds** — set them, and set them for every source of randomness
  (numpy, the framework's own RNG, Python's `random`, data-loader workers),
  not just the model's.
- **Environment specification** — package name + version for anything whose
  behavior the workflow depends on (this is exactly what `version_constraints`
  in a skill's frontmatter is for — see docs/SKILL-SPEC.md).
- **Dataset version** — a hash, a fixed snapshot/date, or a pinned dataset
  release tag; "the current data" is not a reproducibility unit.
- **Model version** — for anything wrapping a hosted model behind a name string
  (e.g. a vendor API model ID), note that the string may point to a
  continuously-updated model unless it's a dated/pinned snapshot — see
  `scripts/check_freshness.py`'s pinned-snapshot pattern checks.
- **Evaluation dataset version** — same as dataset version, tracked separately,
  since eval sets and training sets drift independently.
- **Hardware assumptions** — note when a workflow assumes a GPU, a minimum VRAM
  budget, or specific accelerator features (e.g. bf16 support) rather than
  silently failing on CPU-only or older hardware.
- **Deterministic vs. nondeterministic behavior** — say which regime the
  workflow is in and why (e.g. "training uses non-deterministic cuDNN kernels
  for speed; expect run-to-run metric variance of roughly X" — state this as an
  observation to make per-project, not a universal number).

## What this repo does NOT claim

No skill in this repository claims **complete** reproducibility across
environments, and none should. Where a skill's code sets a seed, that is a
best-effort reproducibility aid for a single environment, not a guarantee.

## Dependency locks

This section is about the repository's own test environments, not about the skills.

**Files.** `requirements-dev.txt` and `requirements-examples.txt` hold the human-edited
direct pins (the files Dependabot bumps). `constraints/dev-linux-cp313.txt` and
`constraints/examples-linux-cp313.txt` are their full resolutions: every package pip installs
for the matching file, transitive ones included, at an exact version, for Linux x86_64 with
CPython 3.13 (the `ci.yml` runners). Every `pip install` of those files in `ci.yml` passes
the lock with `-c`, so a new upstream release of an unpinned transitive package cannot change
what CI installs. `release.yml` installs ruff and shellcheck by name without the lock.

**What `ci.yml`'s `deps` job checks** on every run: each requirements file resolves together
with its lock (`pip install --dry-run --ignore-installed --report`), and pip would install
exactly what the lock lists, nothing more, nothing less, no other version. A pin bumped in a
requirements file without regenerating its lock fails there with `ResolutionImpossible`.
When a requirements file or lock differs from the base commit (or on a manual run), the job
also resolves each requirements file without its lock, so the whole file must resolve on its
own, and uploads locks regenerated from that resolution as the `dependency-locks` artifact.
That second step does not fail on drift: upstream releases change a fresh resolution over
time, and the committed lock is what CI installs.

**Regenerating a lock** after editing a requirements file: download the `dependency-locks`
artifact from the PR's `deps` job and commit its files to `constraints/`. Locally, on Linux
x86_64 with CPython 3.13, run the dry-run and then the generator that job uses (the Python
heredoc in its "When requirements changed" step):

```bash
python -m pip install --dry-run --ignore-installed --quiet --report report.json -r requirements-examples.txt
python - <<'EOF' > gen_lock.py
import sys; sys.path.insert(0, "scripts"); import audit_workflows as aw
steps = aw.parse_yaml(open(".github/workflows/ci.yml", encoding="utf-8").read())["jobs"]["deps"]["steps"]
run = next(str(s["run"]) for s in steps if str(s.get("name", "")).startswith("When requirements changed"))
print(run.split("<<'PY'\n", 1)[1].split("\nPY\n", 1)[0])
EOF
python gen_lock.py report.json requirements-examples.txt > constraints/examples-linux-cp313.txt
```

The same with `requirements-dev.txt` and `constraints/dev-linux-cp313.txt`.
`tests/test_audit_workflows.py` (`TestDependencyLocks`) checks offline that each lock is
sorted, carries the generated header, and agrees with every direct pin.

**What the locks are not.**

- **Not cross-platform.** On Windows pip resolves a different set (for example `pywin32`,
  `colorama` and `waitress` instead of `gunicorn` and `uvloop`), so use the locks only on
  Linux x86_64 with CPython 3.13; elsewhere install the requirements file alone.
- **Not hashed.** There are no `--hash` lines and CI does not use `--require-hashes`. Three
  example packages are sdist-only and are built at install time (lime 0.2.0.1, rouge-score
  0.1.2, seqeval 1.2.2), torch and torchvision come from the PyTorch index, and whether pip's
  hash-checking mode accepts this layout (hashes in a constraints file, built sdists) has not
  been tried. Integrity therefore rests on exact versions and TLS to the indexes.
- **Not a resolution from Windows.** A cross-platform dry run from Windows
  (`--platform manylinux_2_28_x86_64 --python-version 3.13 --only-binary=:all:`) cannot
  resolve the file, because of the sdist-only packages
  (`No matching distribution found for lime==0.2.0.1`). The committed locks were resolved
  natively on Linux instead (below).

**Observed** (2026-10-29, not on a GitHub runner). The locks were resolved on Ubuntu 26.04,
whose glibc is newer than `ubuntu-latest`'s; a pinned package that ships only newer
manylinux wheels would build from source, or fail, on the runner. That is unverified on
`ubuntu-latest`: the first run of the `deps` job there is the real check.

- On WSL Ubuntu 26.04 with CPython 3.13.18 (python-build-standalone 20261026, sha256
  checked against the release's `SHA256SUMS`) and pip 26.2.2, a fresh venv ran
  `pip install -r requirements-examples.txt` (commit `51743b0`'s file) to exit 0 in about
  30 minutes, building lime, rouge-score and seqeval from source. pip resolved
  `wrapt==2.5.2rc1`: formulaic requires `wrapt>=1.17.0rc1` on Python 3.13, and a pre-release
  in a specifier lets pip choose pre-releases. `requirements-examples.txt` now pins
  `wrapt==2.5.1`.
- With that pin, the dry-run resolutions behind the committed locks (226 packages for the
  examples file, 37 for the dev file) exited 0 on the same machine, and the `deps` job's
  completeness check passed against them ("226 packages resolved, 226 locked"; "37 packages
  resolved, 37 locked"). Bumping one pin (`numpy==2.5.3`) without regenerating the lock made
  the constrained dry run fail with `ResolutionImpossible`, as intended.
- In that Linux venv, the `examples` job's own commands, run with
  `pip install -r requirements-examples.txt -c constraints/examples-linux-cp313.txt`
  (exit 0) against commit `51743b0`'s tests and `ML_AI_SKILLS_REQUIRE_EXAMPLE_DEPS=1`, gave
  `tests/examples`: 351 tests OK, none skipped (858 s), and `check_example_coverage.py`:
  "every runnable python block executed: 145 block(s) across 26 skill(s)". The runtime
  freshness tests (`test_check_freshness.py`) ran 30 tests OK there.
- On Windows 11 (CPython 3.13.2, pip 26.2.2, fresh venv) the same file resolved: pip
  collected every package and started installing. The install then failed with
  `[Errno 28] No space left on device` after about 35 minutes because the shared machine's
  disk was full, so no complete Windows install of the current pins was observed.
- `pip-audit` 2.10.1 (`--disable-pip --no-deps`, PyPI advisory data, `+cpu` stripped) reported
  "No known vulnerabilities found" for the direct pins of both files and for both locks. That
  is a point-in-time result: a new advisory for any locked package fails the `security` job
  until the lock is regenerated with a fixed version.

## Release archive

This section is about the repository's own release tarball (`scripts/build_release.py`,
`docs/RELEASE.md`), not about the skills.

**The guarantee.** For the same input files, `VERSION` and epoch, the builder produces the
same **uncompressed tar stream** and therefore the same `RELEASE-MANIFEST.sha256`. That is the
reproducibility contract. The `.tar.gz` bytes (and the hash in `SHA256SUMS`) are the same only
when the deflate implementation produces the same output for the same input and settings,
which holds for a given zlib build but is not guaranteed across zlib versions or
implementations. If two builds' tarball hashes differ, compare `gzip -dc <tarball> | sha256sum`
or the manifests before concluding the content differs.

**What is fixed**, so that nothing about the build machine leaks in:

- **Input set:** the files git would commit (a filtered walk outside a git checkout), with
  CRLF text refused; `.gitattributes` makes every text file LF on checkout.
- **Order and paths:** entries sorted by posix path, `/` separators, one top-level
  `ml-ai-skills-<VERSION>/` directory, explicit directory entries.
- **Metadata:** `mtime` = `SOURCE_DATE_EPOCH` (env var or `--source-date-epoch`; default the
  HEAD commit time, which `release.yml` sets explicitly to the tag commit's time); uid/gid 0;
  empty user/group names; mode 0755 for directories and for files whose content starts with
  `#!`, else 0644. The mode comes from the bytes, not the filesystem or git, so a checkout
  without exec bits (Windows) and a rebuild from an extracted tarball give the same archive;
  `tests/test_release_gitattributes.py` checks that git's index modes agree.
- **Format:** `tarfile.PAX_FORMAT` (POSIX.1-2001), UTF-8 names. Short ASCII names produce
  plain ustar headers with no pax records.
- **Compression:** written by hand, not by the `gzip` module: header `1f 8b 08 00`, mtime 0,
  XFL 2, OS 255, no file name; raw deflate with level 9, `memLevel` 8, default strategy; CRC-32
  and size trailer.

**What was measured** (2026-10-29, one Windows 11 machine, so this says nothing about other
operating systems): the real archive of a fresh clone of commit `51743b0` with this change's
`scripts/build_release.py` (sha256 `8e552610...`) copied in, built with the same
`SOURCE_DATE_EPOCH` (1700000000) by each available interpreter:

| Interpreter | zlib (`ZLIB_RUNTIME_VERSION`) | tar stream sha256 (prefix) | .tar.gz sha256 (prefix) |
|---|---|---|---|
| CPython 3.13.2 | 1.3.1 | `5061ad0253b33087` | `2bf6a2d7ade479a1` |
| CPython 3.12.10 | 1.3.1 | `5061ad0253b33087` | `2bf6a2d7ade479a1` |
| CPython 3.10.22 (uv build) | 1.3.2 | `5061ad0253b33087` | `2bf6a2d7ade479a1` |

Repeated later the same day, in memory (the disk was full), on the shared working tree
(1,277 classified files, `build_release.py` sha256 `054bbb5d1575...`, same epoch): CPython
3.13.2 (zlib 1.3.1), 3.12.10 (zlib 1.3.1) and 3.10.22 (zlib 1.3.2) all gave tar stream
`0aa6d4da4f3d5c47` and `.tar.gz` `ea9af0daa8fb5b9c`. These hashes describe an uncommitted
tree and are not a release; they only show that the three interpreters agreed again.

CPython 3.11 was not available on that machine. Re-compressing the same tar stream with
zlib-ng 2.2.5 (the `zlib-ng` PyPI package, same level, window and memLevel) gave different
deflate bytes (1,345,558 vs 1,343,686 bytes), which is why the `.tar.gz` hash is not part of the
contract. `tests/test_build_release.py` pins the tar-stream hash of a small fixture, so a
Python version that changed tar output would fail that test; rebuilding from an extracted
archive is also tested to give identical bytes.

**Not shown:** identical output on Linux or macOS, on GitHub's runners, or with Python 3.11
or 3.14+. `release.yml` compares two builds on one runner and a rebuild on a second runner
of the same image; that has not been observed running.
