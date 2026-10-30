"""Opt-in gating shared by the integration tests (stdlib only).

``opt_in(NAME, ...)`` is a class decorator:

* ``ML_AI_SKILLS_INTEGRATION_<NAME>`` unset (or not ``1``) -> the class is **skipped**, and the
  reason names the variable and what the class would check;
* set to ``1`` -> the class runs, and a missing Python module, executable or precondition is a
  **failure** raised from ``setUpClass``. Someone who opted in wants evidence, and a skip there
  would be indistinguishable from a pass in a summary line.

``block_index(slug, needle)`` finds a SKILL.md python block by a string it must contain exactly
once across the skill, so a test keeps pointing at the right block when blocks are reordered.
"""
from __future__ import annotations

import importlib.util
import os
import shutil
import unittest
from typing import Callable, Iterable

from examples import harness

PREFIX = "ML_AI_SKILLS_INTEGRATION_"


def flag(name: str) -> str:
    return PREFIX + name


def enabled(name: str) -> bool:
    return os.environ.get(flag(name)) == "1"


def _missing(modules: Iterable[str], binaries: Iterable[str]) -> list[str]:
    out = []
    for mod in modules:
        try:
            found = importlib.util.find_spec(mod) is not None
        except (ImportError, ValueError):   # a dotted name whose parent is missing
            found = False
        if not found:
            out.append(f"python module {mod!r}")
    out += [f"executable {b!r} on PATH" for b in binaries if shutil.which(b) is None]
    return out


def opt_in(name: str, *, what: str, modules: Iterable[str] = (), binaries: Iterable[str] = (),
           precondition: Callable[[], str | None] | None = None):
    """Skip unless ``ML_AI_SKILLS_INTEGRATION_<name>=1``; once opted in, fail on anything missing.

    ``precondition`` runs only when opted in and the modules/binaries exist; it returns ``None``
    when the environment is usable, or a message explaining what is missing (e.g. no CUDA device).
    """
    modules, binaries = tuple(modules), tuple(binaries)

    def decorate(cls):
        if not enabled(name):
            return unittest.skip(f"opt-in integration test ({what}); set {flag(name)}=1 to run")(cls)
        original = cls.__dict__.get("setUpClass")

        def setUpClass(klass):  # noqa: N802 - unittest hook name
            missing = _missing(modules, binaries)
            if missing:
                raise AssertionError(f"{flag(name)}=1 but missing: {', '.join(missing)}")
            if precondition is not None:
                problem = precondition()
                if problem:
                    raise AssertionError(f"{flag(name)}=1 but {problem}")
            if original is not None:
                original.__get__(None, klass)()
            else:
                super(cls, klass).setUpClass()

        cls.setUpClass = classmethod(setUpClass)
        return cls

    return decorate


def block_index(slug: str, needle: str) -> int:
    """Index of the one python block of ``<slug>/SKILL.md`` that contains ``needle``."""
    hits = [i for i, code in enumerate(harness.python_blocks(slug)) if needle in code]
    if len(hits) != 1:
        raise AssertionError(f"{slug}: {needle!r} is in python blocks {hits}; expected exactly one "
                             "(the skill changed; update the integration test)")
    return hits[0]
