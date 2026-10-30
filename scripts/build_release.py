#!/usr/bin/env python3
"""Build (and verify) the reproducible release tarball: a self-contained developer distribution.

Output (default ``dist/``):
    ml-ai-skills-<VERSION>.tar.gz   the distribution, under one top-level directory
    SHA256SUMS                      ``<sha256>  <file>`` for the tarball (sha256sum -c format)

Inside the tarball, ``ml-ai-skills-<VERSION>/RELEASE-MANIFEST.sha256`` lists the
SHA-256 of every other file, so an extracted tree can be checked with
``sha256sum -c RELEASE-MANIFEST.sha256`` from that directory, or with
``python scripts/build_release.py --verify-archive <tarball>`` (no sha256sum needed).

What ships (contract A, docs/RELEASE.md "What ships"): every file git would commit
(tracked + untracked-but-not-ignored; a filtered walk outside a git checkout), so the
extracted tree can run the same validators, evals and unit tests as the repository.
Every candidate path must be classified: it is shipped if it is a skill folder or
matches SHIPPED_FILES / SHIPPED_TREES, dropped if it is untracked and matches the
EXCLUDED_* rules (caches, editor/agent state, build output, local logs), and anything
else is an error - including a *tracked* file that matches an EXCLUDED_* rule - so a
file is never silently shipped or silently missed. Symlinks, submodules and
non-portable path names (path_problem) are refused. release.yml additionally requires a
clean tree (--require-clean), which makes the file list exactly ``git ls-files``.

Before building, the generated artifacts (INDEX.md, README stats, plugins/, the
dependency graph, the JSON Schema, the contract lock) must match their canonical
sources: scripts/check_generated.py runs and any drift refuses the build
(--skip-generated-check skips it, for local iteration only; release.yml's verify job
never passes it).

Reproducibility (docs/REPRODUCIBILITY.md, "Release archive"): entries are sorted by
posix path; every entry gets mtime = SOURCE_DATE_EPOCH (env var or --source-date-epoch;
else the HEAD commit time), uid/gid 0, empty owner names, POSIX.1-2001 (pax) format,
and mode 0755 (directories, files whose content starts with "#!") or 0644 - derived from
the bytes, not the filesystem, so a checkout without exec bits builds the same archive.
The gzip layer is written by hand: fixed header (mtime 0, no file name, XFL 2, OS 255),
raw deflate at level 9 / memLevel 8 / default strategy. File bytes are copied verbatim;
a text file with CRLF line endings is an error (the repo is LF-only via .gitattributes).
The uncompressed tar stream and RELEASE-MANIFEST.sha256 depend only on the files, the
version and the epoch; the .tar.gz bytes additionally depend on the zlib build, which is
not guaranteed to be identical across zlib versions or implementations (e.g. zlib-ng).

Writes are atomic: the tarball and SHA256SUMS are written to temporary files in the
output directory, any existing SHA256SUMS is removed first, the tarball is renamed into
place and SHA256SUMS is renamed last, so a SHA256SUMS on disk always describes a
complete tarball. On any failure the partial outputs are removed.

Usage:
    python scripts/build_release.py [--out-dir dist] [--expect-version 1.2.3] [--require-clean]
    python scripts/build_release.py --list                     # print the files that would ship
    python scripts/build_release.py --verify-archive dist/ml-ai-skills-1.2.3.tar.gz [--extract-to DIR] [--no-sums]

Exit codes: 0 built/verified; 1 content/consistency error; 2 usage/environment error.
"""
from __future__ import annotations

import argparse
import contextlib
import fnmatch
import hashlib
import io
import os
import re
import shutil
import stat
import struct
import subprocess
import sys
import tarfile
import tempfile
import zlib
from pathlib import Path, PurePosixPath

sys.path.insert(0, str(Path(__file__).resolve().parent))
import skills_lib as sl  # noqa: E402

