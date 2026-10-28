#!/usr/bin/env python3
"""Build the reproducible release tarball of the canonical distribution.

Output (default ``dist/``):
    ml-ai-skills-<VERSION>.tar.gz   the distribution, under one top-level directory
    SHA256SUMS                      ``<sha256>  <file>`` for the tarball (sha256sum -c format)

Inside the tarball, ``ml-ai-skills-<VERSION>/RELEASE-MANIFEST.sha256`` lists the
SHA-256 of every other file, so an extracted tree can be checked with
``sha256sum -c RELEASE-MANIFEST.sha256`` from that directory.

What ships (everything a user needs; nothing that is only for developing the repo):
    every skill folder (``<slug>/``, as found by skills_lib.iter_skill_dirs)
    plugins/, .claude-plugin/              native Claude Code plugin/marketplace packaging
    install.sh, uninstall.sh, bin/ml-ai-skills
    scripts/*.py, schema/                  validate/list tooling bin/ml-ai-skills runs;
                                           install.sh requires schema/allowed_values.py
    INDEX.md (install.sh requires it), docs/ (README links into it)
    LICENSE, README.md, CHANGELOG.md, VERSION, SECURITY.md, CONTRIBUTING.md
Not shipped: tests/, evals/, .github/, _TEMPLATE/, CLAUDE.md, requirements-*.txt, caches.
Files are taken from what git would commit (tracked + untracked-but-not-ignored);
release.yml additionally requires a clean tree (--require-clean).

Reproducibility: entries are sorted; every entry gets mtime = SOURCE_DATE_EPOCH
(env var or --source-date-epoch; else the HEAD commit time), uid/gid 0, empty
owner names, and mode 0755 (directories, files starting with "#!") or 0644; the
gzip header carries the same mtime and no file name. File bytes are copied
verbatim; a text file with CRLF line endings is an error (the repo is LF-only via
.gitattributes, and a CRLF checkout would silently change the archive).
Byte-identical output across machines additionally assumes the same zlib
compression output for the same input and level, which holds for a given zlib
version but is not guaranteed across zlib implementations; the per-file manifest
is independent of compression.

Usage:
    python scripts/build_release.py [--out-dir dist] [--expect-version 1.2.3] [--require-clean]
    python scripts/build_release.py --list          # print the files that would ship

Exit codes: 0 built; 1 content/consistency error; 2 usage/environment error.
"""
from __future__ import annotations

import argparse
import gzip
import hashlib
import io
import os
import re
import subprocess
import sys
import tarfile
from pathlib import Path, PurePosixPath

sys.path.insert(0, str(Path(__file__).resolve().parent))
import skills_lib as sl  # noqa: E402

REPO_ROOT = sl.REPO_ROOT
PROJECT = "ml-ai-skills"
REQUIRED_FILES = ("LICENSE", "README.md", "CHANGELOG.md", "VERSION", "INDEX.md", "install.sh", "uninstall.sh",
                  "bin/ml-ai-skills", ".claude-plugin/marketplace.json", "schema/allowed_values.py")
TOP_FILES = {"LICENSE", "README.md", "CHANGELOG.md", "VERSION", "INDEX.md", "install.sh", "uninstall.sh",
             "SECURITY.md", "CONTRIBUTING.md"}  # shipped docs link to both
TREES = ("plugins/", ".claude-plugin/", "bin/", "schema/", "docs/")
SKIP_PARTS = {"__pycache__", ".pytest_cache", ".ruff_cache"}
MANIFEST_NAME = "RELEASE-MANIFEST.sha256"
SEMVER_RE = re.compile(r"^\d+\.\d+\.\d+(?:-[0-9A-Za-z.-]+)?(?:\+[0-9A-Za-z.-]+)?$")


class ReleaseError(Exception):
    """A problem with the content being released (exit 1)."""


class EnvError(Exception):
    """A problem with the environment or arguments (exit 2)."""


