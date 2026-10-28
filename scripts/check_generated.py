#!/usr/bin/env python3
"""Generated-artifact drift gate: every committed generated file must be exactly
what its generator produces now.

Default mode (safe anywhere, never touches the working tree):
  1. copy the repository (tracked + untracked-but-not-ignored files; a plain walk
     when git is unavailable) into a temporary directory;
  2. run every generator below in its write mode *inside the copy*, one at a time,
     attributing each changed file to the generator that changed it;
  3. compare the copy against the working tree, byte for byte - so CRLF line
     endings, a missing trailing newline or a stale count all count as drift;
  4. run every generator a second time and require no further change (idempotence).
It also fails when a generator exits non-zero, writes outside its declared
outputs, or when a declared output in the working tree contains CRLF.

--in-place (CI only): refuses to start unless `git status --porcelain` is clean,
runs the generators in the real checkout, then fails if `git status --porcelain`
is no longer clean (printing `git diff --stat`). This is the plain
"regenerate, then git diff --exit-code" check, including untracked files.

Usage:
    python scripts/check_generated.py              # temp-copy check
    python scripts/check_generated.py --in-place   # CI: regenerate in place + git status

Exit codes: 0 no drift; 1 drift or a generator failed; 2 usage/environment error.
"""
from __future__ import annotations

import argparse
import fnmatch
import hashlib
import os
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SKIP_DIRS = {".git", "__pycache__", ".ruff_cache", ".pytest_cache", "dist", ".venv", "venv", "node_modules"}
GENERATOR_TIMEOUT = 300


@dataclass(frozen=True)
class Generator:
    script: str              # path relative to the repo root
    args: tuple[str, ...]    # write-mode arguments
    outputs: tuple[str, ...]  # fnmatch patterns (posix, repo-relative) the generator may write

    @property
    def label(self) -> str:
        return " ".join([self.script, *self.args])


# Order matters only for attribution; every generator reads canonical sources, not
# another generator's output. Keep in sync with the `--check` steps in docs/QUALITY-GATES.md.
GENERATORS: tuple[Generator, ...] = (
    Generator("scripts/generate_schema.py", ("--write",), ("schema/skill-frontmatter.schema.json",)),
    Generator("scripts/check_contract.py", ("--write",), ("schema/public-contract.json",)),
    Generator("scripts/generate_index.py", ("--fix",), ("INDEX.md",)),
    Generator("scripts/build_dependency_graph.py", ("--write",), ("docs/SKILL-GRAPH.md",)),
    Generator("scripts/generate_readme_stats.py", ("--write",), ("README.md",)),
    Generator("scripts/generate_plugins.py", ("--write",), ("plugins/*", ".claude-plugin/marketplace.json")),
)


@dataclass
class Report:
    problems: list[str] = field(default_factory=list)
    drift: dict[str, str] = field(default_factory=dict)  # rel path -> "modified" | "created" | "deleted"
    attributed: dict[str, str] = field(default_factory=dict)  # rel path -> generator label

    @property
    def ok(self) -> bool:
        return not self.problems and not self.drift


# --------------------------------------------------------------------------- file sets

def list_source_files(root: Path) -> list[str]:
    """Repo-relative posix paths of the files to copy: what git would commit
    (tracked + untracked-not-ignored, existing on disk); a filtered walk without git."""
    try:
        out = subprocess.run(
            ["git", "-C", str(root), "ls-files", "-z", "--cached", "--others", "--exclude-standard"],
            capture_output=True, check=True, timeout=60,
        ).stdout
        top = subprocess.run(["git", "-C", str(root), "rev-parse", "--show-toplevel"],
                             capture_output=True, text=True, check=True, timeout=60).stdout.strip()
        if Path(top).resolve() == root.resolve():
            names = sorted({n for n in out.decode("utf-8").split("\0") if n})
            return [n for n in names if (root / n).is_file() and not _skipped(n)]
    except (OSError, subprocess.SubprocessError, UnicodeDecodeError):
        pass
    files = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if d not in SKIP_DIRS)
        for fn in filenames:
            rel = (Path(dirpath) / fn).relative_to(root).as_posix()
            if not _skipped(rel):
                files.append(rel)
    return sorted(files)


def _skipped(rel: str) -> bool:
    return any(part in SKIP_DIRS for part in rel.split("/")[:-1]) or rel.endswith((".pyc", ".pyo"))


def snapshot(root: Path) -> dict[str, str]:
    """{rel: sha256} for every file under root (skipping caches)."""
    snap = {}
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
        for fn in filenames:
            p = Path(dirpath) / fn
            rel = p.relative_to(root).as_posix()
            if not _skipped(rel):
                snap[rel] = hashlib.sha256(p.read_bytes()).hexdigest()
    return snap


def diff_snapshots(before: dict[str, str], after: dict[str, str]) -> dict[str, str]:
    changes = {}
    for rel in before.keys() | after.keys():
        if rel not in after:
            changes[rel] = "deleted"
        elif rel not in before:
            changes[rel] = "created"
        elif before[rel] != after[rel]:
            changes[rel] = "modified"
    return dict(sorted(changes.items()))


def declared(gen: Generator, rel: str) -> bool:
    return any(fnmatch.fnmatchcase(rel, pat) for pat in gen.outputs)