REPO_ROOT = sl.REPO_ROOT
PROJECT = "ml-ai-skills"
REQUIRED_FILES = ("LICENSE", "README.md", "CHANGELOG.md", "VERSION", "INDEX.md", "install.sh", "uninstall.sh",
                  "bin/ml-ai-skills", ".claude-plugin/marketplace.json", "schema/allowed_values.py",
                  "scripts/check_generated.py", "tests/testutil.py", "evals/routing/benchmark.json")
# Shipped: exact top-level files, and whole trees (a trailing "/"); skill folders are added
# from skills_lib.iter_skill_dirs. Everything a contributor needs to run the gates.
SHIPPED_FILES = frozenset({
    "LICENSE", "README.md", "CHANGELOG.md", "VERSION", "INDEX.md", "install.sh", "uninstall.sh",
    "SECURITY.md", "CONTRIBUTING.md", "CLAUDE.md", "requirements-dev.txt", "requirements-examples.txt",
    "ruff.toml", ".shellcheckrc", ".gitattributes", ".gitignore", ".coveragerc",
})
SHIPPED_TREES = ("plugins/", ".claude-plugin/", "bin/", "schema/", "docs/", "scripts/", "tests/", "evals/",
                 ".github/", "_TEMPLATE/", "constraints/")
TOP_FILES, TREES = SHIPPED_FILES, SHIPPED_TREES  # earlier names (tests/test_doc_consistency.py uses TOP_FILES)
# Excluded: any path with one of these components, top-level trees, file names and suffixes.
# Mirrors .gitignore for the walk used outside a git checkout (git applies .gitignore itself).
EXCLUDED_PARTS = frozenset({".git", "__pycache__", ".pytest_cache", ".ruff_cache", ".mypy_cache",
                            ".ipynb_checkpoints"})
EXCLUDED_TREES = (".claude/", ".cursor/", ".vscode/", ".idea/", "dist/", ".venv/", "venv/", "env/",
                  "node_modules/", "htmlcov/", "build/")
EXCLUDED_NAMES = frozenset({"validate_out.json", ".coverage", ".DS_Store", "Thumbs.db", "desktop.ini"})
EXCLUDED_GLOBS = ("*.pyc", "*.pyo", "*$py.class", "*.log", "*.tmp", "*.bak", "*.egg-info/*", ".coverage.*")
MANIFEST_NAME = "RELEASE-MANIFEST.sha256"
SUMS_NAME = "SHA256SUMS"
SEMVER_RE = re.compile(r"^\d+\.\d+\.\d+(?:-[0-9A-Za-z.-]+)?(?:\+[0-9A-Za-z.-]+)?$")
SAFE_PATH_RE = re.compile(r"^[A-Za-z0-9._@+-]+(?:/[A-Za-z0-9._@+-]+)*$")
# Windows device names (with or without an extension: "nul", "NUL.txt") cannot be ordinary files there.
WINDOWS_RESERVED = frozenset({"con", "prn", "aux", "nul", *(f"com{i}" for i in range(10)),
                              *(f"lpt{i}" for i in range(10))})
GZIP_LEVEL, GZIP_MEMLEVEL = 9, 8                  # gzip module defaults for compresslevel=9
GZIP_HEADER = b"\x1f\x8b\x08\x00" + b"\0\0\0\0" + b"\x02\xff"  # magic, deflate, no flags, mtime 0, XFL, OS
MAX_ARCHIVE_BYTES = 1 << 30                       # --verify-archive refuses more uncompressed data
GENERATED_CHECK_TIMEOUT = 1800


class ReleaseError(Exception):
    """A problem with the content being released (exit 1)."""


class EnvError(Exception):
    """A problem with the environment or arguments (exit 2)."""


def _git(root: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", str(root), "-c", "core.quotepath=off", *args],
                          capture_output=True, timeout=60)


def _is_repo_root(root: Path) -> bool:
    try:
        top = _git(root, "rev-parse", "--show-toplevel")
    except (OSError, subprocess.SubprocessError):
        return False
    return top.returncode == 0 and Path(top.stdout.decode().strip()).resolve() == root.resolve()