def _git(root: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", str(root), *args], capture_output=True, timeout=60)


def _is_repo_root(root: Path) -> bool:
    try:
        top = _git(root, "rev-parse", "--show-toplevel")
    except (OSError, subprocess.SubprocessError):
        return False
    return top.returncode == 0 and Path(top.stdout.decode().strip()).resolve() == root.resolve()


def candidate_files(root: Path) -> list[str]:
    """Repo-relative posix paths of every file git would commit (a walk without git)."""
    if _is_repo_root(root):
        out = _git(root, "ls-files", "-z", "--cached", "--others", "--exclude-standard").stdout.decode("utf-8")
        names = {n for n in out.split("\0") if n}
    else:
        names = set()
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = [d for d in dirnames if d != ".git"]
            for fn in filenames:
                names.add((Path(dirpath) / fn).relative_to(root).as_posix())
    return sorted(n for n in names
                  if (root / n).is_file() and not SKIP_PARTS & set(n.split("/")) and not n.endswith((".pyc", ".pyo")))


def select_files(root: Path) -> list[str]:
    skill_dirs = {p.name + "/" for p in sl.iter_skill_dirs(root)}
    chosen = []
    for rel in candidate_files(root):
        top = rel.split("/", 1)[0] + "/"
        if (rel in TOP_FILES or rel.startswith(TREES) or top in skill_dirs
                or (rel.startswith("scripts/") and rel.count("/") == 1 and rel.endswith(".py"))):
            chosen.append(rel)
    missing = [f for f in REQUIRED_FILES if f not in chosen]
    if missing:
        raise ReleaseError(f"required file(s) missing from the distribution: {', '.join(missing)}")
    if not skill_dirs:
        raise ReleaseError("no skill folders found")
    if MANIFEST_NAME in chosen:
        raise ReleaseError(f"{MANIFEST_NAME} is generated by this script and must not exist in the repo")
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


def build_tar_bytes(root: Path, files: list[str], version: str, epoch: int) -> bytes:
    prefix = f"{PROJECT}-{version}"
    contents: dict[str, bytes] = {}
    for rel in files:
        data = (root / rel).read_bytes()
        if b"\0" not in data and b"\r\n" in data:
            raise ReleaseError(f"{rel}: CRLF line endings (the repo is LF-only; fix the checkout or the file)")
        contents[rel] = data
    manifest = "".join(f"{hashlib.sha256(contents[r]).hexdigest()}  {r}\n" for r in sorted(contents))
    contents[MANIFEST_NAME] = manifest.encode("utf-8")

    dirs = set()
    for rel in contents:
        parent = PurePosixPath(rel).parent
        while str(parent) != ".":
            dirs.add(str(parent))
            parent = parent.parent
    entries = sorted([(d, None) for d in dirs] + [(f, contents[f]) for f in contents], key=lambda e: e[0])

    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w", format=tarfile.PAX_FORMAT) as tar:
        tar.addfile(_info(prefix, epoch, is_dir=True))
        for rel, data in entries:
            name = f"{prefix}/{rel}"
            if data is None:
                tar.addfile(_info(name, epoch, is_dir=True))
            else:
                tar.addfile(_info(name, epoch, size=len(data), mode=_mode(data)), io.BytesIO(data))
    return buf.getvalue()


def gzip_bytes(data: bytes, epoch: int) -> bytes:
    out = io.BytesIO()
    with gzip.GzipFile(filename="", mode="wb", fileobj=out, mtime=epoch, compresslevel=9) as gz:
        gz.write(data)
    return out.getvalue()


def build(root: Path, out_dir: Path, *, epoch: int | None = None, expect_version: str | None = None,
          require_clean: bool = False) -> tuple[Path, Path]:
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
    files = select_files(root)
    archive = gzip_bytes(build_tar_bytes(root, files, version, when), when)

    out_dir.mkdir(parents=True, exist_ok=True)
    name = f"{PROJECT}-{version}.tar.gz"
    tar_path = out_dir / name
    tar_path.write_bytes(archive)
    sums = out_dir / "SHA256SUMS"
    sums.write_bytes(f"{hashlib.sha256(archive).hexdigest()}  {name}\n".encode("ascii"))
    return tar_path, sums


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--out-dir", type=Path, default=REPO_ROOT / "dist")
    ap.add_argument("--root", type=Path, default=REPO_ROOT, help="repository root (default: this repo)")
    ap.add_argument("--source-date-epoch", type=int, default=None,
                    help="timestamp for every entry (default: $SOURCE_DATE_EPOCH, else the HEAD commit time)")
    ap.add_argument("--expect-version", help="fail unless VERSION equals this (a leading 'v' is ignored)")
    ap.add_argument("--require-clean", action="store_true", help="fail if git status is not clean")
    ap.add_argument("--list", action="store_true", help="print the files that would ship and exit")
    args = ap.parse_args(argv)
    if args.source_date_epoch is not None and args.source_date_epoch < 0:
        ap.error("--source-date-epoch must be >= 0")
    try:
        if args.list:
            for rel in select_files(args.root.resolve()):
                print(rel)
            return 0
        tar_path, sums = build(args.root, args.out_dir, epoch=args.source_date_epoch,
                               expect_version=args.expect_version, require_clean=args.require_clean)
    except ReleaseError as e:
        print(f"build_release: ERROR: {e}", file=sys.stderr)
        return 1
    except EnvError as e:
        print(f"build_release: {e}", file=sys.stderr)
        return 2
    print(f"built {tar_path} ({tar_path.stat().st_size} bytes)")
    print(sums.read_text(encoding="ascii"), end="")
    return 0


if __name__ == "__main__":
    sys.exit(main())
