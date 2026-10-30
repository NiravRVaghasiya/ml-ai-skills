"""Harness for executing the Python code blocks embedded in a SKILL.md.

The point of these tests is to protect the *published* skill content, not a
copy of it: every test loads the canonical ``<slug>/SKILL.md``, extracts its
fenced ``python`` blocks in document order, and ``exec``s the selected blocks
in one shared namespace (blocks in a Workflow build on each other).

Rules for test authors (see docs/EXAMPLE-TESTS.md):

* Prefer running blocks verbatim. When a block needs something the test
  environment cannot provide (a network-downloaded model, a CSV on disk, an
  API key), inject it through ``namespace=`` / ``stubs=`` — not by rewriting
  the block. ``replace=`` (exact, must-match textual substitution) is the last
  resort, e.g. shrinking ``n_trials=200`` to ``n_trials=3`` for speed; every
  replacement must match exactly once or the test errors, so a skill edit
  that invalidates the substitution is noticed instead of silently skipped.
* Blocks whose first non-blank line starts with ``# illustrative`` are
  documentation-only by convention and are not expected to run standalone.
* Code runs inside a fresh temporary working directory, so blocks that write
  artifacts (``joblib.dump("model.joblib")``) cannot pollute the repo.

Environment variables:

* ``ML_AI_SKILLS_REQUIRE_EXAMPLE_DEPS=1`` (the CI examples job): a missing
  dependency named in ``requires(...)`` fails instead of skipping, and
  opt-in slow tests (``slow(...)``) run, so nothing is skipped by a decorator.
* ``ML_AI_SKILLS_SLOW_EXAMPLES=1``: run opt-in slow tests locally; without it
  (and without the variable above) they skip with a reason naming it.
* ``ML_AI_SKILLS_EXAMPLE_COVERAGE=<path>``: append one coverage record per
  successful ``run_blocks`` call (see ``_record_coverage``).
* ``ML_AI_SKILLS_EXAMPLE_TIMEOUT=<seconds>`` (opt-in watchdog; unset, empty or ``0`` = off):
  if one ``run_blocks`` call runs longer, ``faulthandler`` dumps every thread's traceback
  to stderr and exits the whole process with status 1, so a hung block fails the job
  with a traceback instead of hanging until the CI job timeout. It is a process exit,
  not a test failure: later tests do not run (see ``block_watchdog``). Nothing sets
  this variable by default (CI included), so it protects only runs that set it.
"""
from __future__ import annotations

import contextlib
import datetime as dt
import faulthandler
import functools
import hashlib
import importlib.util
import json
import math
import os
import platform
import subprocess
import sys
import tempfile
import types
import unittest
from pathlib import Path
from typing import Any, Iterable, Mapping

TESTS_DIR = Path(__file__).resolve().parent.parent
REPO_ROOT = TESTS_DIR.parent
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import skills_lib as sl  # noqa: E402

REQUIRE_ENV = "ML_AI_SKILLS_REQUIRE_EXAMPLE_DEPS"
SLOW_ENV = "ML_AI_SKILLS_SLOW_EXAMPLES"
COVERAGE_ENV = "ML_AI_SKILLS_EXAMPLE_COVERAGE"
TIMEOUT_ENV = "ML_AI_SKILLS_EXAMPLE_TIMEOUT"
ILLUSTRATIVE_MARKER = "# illustrative"
# Coverage records without "schema" are the earlier format (skill, blocks, total_blocks,
# skill_sha256); version 2 adds where, when and by which test the blocks ran.
COVERAGE_SCHEMA = "ml-ai-skills/example-coverage"
COVERAGE_SCHEMA_VERSION = 2
GIT_TIMEOUT = 60


def python_blocks(slug: str) -> list[str]:
    """Return every fenced ``python`` block of ``<slug>/SKILL.md`` (dedented), in order."""
    skill = sl.load_skill(REPO_ROOT / slug)
    return [code for _, code in skill.code_blocks(lang="python")]


def is_illustrative(code: str) -> bool:
    for line in code.splitlines():
        if line.strip():
            return line.strip().lower().startswith(ILLUSTRATIVE_MARKER)
    return False


def missing_modules(*modules: str) -> list[str]:
    return [m for m in modules if importlib.util.find_spec(m) is None]


def requires(*modules: str):
    """Class/function decorator: skip when an import is unavailable — or fail,
    when ML_AI_SKILLS_REQUIRE_EXAMPLE_DEPS=1, so CI can't silently skip."""
    missing = missing_modules(*modules)
    if not missing:
        return lambda obj: obj
    reason = f"example dependencies not installed: {', '.join(missing)}"
    if os.environ.get(REQUIRE_ENV) == "1":
        def _fail(obj):
            if isinstance(obj, type):
                def setUpClass(cls):  # noqa: N802 - unittest hook name
                    raise AssertionError(f"{REQUIRE_ENV}=1 but {reason}")
                obj.setUpClass = classmethod(setUpClass)
                return obj

            def wrapper(*a, **kw):
                raise AssertionError(f"{REQUIRE_ENV}=1 but {reason}")
            return wrapper
        return _fail
    return unittest.skip(reason)