def _excluded(rel: str) -> bool:
    parts = rel.split("/")
    return (bool(EXCLUDED_PARTS & set(parts)) or rel.startswith(EXCLUDED_TREES) or parts[-1] in EXCLUDED_NAMES
            or any(fnmatch.fnmatchcase(rel, g) or fnmatch.fnmatchcase(parts[-1], g) for g in EXCLUDED_GLOBS))


def path_problem(rel: str) -> str | None:
    """Why `rel` (a posix path below the archive's top-level directory) is not portable, or None.

    Used by the builder and by --verify-archive, so an archive that verifies extracts the same
    tree on Linux, macOS and Windows: no characters outside SAFE_PATH_RE (that also rules out
    ':', which would write an NTFS alternate data stream), no '.'/'..' components, no
    component ending in '.' (Windows drops it, so 'a.md.' would overwrite 'a.md'), and no
    Windows device name ('nul' would vanish into the NUL device)."""
    if not SAFE_PATH_RE.match(rel):
        return f"characters outside {SAFE_PATH_RE.pattern}"
    for part in rel.split("/"):
        if part in (".", ".."):
            return f"{part!r} component"
        if part.endswith("."):
            return f"component {part!r} ends in '.'"
        if part.split(".", 1)[0].casefold() in WINDOWS_RESERVED:
            return f"component {part!r} is a Windows device name"
    return None


def candidate_files(root: Path) -> list[str]:
    """Repo-relative posix paths of every file git would commit (a filtered walk without git).

    In a git checkout the EXCLUDED_* rules apply only to untracked files: a tracked file that
    matches one is an error (untrack it or change the rule), never silently left out, so a
    clean-tree build ships exactly ``git ls-files``. Refuses symlinks, submodules and other
    non-regular entries rather than following them."""
    tracked: set[str] = set()
    if _is_repo_root(root):
        staged = _git(root, "ls-files", "-z", "--cached", "--stage")
        others = _git(root, "ls-files", "-z", "--others", "--exclude-standard")
        if staged.returncode or others.returncode:
            raise EnvError("git ls-files failed: " + (staged.stderr or others.stderr).decode("utf-8", "replace"))
        names = set()
        for entry in staged.stdout.decode("utf-8").split("\0"):
            if not entry:
                continue
            meta, name = entry.split("\t", 1)
            mode = meta.split(" ", 1)[0]
            if mode not in ("100644", "100755"):
                raise ReleaseError(f"{name}: git mode {mode} (symlink or submodule) cannot be released")
            tracked.add(name)
        names = tracked | {n for n in others.stdout.decode("utf-8").split("\0") if n}
        names = {n for n in names if (root / n).exists() or (root / n).is_symlink()}  # deleted, not yet staged
    else:
        names = set()
        for dirpath, dirnames, filenames in os.walk(root):
            here = Path(dirpath).relative_to(root).as_posix()
            dirnames[:] = sorted(d for d in dirnames if d not in EXCLUDED_PARTS
                                 and not (f"{d}/" if here == "." else f"{here}/{d}/").startswith(EXCLUDED_TREES))
            for d in dirnames:
                if (Path(dirpath) / d).is_symlink():
                    names.add((Path(dirpath) / d).relative_to(root).as_posix())
            for fn in filenames:
                names.add((Path(dirpath) / fn).relative_to(root).as_posix())
        names.discard(MANIFEST_NAME)  # an extracted release: regenerated, never copied
    out = []
    for n in sorted(names):
        if _excluded(n):
            if n in tracked:
                raise ReleaseError(f"{n}: tracked by git but matches an EXCLUDED_* rule in scripts/build_release.py; "
                                   "untrack it (git rm --cached) or narrow the rule - it is never dropped silently")
            continue
        p = root / n
        if p.is_symlink() or not stat.S_ISREG(p.lstat().st_mode):
            raise ReleaseError(f"{n}: not a regular file (symlinks and special files are not released)")
        problem = path_problem(n)
        if problem:
            raise ReleaseError(f"{n!r}: unsafe or non-portable path name ({problem})")
        out.append(n)
    return out


