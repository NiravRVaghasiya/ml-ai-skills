#!/usr/bin/env python3
"""Keep README.md's PROJECT-STATS block honest, the same way generate_index.py
keeps INDEX.md honest: compute every number from the actual repo state, never
hand-maintain a count that can silently drift out of date.

Usage:
    python scripts/generate_readme_stats.py --check   # exit 1 if README.md is stale (CI)
    python scripts/generate_readme_stats.py --write    # regenerate the block in place
"""
from __future__ import annotations

import argparse
import ast
import sys
import unittest
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import skills_lib as sl  # noqa: E402

README = sl.REPO_ROOT / "README.md"
START = "<!-- PROJECT-STATS:START -->"
END = "<!-- PROJECT-STATS:END -->"

DOMAIN_LABEL = {
    "foundations": "Foundations",
    "classical-ml": "Classical ML",
    "deep-learning": "Deep Learning",
    "llm": "LLM",
    "specialized": "Specialized",
    "mlops": "MLOps",
    "responsible-ai": "Responsible AI",
    "security": "Security",
}
DOMAIN_ORDER = list(DOMAIN_LABEL)


def _opt_in_classes(module_file: Path) -> set[str]:
    """Top-level classes in ``module_file`` decorated with ``opt_in(...)`` / ``gate.opt_in(...)``.

    Read statically, so the split does not depend on which ML_AI_SKILLS_INTEGRATION_* flags are
    set in the environment that regenerates the README.
    """
    tree = ast.parse(module_file.read_text(encoding="utf-8"), filename=str(module_file))
    names = set()
    for node in tree.body:
        if not isinstance(node, ast.ClassDef):
            continue
        for dec in node.decorator_list:
            func = dec.func if isinstance(dec, ast.Call) else dec
            if (isinstance(func, ast.Attribute) and func.attr == "opt_in") or (
                    isinstance(func, ast.Name) and func.id == "opt_in"):
                names.add(node.name)
    return names


def count_tests() -> tuple[int, int, int]:
    """Returns (default_suite_tests, example_tests, opt_in_integration_tests).

    The default suite is everything ``unittest discover -s tests`` loads outside tests/examples/
    (stdlib only; optional PyYAML/jsonschema/claude-CLI cross-checks skip without them). The
    example suite is tests/examples/ (executes SKILL.md code, needs requirements-examples.txt).
    The third bucket is the tests/integration/ classes decorated with ``gate.opt_in``; each skips
    unless ML_AI_SKILLS_INTEGRATION_<NAME>=1. Undecorated integration classes, such as the gate's
    own stdlib self-tests in tests/integration/test_gate.py, run by default and are counted in the
    default suite. Counts are test methods as loaded, not outcomes of a run.
    """
    tests_dir = sl.REPO_ROOT / "tests"
    suite = unittest.defaultTestLoader.discover(str(tests_dir), pattern="test_*.py")
    counts = [0, 0, 0]
    gated: dict[str, set[str]] = {}

    def _is_opt_in(tid: str) -> bool:
        parts = tid.split(".")  # integration.<module>.<Class>.<method>
        if len(parts) < 4:
            return False
        mod_name, cls_name = parts[1], parts[2]
        if mod_name not in gated:
            path = tests_dir / "integration" / f"{mod_name}.py"
            gated[mod_name] = _opt_in_classes(path) if path.is_file() else set()
        return cls_name in gated[mod_name]

    def _walk(s) -> None:
        for item in s:
            if isinstance(item, unittest.TestSuite):
                _walk(item)
                continue
            tid = item.id()
            if tid.startswith("examples."):
                counts[1] += 1
            elif tid.startswith("integration.") and _is_opt_in(tid):
                counts[2] += 1
            else:
                counts[0] += 1

    _walk(suite)
    return counts[0], counts[1], counts[2]


def count_evals() -> tuple[int, int]:
    """Returns (case_file_count, distinct_skills_covered)."""
    evals_dir = sl.REPO_ROOT / "evals"
    files = list(evals_dir.glob("*/*.yaml"))
    covered = {f.parent.name for f in files}
    return len(files), len(covered)


def fmt_counter(counter: Counter, order: list[str] | None = None) -> str:
    keys = order if order else sorted(counter)
    return " · ".join(f"{k} ({counter.get(k, 0)})" for k in keys if counter.get(k, 0))


def render() -> str:
    skills = sl.load_all_skills()
    total = len(skills)
    type_counts = Counter(s.frontmatter.get("type") for s in skills)
    domain_counts = Counter(s.frontmatter.get("domain") for s in skills)
    level_counts = Counter(s.frontmatter.get("level") for s in skills)
    risk_counts = Counter(s.frontmatter.get("risk_level") for s in skills)
    evidence_counts = Counter(s.frontmatter.get("evidence_level") for s in skills)

    eval_files, eval_skills_covered = count_evals()
    default_tests, example_tests, integration_tests = count_tests()
    script_count = len(list((sl.REPO_ROOT / "scripts").glob("*.py")))
    docs_count = len(list((sl.REPO_ROOT / "docs").glob("*.md")))

    lines = [
        START,
        "<!-- Generated by `python scripts/generate_readme_stats.py --write`. Do not hand-edit",
        "     this block — your changes will be overwritten. Edit skill frontmatter, scripts/,",
        "     tests/, docs/, or evals/ instead, then regenerate. -->",
        "",
        "| Metric | Value |",
        "|---|---|",
        f"| Skills | **{total}** ({fmt_counter(type_counts, ['workflow', 'reference'])}) |",
        f"| Domains | {len(domain_counts)} — {fmt_counter(domain_counts, DOMAIN_ORDER)} |",
        f"| Level | {fmt_counter(level_counts, ['beginner', 'intermediate', 'advanced'])} |",
        f"| Risk level | {fmt_counter(risk_counts, ['low', 'medium', 'high', 'critical'])} |",
        f"| Evidence level | {fmt_counter(evidence_counts, ['primary', 'official-documentation', 'established-practice', 'heuristic', 'opinion'])} |",
        f"| Eval cases | {eval_files} case file(s) covering {eval_skills_covered}/{total} skills — graded offline against "
        "pass/fail fixtures by regex hard assertions; no model output graded (see `evals/SCHEMA.md`) |",
        f"| Unit tests | {default_tests} in the default suite (`tests/test_*.py` and the gate self-tests, stdlib only) + "
        f"{example_tests} optional example tests (`tests/examples/`, need `requirements-examples.txt`) + "
        f"{integration_tests} opt-in integration tests (`tests/integration/`, skipped unless "
        "`ML_AI_SKILLS_INTEGRATION_<NAME>=1`) |",
        f"| Scripts / docs | {script_count} scripts in `scripts/`, {docs_count} policy docs in `docs/` |",
        END,
    ]
    return "\n".join(lines)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true")
    ap.add_argument("--write", action="store_true")
    args = ap.parse_args()

    text = README.read_text(encoding="utf-8")
    if START not in text or END not in text:
        print(f"README.md is missing {START} / {END} markers")
        return 1

    pre, rest = text.split(START, 1)
    _, post = rest.split(END, 1)
    new_block = render()
    new_text = pre + new_block + post

    if args.write:
        README.write_text(new_text, encoding="utf-8", newline="\n")  # LF on every platform
        print("README.md PROJECT-STATS block updated")
        return 0

    if new_text != text:
        print("README.md PROJECT-STATS block is stale — run `python scripts/generate_readme_stats.py --write`")
        return 1
    print("README.md PROJECT-STATS block is up to date")
    return 0


if __name__ == "__main__":
    sys.exit(main())