def slow_examples_enabled() -> bool:
    """True when opt-in slow tests should run: ML_AI_SKILLS_SLOW_EXAMPLES=1, or
    ML_AI_SKILLS_REQUIRE_EXAMPLE_DEPS=1 (CI), which must never skip a test as "slow"."""
    return os.environ.get(SLOW_ENV) == "1" or os.environ.get(REQUIRE_ENV) == "1"


def slow(why: str):
    """Class/function decorator for opt-in slow tests (e.g. real MLflow setup).

    Runs them when ``slow_examples_enabled()``; otherwise skips with a reason that names
    ML_AI_SKILLS_SLOW_EXAMPLES. Read when the decorator is applied (import time)."""
    if slow_examples_enabled():
        return lambda obj: obj
    return unittest.skip(f"slow ({why}); set {SLOW_ENV}=1 to run")


@contextlib.contextmanager
def stub_modules(stubs: Mapping[str, types.ModuleType] | None):
    """Temporarily install fake modules into ``sys.modules`` (restored after)."""
    stubs = dict(stubs or {})
    saved = {name: sys.modules.get(name) for name in stubs}
    try:
        sys.modules.update(stubs)
        yield
    finally:
        for name, mod in saved.items():
            if mod is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = mod


def make_module(name: str, **attrs: Any) -> types.ModuleType:
    mod = types.ModuleType(name)
    for k, v in attrs.items():
        setattr(mod, k, v)
    return mod


def _apply_replacements(code: str, replace: Mapping[str, str] | None, where: str) -> str:
    for old, new in (replace or {}).items():
        count = code.count(old)
        if count != 1:
            raise AssertionError(
                f"{where}: replacement target {old!r} found {count} times (expected exactly 1) — "
                "the skill's code changed; update the test's replace= map"
            )
        code = code.replace(old, new)
    return code


def _git(*args: str) -> str | None:
    try:
        # --no-optional-locks: never take .git/index.lock while other git processes run.
        p = subprocess.run(["git", "--no-optional-locks", *args], cwd=REPO_ROOT, capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=GIT_TIMEOUT)
    except (OSError, subprocess.SubprocessError):
        return None
    return p.stdout if p.returncode == 0 else None


@functools.lru_cache(maxsize=1)
def run_provenance() -> dict[str, Any]:
    """Where this process runs: git revision/dirty state of REPO_ROOT, python, platform.

    Computed once per process. The revision is only reported when REPO_ROOT is itself the
    top of a git work tree, so an extracted release inside some other repository records
    None instead of that repository's HEAD. ``git_dirty`` covers tracked files only, and
    ``git_untracked_files`` counts untracked, non-ignored files other than the coverage file
    itself: a CI job's own logs, freeze files and the coverage jsonl are untracked, and folding
    them into ``git_dirty`` would make it true on every CI run. Per-skill content is pinned by
    each record's ``skill_sha256``.
    """
    top = _git("rev-parse", "--show-toplevel")
    rev = _git("rev-parse", "HEAD")
    same_tree = (top is not None and
                 os.path.normcase(os.path.realpath(top.strip())) == os.path.normcase(os.path.realpath(REPO_ROOT)))
    revision = rev.strip() if same_tree and rev else None
    status = _git("status", "--porcelain", "--untracked-files=no") if revision else None
    others = _git("ls-files", "--others", "--exclude-standard", "-z") if revision else None
    untracked = None
    if others is not None:
        out = os.environ.get(COVERAGE_ENV)
        skip = os.path.normcase(os.path.realpath(out)) if out else None
        untracked = sum(1 for rel in others.split("\0")
                        if rel and os.path.normcase(os.path.realpath(REPO_ROOT / rel)) != skip)
    return {"git_revision": revision, "git_dirty": (bool(status.strip()) if status is not None else None),
            "git_untracked_files": untracked,
            "python": platform.python_version(), "implementation": platform.python_implementation(),
            "platform": platform.system(), "platform_release": platform.release()}


def calling_test() -> dict[str, str | None]:
    """The unittest test (or test class, from setUpClass) on the call stack, if any.

    Recorded with each coverage line so scripts/quality_report.py can attribute example
    test outcomes to the skill whose blocks a test actually ran, not only by module name.
    """
    frame = sys._getframe(1)
    module = None
    while frame is not None:
        name = frame.f_globals.get("__name__")
        if module is None and name != __name__:
            module = name
        local = frame.f_locals
        obj = local.get("self")
        if isinstance(obj, unittest.TestCase):
            return {"test_id": obj.id(), "test_module": type(obj).__module__}
        cls = local.get("cls")
        if isinstance(cls, type) and issubclass(cls, unittest.TestCase):
            return {"test_id": f"{cls.__module__}.{cls.__qualname__}", "test_module": cls.__module__}
        frame = frame.f_back
    return {"test_id": None, "test_module": module}