def classify(root: Path, rels: list[str]) -> tuple[list[str], list[str]]:
    """(shipped, unclassified) for already-filtered candidate paths."""
    skill_dirs = {p.name + "/" for p in sl.iter_skill_dirs(root)}
    shipped, unknown = [], []
    for rel in rels:
        top = rel.split("/", 1)[0] + "/"
        if rel in SHIPPED_FILES or rel.startswith(SHIPPED_TREES) or top in skill_dirs:
            shipped.append(rel)
        else:
            unknown.append(rel)
    return shipped, unknown


def select_files(root: Path, *, exclude_dir: Path | None = None) -> list[str]:
    rels = candidate_files(root)
    if exclude_dir is not None:  # the output directory, when it lives inside the tree
        with contextlib.suppress(ValueError):
            inside = exclude_dir.resolve().relative_to(root.resolve()).as_posix()
            if inside != ".":
                rels = [r for r in rels if not (r == inside or r.startswith(inside + "/"))]
    if MANIFEST_NAME in rels:
        raise ReleaseError(f"{MANIFEST_NAME} is generated by this script and must not exist in the repo")
    chosen, unknown = classify(root, rels)
    if unknown:
        raise ReleaseError("file(s) neither shipped nor excluded - add them to SHIPPED_FILES/SHIPPED_TREES or "
                           "an EXCLUDED_* rule in scripts/build_release.py (or .gitignore): " + ", ".join(unknown))
    _check_layout(chosen, [])  # e.g. README.md and readme.md from a case-sensitive checkout
    missing = [f for f in REQUIRED_FILES if f not in chosen]
    if missing:
        raise ReleaseError(f"required file(s) missing from the distribution: {', '.join(missing)}")
    if not sl.iter_skill_dirs(root):
        raise ReleaseError("no skill folders found")
    return chosen


def source_date_epoch(root: Path, override: int | None) -> int:
    if override is not None:
        return override
    env = os.environ.get("SOURCE_DATE_EPOCH")
    if env is not None:
        if not env.strip().isdigit():
            raise EnvError(f"SOURCE_DATE_EPOCH must be a non-negative integer, got {env!r}")
        return int(env)
    if _is_repo_root(root):
        out = _git(root, "log", "-1", "--format=%ct", "HEAD")
        if out.returncode == 0 and out.stdout.strip().isdigit():
            return int(out.stdout.strip())
    raise EnvError("no SOURCE_DATE_EPOCH, no --source-date-epoch and no git HEAD commit to take the time from")


def read_version(root: Path) -> str:
    version = (root / "VERSION").read_text(encoding="utf-8").strip()
    if not SEMVER_RE.match(version):
        raise ReleaseError(f"VERSION {version!r} is not a semantic version")
    return version


def check_generated(root: Path) -> None:
    """Refuse to release generated artifacts that do not match their canonical sources."""
    script = root / "scripts" / "check_generated.py"
    try:
        proc = subprocess.run([sys.executable, str(script), "--root", str(root)], capture_output=True, text=True,
                              encoding="utf-8", errors="replace", timeout=GENERATED_CHECK_TIMEOUT)
    except subprocess.TimeoutExpired as e:
        raise EnvError(f"scripts/check_generated.py timed out after {GENERATED_CHECK_TIMEOUT}s") from e
    except OSError as e:
        raise EnvError(f"cannot run scripts/check_generated.py: {e}") from e
    if proc.returncode == 2:
        raise EnvError("scripts/check_generated.py could not run:\n" + (proc.stdout + proc.stderr)[-4000:])
    if proc.returncode != 0:
        raise ReleaseError("generated artifacts do not match their sources (scripts/check_generated.py); "
                           "regenerate and commit them first:\n" + (proc.stdout + proc.stderr)[-4000:])


def _mode(data: bytes) -> int:
    return 0o755 if data.startswith(b"#!") else 0o644


def _info(name: str, epoch: int, *, size: int = 0, mode: int = 0o644, is_dir: bool = False) -> tarfile.TarInfo:
    ti = tarfile.TarInfo(name)
    ti.type = tarfile.DIRTYPE if is_dir else tarfile.REGTYPE
    ti.size = 0 if is_dir else size
    ti.mode = 0o755 if is_dir else mode
    ti.mtime = epoch
    ti.uid = ti.gid = 0
    ti.uname = ti.gname = ""
    return ti


