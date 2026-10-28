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
"""
from __future__ import annotations

import contextlib
import hashlib
import importlib.util
import json
import os
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
COVERAGE_ENV = "ML_AI_SKILLS_EXAMPLE_COVERAGE"
ILLUSTRATIVE_MARKER = "# illustrative"


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


def _record_coverage(slug: str, indices: Iterable[int], total: int) -> None:
    path = os.environ.get(COVERAGE_ENV)
    if not path:
        return
    # skill_sha256 (LF-normalised) lets scripts/quality_report.py discard coverage
    # recorded against an older revision of the SKILL.md, even if the block count matches.
    raw = (REPO_ROOT / slug / "SKILL.md").read_bytes().replace(b"\r\n", b"\n")
    record = {"skill": slug, "blocks": sorted(set(indices)), "total_blocks": total,
              "skill_sha256": hashlib.sha256(raw).hexdigest()}
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(record) + "\n")


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
            for i in indices:
                where = f"{slug}/SKILL.md python block #{i}"
                code = _apply_replacements(blocks[i], replace.get(i), where)
                exec(compile(code, where, "exec", dont_inherit=True), ns)  # noqa: S102 - executing repo-owned example code is the point
        finally:
            os.chdir(old_cwd)
    _record_coverage(slug, indices, len(blocks))
    return ns