def _now() -> str:
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def coverage_record(slug: str, indices: Iterable[int], total: int) -> dict[str, Any]:
    """One coverage record (schema ml-ai-skills/example-coverage, version 2).

    ``skill_sha256`` (LF-normalised) ties the record to the exact SKILL.md whose blocks ran,
    so scripts/quality_report.py and scripts/check_example_coverage.py discard coverage
    recorded against another version of it even if the block count matches. ``git_revision``
    / ``git_dirty`` / ``git_untracked_files``, ``python``, ``platform`` and ``recorded_at`` say
    where and when it ran; ``test_id`` / ``test_module`` say which test ran it (None when not called from a test).
    """
    raw = (REPO_ROOT / slug / "SKILL.md").read_bytes().replace(b"\r\n", b"\n")
    prov = run_provenance()
    return {"schema": COVERAGE_SCHEMA, "schema_version": COVERAGE_SCHEMA_VERSION,
            "skill": slug, "blocks": sorted(set(indices)), "total_blocks": total,
            "skill_sha256": hashlib.sha256(raw).hexdigest(),
            "git_revision": prov["git_revision"], "git_dirty": prov["git_dirty"],
            "git_untracked_files": prov["git_untracked_files"], "python": prov["python"],
            "platform": prov["platform"], "recorded_at": _now(),
            "env": {k: os.environ.get(k) for k in (REQUIRE_ENV, SLOW_ENV)},
            **calling_test()}


def _record_coverage(slug: str, indices: Iterable[int], total: int) -> None:
    path = os.environ.get(COVERAGE_ENV)
    if not path:
        return
    record = coverage_record(slug, indices, total)
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(record, sort_keys=True) + "\n")


def example_timeout() -> float:
    """Seconds allowed per ``run_blocks`` call from ML_AI_SKILLS_EXAMPLE_TIMEOUT; 0 = no watchdog."""
    raw = (os.environ.get(TIMEOUT_ENV) or "").strip()
    if not raw:
        return 0.0
    try:
        limit = float(raw)
    except ValueError:
        limit = float("nan")
    if not math.isfinite(limit):
        raise AssertionError(f"{TIMEOUT_ENV}={raw!r} is not a finite number of seconds")
    return limit if limit > 0 else 0.0


@contextlib.contextmanager
def block_watchdog():
    """Arm ``faulthandler.dump_traceback_later(limit, exit=True)`` for the body when
    ML_AI_SKILLS_EXAMPLE_TIMEOUT is set (> 0); always cancelled on exit, even when a block
    raises. faulthandler holds one timer per process and has no API to read it back, so
    this is not reentrant: arming replaces any timer already set (a nested call, or a
    runner's own such as pytest's ``faulthandler_timeout``), and the exit cancels it
    without restoring the earlier one. Leave the variable unset when another layer
    already owns faulthandler's timer. The dump goes to the real stderr
    (``sys.__stderr__``) because unittest's ``-b`` buffering swaps ``sys.stderr`` for an
    object without a file descriptor."""
    limit = example_timeout()
    if limit <= 0:
        yield
        return
    faulthandler.dump_traceback_later(limit, exit=True, file=sys.__stderr__ or sys.stderr)
    try:
        yield
    finally:
        faulthandler.cancel_dump_traceback_later()


def run_blocks(
    slug: str,
    indices: Iterable[int] | None = None,
    *,
    namespace: dict[str, Any] | None = None,
    stubs: Mapping[str, types.ModuleType] | None = None,
    replace: Mapping[int, Mapping[str, str]] | None = None,
    workdir: Path | None = None,
) -> dict[str, Any]:
    """Execute the selected python blocks of ``<slug>/SKILL.md`` in one namespace.

    ``indices`` defaults to every non-illustrative block. ``replace`` maps a
    block index to exact-match textual substitutions (see module docstring).
    Returns the namespace so the test can assert on the objects the blocks built.
    """
    blocks = python_blocks(slug)
    if indices is None:
        indices = [i for i, b in enumerate(blocks) if not is_illustrative(b)]
    indices = list(indices)
    for i in indices:
        if not 0 <= i < len(blocks):
            raise AssertionError(f"{slug}: block index {i} out of range (skill has {len(blocks)} python blocks)")

    ns: dict[str, Any] = {"__name__": f"skill_example_{slug.replace('-', '_')}"}
    ns.update(namespace or {})
    replace = replace or {}

    tmp_ctx = tempfile.TemporaryDirectory(ignore_cleanup_errors=True) if workdir is None else contextlib.nullcontext(str(workdir))
    with tmp_ctx as tmp, stub_modules(stubs):
        old_cwd = os.getcwd()
        os.chdir(tmp)
        try:
            with block_watchdog():
                for i in indices:
                    where = f"{slug}/SKILL.md python block #{i}"
                    code = _apply_replacements(blocks[i], replace.get(i), where)
                    exec(compile(code, where, "exec", dont_inherit=True), ns)  # noqa: S102 - executing repo-owned example code is the point
        finally:
            os.chdir(old_cwd)
    _record_coverage(slug, indices, len(blocks))
    return ns