def render_manifest(contents: dict[str, bytes]) -> bytes:
    return "".join(f"{hashlib.sha256(contents[r]).hexdigest()}  {r}\n" for r in sorted(contents)).encode("utf-8")


def build_tar_bytes(root: Path, files: list[str], version: str, epoch: int) -> bytes:
    prefix = f"{PROJECT}-{version}"
    contents: dict[str, bytes] = {}
    for rel in files:
        data = (root / rel).read_bytes()
        if b"\0" not in data and b"\r\n" in data:
            raise ReleaseError(f"{rel}: CRLF line endings (the repo is LF-only; fix the checkout or the file)")
        contents[rel] = data
    contents[MANIFEST_NAME] = render_manifest(contents)

    dirs = set()
    for rel in contents:
        parent = PurePosixPath(rel).parent
        while str(parent) != ".":
            dirs.add(str(parent))
            parent = parent.parent
    entries = sorted([(d, None) for d in dirs] + [(f, contents[f]) for f in contents], key=lambda e: e[0])

    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w", format=tarfile.PAX_FORMAT, encoding="utf-8") as tar:
        tar.addfile(_info(prefix, epoch, is_dir=True))
        for rel, data in entries:
            name = f"{prefix}/{rel}"
            if data is None:
                tar.addfile(_info(name, epoch, is_dir=True))
            else:
                tar.addfile(_info(name, epoch, size=len(data), mode=_mode(data)), io.BytesIO(data))
    return buf.getvalue()


def gzip_bytes(data: bytes, epoch: int | None = None) -> bytes:
    """RFC 1952 member with a fixed header; `epoch` is accepted for backwards compatibility and ignored."""
    comp = zlib.compressobj(GZIP_LEVEL, zlib.DEFLATED, -zlib.MAX_WBITS, GZIP_MEMLEVEL, zlib.Z_DEFAULT_STRATEGY)
    body = comp.compress(data) + comp.flush()
    return GZIP_HEADER + body + struct.pack("<II", zlib.crc32(data) & 0xFFFFFFFF, len(data) & 0xFFFFFFFF)


def _write_temp(out_dir: Path, data: bytes, temps: list[Path]) -> Path:
    fd, name = tempfile.mkstemp(prefix=".build_release-", suffix=".tmp", dir=out_dir)
    temps.append(Path(name))
    with os.fdopen(fd, "wb") as fh:
        fh.write(data)
        fh.flush()
        os.fsync(fh.fileno())
    return Path(name)


def write_outputs(out_dir: Path, name: str, archive: bytes) -> tuple[Path, Path]:
    """Atomically publish <name> and SHA256SUMS (renamed last); remove partial outputs on failure."""
    out_dir.mkdir(parents=True, exist_ok=True)
    tar_path, sums = out_dir / name, out_dir / SUMS_NAME
    temps: list[Path] = []
    published = False
    try:
        tmp_tar = _write_temp(out_dir, archive, temps)
        tmp_sums = _write_temp(out_dir, f"{hashlib.sha256(archive).hexdigest()}  {name}\n".encode("ascii"), temps)
        with contextlib.suppress(FileNotFoundError):
            sums.unlink()  # never leave a SHA256SUMS that describes a different tarball
        published = True
        os.replace(tmp_tar, tar_path)
        os.replace(tmp_sums, sums)
    except BaseException:
        for t in temps:
            with contextlib.suppress(OSError):
                t.unlink()
        if published:
            for p in (tar_path, sums):
                with contextlib.suppress(OSError):
                    p.unlink()
        raise
    return tar_path, sums