# --------------------------------------------------------------------------- running

def run_generator(gen: Generator, root: Path) -> tuple[int, str]:
    env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1", PYTHONUTF8="1")
    try:
        proc = subprocess.run([sys.executable, str(root / gen.script), *gen.args], cwd=root, env=env,
                              capture_output=True, text=True, encoding="utf-8", errors="replace",
                              timeout=GENERATOR_TIMEOUT)
    except subprocess.TimeoutExpired:
        return 124, f"timed out after {GENERATOR_TIMEOUT}s"
    except OSError as e:
        return 127, str(e)
    return proc.returncode, (proc.stdout + proc.stderr).strip()


def run_all(root: Path, generators: tuple[Generator, ...], report: Report, pass_no: int) -> dict[str, str]:
    """Run each generator in root; return {rel: change} for the whole pass."""
    start = snapshot(root)
    before = start
    for gen in generators:
        if not (root / gen.script).is_file():
            report.problems.append(f"generator not found: {gen.script}")
            continue
        code, output = run_generator(gen, root)
        after = snapshot(root)
        changes = diff_snapshots(before, after)
        if code != 0:
            tail = "\n    ".join(output.splitlines()[-15:])
            report.problems.append(f"pass {pass_no}: `{gen.label}` exited {code}:\n    {tail}")
        for rel in changes:
            if not declared(gen, rel):
                report.problems.append(f"pass {pass_no}: `{gen.label}` changed {rel}, which is not one of its "
                                       f"declared outputs {list(gen.outputs)}")
            if pass_no == 1:
                report.attributed.setdefault(rel, gen.label)
        before = after
    return diff_snapshots(start, before)


def crlf_outputs(root: Path, generators: tuple[Generator, ...], files: list[str]) -> list[str]:
    hits = []
    for rel in files:
        if any(declared(g, rel) for g in generators) and b"\r\n" in (root / rel).read_bytes():
            hits.append(rel)
    return hits


def check_copy(root: Path, generators: tuple[Generator, ...] = GENERATORS) -> Report:
    report = Report()
    files = list_source_files(root)
    for rel in crlf_outputs(root, generators, files):
        report.problems.append(f"{rel}: generated file contains CRLF line endings (generators write LF)")
    with tempfile.TemporaryDirectory(prefix="check-generated-") as tmp:
        copy = Path(tmp) / "repo"
        for rel in files:
            dst = copy / rel
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(root / rel, dst)
        original = snapshot(copy)
        run_all(copy, generators, report, pass_no=1)
        report.drift = diff_snapshots(original, snapshot(copy))
        second = run_all(copy, generators, report, pass_no=2)
        for rel, change in second.items():
            report.problems.append(f"not idempotent: a second run of the generators {change} {rel}")
    return report


def _git(root: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", str(root), "-c", "core.quotepath=off", *args],
                          capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=120)


def check_in_place(root: Path, generators: tuple[Generator, ...] = GENERATORS) -> int:
    try:
        status = _git(root, "status", "--porcelain", "--untracked-files=all")
    except (OSError, subprocess.SubprocessError) as e:
        print(f"check_generated: git unavailable: {e}", file=sys.stderr)
        return 2
    if status.returncode != 0:
        print(f"check_generated: not a git checkout: {status.stderr.strip()}", file=sys.stderr)
        return 2
    if status.stdout.strip():
        print("check_generated --in-place refuses to run on a dirty tree (it would overwrite local edits):\n"
              + status.stdout, file=sys.stderr)
        return 2
    report = Report()
    run_all(root, generators, report, pass_no=1)
    after = _git(root, "status", "--porcelain", "--untracked-files=all").stdout
    for p in report.problems:
        print(f"GENERATOR ERROR: {p}")
    if after.strip():
        print("GENERATED DRIFT: regenerating in place changed the checkout:")
        print(after.rstrip())
        print(_git(root, "diff", "--stat").stdout.rstrip())
        print("\nRun the generators locally (see scripts/check_generated.py GENERATORS) and commit the result.")
        return 1
    if report.problems:
        return 1
    print(f"check_generated --in-place: {len(generators)} generator(s) ran; git status is clean")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--in-place", action="store_true",
                    help="CI: run generators in the checkout and fail if git status changes (needs a clean tree)")
    ap.add_argument("--root", type=Path, default=REPO_ROOT, help=argparse.SUPPRESS)
    args = ap.parse_args(argv)
    root = args.root.resolve()

    if args.in_place:
        return check_in_place(root)

    report = check_copy(root)
    for p in report.problems:
        print(f"GENERATOR ERROR: {p}")
    for rel, change in report.drift.items():
        by = report.attributed.get(rel, "?")
        print(f"GENERATED DRIFT: {rel} would be {change} by `{by}`")
    if report.ok:
        print(f"check_generated: {len(GENERATORS)} generator(s), no drift, idempotent")
        return 0
    if report.drift:
        fixes = sorted({report.attributed.get(r, "?") for r in report.drift})
        print(f"\n{len(report.drift)} drifted file(s). Fix with: " + "; ".join(f"python {f}" for f in fixes))
    return 1


if __name__ == "__main__":
    sys.exit(main())