def build(root: Path, out_dir: Path, *, epoch: int | None = None, expect_version: str | None = None,
          require_clean: bool = False, verify_generated: bool = False) -> tuple[Path, Path]:
    """Build the archive. `verify_generated` (the CLI default) runs scripts/check_generated.py first."""
    root = root.resolve()
    if require_clean:
        if not _is_repo_root(root):
            raise EnvError("--require-clean needs a git checkout")
        status = _git(root, "status", "--porcelain", "--untracked-files=all").stdout.decode("utf-8", "replace")
        if status.strip():
            raise ReleaseError("working tree is not clean (release builds must match a commit):\n" + status)
    version = read_version(root)
    if expect_version is not None and expect_version.removeprefix("v") != version:
        raise ReleaseError(f"VERSION is {version} but the release tag/expected version is {expect_version}")
    when = source_date_epoch(root, epoch)
    files = select_files(root, exclude_dir=out_dir)
    if verify_generated:
        check_generated(root)
    archive = gzip_bytes(build_tar_bytes(root, files, version, when))
    return write_outputs(out_dir, f"{PROJECT}-{version}.tar.gz", archive)


# --------------------------------------------------------------------------- verification

def _check_member(m: tarfile.TarInfo, prefix: str | None) -> str:
    """The member's path below the top-level directory ('' for the directory itself)."""
    name = m.name
    if m.type not in (tarfile.REGTYPE, tarfile.AREGTYPE, tarfile.DIRTYPE):
        raise ReleaseError(f"{name!r}: only regular files and directories are allowed (links/devices refused)")
    if (name.startswith(("/", "\\")) or "\\" in name or re.match(r"^[A-Za-z]:", name)
            or any(ch < " " or ch == "\x7f" for ch in name)):
        raise ReleaseError(f"{name!r}: absolute, Windows-style or control-character path refused")
    parts = name.split("/")
    if any(p in ("", ".", "..") for p in parts):
        raise ReleaseError(f"{name!r}: empty, '.' or '..' path component refused")
    if prefix is not None and parts[0] != prefix:
        raise ReleaseError(f"{name!r}: outside the single top-level directory {prefix!r}")
    if m.mode not in ((0o755,) if m.isdir() else (0o644, 0o755)):
        raise ReleaseError(f"{name!r}: unexpected mode {oct(m.mode)}")
    rel = "/".join(parts[1:])
    problem = path_problem(name if not rel else rel)  # the same rules the builder applies
    if problem:
        raise ReleaseError(f"{name!r}: non-portable path refused ({problem})")
    return rel


def _check_layout(files: list[str], dirs: list[str]) -> None:
    """Refuse a path that is both a file and a directory ('a' and 'a/b'), or two spellings of one
    path on a case-insensitive filesystem ('D/x' and 'd/y'): either would fail or merge halfway
    through extraction."""
    seen: dict[str, tuple[str, str]] = {}
    paths = [(f, "file") for f in files] + [(d, "directory") for d in dirs if d]
    for rel in files + dirs:
        parent = PurePosixPath(rel).parent
        while str(parent) != ".":
            paths.append((str(parent), "directory"))
            parent = parent.parent
    for rel, kind in paths:
        prev = seen.setdefault(rel.casefold(), (rel, kind))
        if prev != (rel, kind):
            raise ReleaseError(f"{rel!r} ({kind}) conflicts with {prev[0]!r} ({prev[1]})")


def read_archive(archive: Path) -> tuple[str, dict[str, tuple[bytes, int]], list[str]]:
    """(top-level dir, {rel: (bytes, mode)}, [dir rels]) after validating every member and the manifest."""
    if archive.stat().st_size > MAX_ARCHIVE_BYTES:
        raise ReleaseError(f"{archive}: larger than {MAX_ARCHIVE_BYTES} bytes")
    files: dict[str, tuple[bytes, int]] = {}
    dirs: list[str] = []
    seen_folded: dict[str, str] = {}
    total = 0
    prefix = None
    try:
        with tarfile.open(archive, "r:gz") as tar:
            for m in tar:
                if prefix is None:
                    if not m.isdir() or "/" in m.name or not m.name.startswith(PROJECT + "-"):
                        raise ReleaseError(f"first member {m.name!r} is not the {PROJECT}-<VERSION> directory")
                    prefix = m.name
                rel = _check_member(m, prefix)
                folded = m.name.casefold()
                if folded in seen_folded:
                    raise ReleaseError(f"{m.name!r} duplicates {seen_folded[folded]!r} (case-insensitively)")
                seen_folded[folded] = m.name
                if m.isdir():
                    dirs.append(rel)
                    continue
                total += m.size
                if total > MAX_ARCHIVE_BYTES:
                    raise ReleaseError(f"{archive}: more than {MAX_ARCHIVE_BYTES} uncompressed bytes")
                fh = tar.extractfile(m)
                files[rel] = (fh.read() if fh else b"", m.mode)
    except (tarfile.TarError, EOFError, zlib.error, OSError) as e:
        raise ReleaseError(f"{archive}: not a readable tar.gz ({e})") from e
    if prefix is None:
        raise ReleaseError(f"{archive}: empty archive")
    _check_layout(sorted(files), dirs)
    if not SEMVER_RE.match(prefix[len(PROJECT) + 1:]):
        raise ReleaseError(f"top-level directory {prefix!r} does not carry a semantic version")
    if MANIFEST_NAME not in files:
        raise ReleaseError(f"{MANIFEST_NAME} is missing")
    manifest = files[MANIFEST_NAME][0]
    others = {r: d for r, (d, _m) in files.items() if r != MANIFEST_NAME}
    if manifest != render_manifest(others):
        listed = {}
        for line in manifest.decode("utf-8", "replace").splitlines():
            digest, _, rel = line.partition("  ")
            listed[rel] = digest
        bad = sorted(set(listed) ^ set(others)) + sorted(
            r for r in set(listed) & set(others) if listed[r] != hashlib.sha256(others[r]).hexdigest())
        raise ReleaseError(f"{MANIFEST_NAME} does not match the archive content: "
                           + (", ".join(bad[:20]) or "format/order differs"))
    return prefix, files, dirs


def safe_extract(archive: Path, dest: Path) -> Path:
    """Verify `archive` (read_archive), then write it under the empty or new directory `dest`.

    Never uses tarfile.extractall: every path was validated, is created exclusively (no
    overwrite, no following of pre-existing links) and must resolve inside `dest`. The tree is
    written into a hidden staging directory inside `dest`, checked against the archive's file
    list, and renamed to `dest/<prefix>` only when complete; on any failure the staging
    directory (and `dest`, if this call created it) is removed, so nothing partial is left."""
    prefix, files, dirs = read_archive(archive)
    created = not dest.exists()
    dest.mkdir(parents=True, exist_ok=True)
    if any(dest.iterdir()):
        raise EnvError(f"{dest} is not empty")
    base = dest.resolve()
    stage = Path(tempfile.mkdtemp(prefix=".extract-", dir=base))
    try:
        tree = stage / prefix
        for rel in sorted({"", *dirs}):
            target = (tree / rel) if rel else tree
            target.mkdir(parents=True, exist_ok=True)
        for rel, (data, mode) in sorted(files.items()):
            target = tree / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            resolved = target.resolve()
            if stage.resolve() not in resolved.parents:
                raise ReleaseError(f"{rel!r} resolves outside {dest}")
            with open(target, "xb") as fh:
                fh.write(data)
            with contextlib.suppress(OSError):  # no-op for the exec bit on Windows
                os.chmod(target, mode)
        on_disk = {p.relative_to(tree).as_posix(): p.stat().st_size for p in tree.rglob("*") if p.is_file()}
        if on_disk != {rel: len(data) for rel, (data, _mode) in files.items()}:
            raise ReleaseError(f"the extracted tree does not match the archive's file list (filesystem "
                               f"aliasing?): {sorted(set(on_disk) ^ set(files))[:20]}")
        final = base / prefix
        os.replace(tree, final)
        stage.rmdir()
    except BaseException:
        shutil.rmtree(stage, ignore_errors=True)
        if created:
            with contextlib.suppress(OSError):
                dest.rmdir()
        raise
    return final


def verify_archive(archive: Path, sums: Path | None = None, *, require_sums: bool = False) -> str:
    """Validate `archive` (and its SHA256SUMS line, if `sums` exists); returns a one-line summary.

    This is an integrity check: RELEASE-MANIFEST.sha256 is written by whoever built the archive,
    so a match says nothing about who that was. Authenticity comes from the release's
    provenance attestation (`gh attestation verify`, docs/RELEASE.md)."""
    sums_note = "SHA256SUMS: not checked (none found next to the tarball)"
    if sums is None or not sums.exists():
        if require_sums:
            raise ReleaseError(f"no {SUMS_NAME} next to {archive.name}; download it with the tarball "
                               "(or pass --no-sums to check only the archive's own manifest)")
    else:
        expected = None
        for line in sums.read_text(encoding="ascii").splitlines():
            digest, _, name = line.partition("  ")
            if name == archive.name:
                expected = digest
        if expected is None:
            raise ReleaseError(f"{sums} has no line for {archive.name}")
        actual = hashlib.sha256(archive.read_bytes()).hexdigest()
        if actual != expected:
            raise ReleaseError(f"{archive.name}: sha256 {actual} != {expected} in {sums}")
        sums_note = f"{SUMS_NAME}: matches"
    prefix, files, _dirs = read_archive(archive)
    return (f"{archive.name}: {prefix}/ with {len(files) - 1} file(s), every SHA-256 in {MANIFEST_NAME} matches; "
            f"{sums_note} (integrity only - check authenticity with `gh attestation verify`)")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--out-dir", type=Path, default=REPO_ROOT / "dist")
    ap.add_argument("--root", type=Path, default=REPO_ROOT, help="repository root (default: this repo)")
    ap.add_argument("--source-date-epoch", type=int, default=None,
                    help="timestamp for every entry (default: $SOURCE_DATE_EPOCH, else the HEAD commit time)")
    ap.add_argument("--expect-version", help="fail unless VERSION equals this (a leading 'v' is ignored)")
    ap.add_argument("--require-clean", action="store_true", help="fail if git status is not clean")
    ap.add_argument("--skip-generated-check", action="store_true",
                    help="do not run scripts/check_generated.py first (local iteration only, never for a release)")
    ap.add_argument("--list", action="store_true", help="print the files that would ship and exit")
    ap.add_argument("--verify-archive", type=Path, metavar="TARBALL",
                    help="validate a built tarball (members, RELEASE-MANIFEST.sha256, a sibling SHA256SUMS) and exit")
    ap.add_argument("--extract-to", type=Path, metavar="DIR",
                    help="with --verify-archive: also extract it, safely, into this empty or new directory")
    ap.add_argument("--no-sums", action="store_true",
                    help="with --verify-archive: do not require a SHA256SUMS next to the tarball")
    args = ap.parse_args(argv)
    if args.source_date_epoch is not None and args.source_date_epoch < 0:
        ap.error("--source-date-epoch must be >= 0")
    if (args.extract_to is not None or args.no_sums) and args.verify_archive is None:
        ap.error("--extract-to and --no-sums need --verify-archive")
    try:
        if args.verify_archive is not None:
            print(verify_archive(args.verify_archive, args.verify_archive.parent / SUMS_NAME,
                                 require_sums=not args.no_sums))
            if args.extract_to is not None:
                print(f"extracted to {safe_extract(args.verify_archive, args.extract_to)}")
            return 0
        if args.list:
            for rel in select_files(args.root.resolve(), exclude_dir=args.out_dir):
                print(rel)
            return 0
        tar_path, sums = build(args.root, args.out_dir, epoch=args.source_date_epoch,
                               expect_version=args.expect_version, require_clean=args.require_clean,
                               verify_generated=not args.skip_generated_check)
    except ReleaseError as e:
        print(f"build_release: ERROR: {e}", file=sys.stderr)
        return 1
    except EnvError as e:
        print(f"build_release: {e}", file=sys.stderr)
        return 2
    except OSError as e:
        print(f"build_release: {e}", file=sys.stderr)
        return 2
    print(f"built {tar_path} ({tar_path.stat().st_size} bytes)")
    print(sums.read_text(encoding="ascii"), end="")
    return 0


if __name__ == "__main__":
    sys.exit(main())
