#!/usr/bin/env python3
"""Structural sanity check for evals/**/*.yaml — see evals/SCHEMA.md.

This does NOT run any model or grade any behavior. It only checks that
every eval case file is well-formed and points at a skill that actually
exists, so that a broken case file fails fast instead of silently being
skipped by whatever grades it later.

Usage:
    python scripts/validate_evals.py
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import skills_lib as sl  # noqa: E402

EVAL_CATEGORIES = ("happy_path", "edge_case", "adversarial", "misconception", "failure_recovery", "ambiguity")
EVALS_DIR = sl.REPO_ROOT / "evals"
REQUIRED_KEYS = ("skill", "category", "input", "expected_behavior")


def main() -> int:
    skills = sl.load_all_skills()
    valid_slugs = {s.slug for s in skills}

    case_files = sorted(EVALS_DIR.glob("*/*.yaml"))
    if not case_files:
        print("no eval case files found under evals/*/*.yaml")
        return 1

    errors: list[str] = []
    per_skill_count: dict[str, int] = {}

    for f in case_files:
        text = f.read_text(encoding="utf-8")
        try:
            fm = sl.parse_frontmatter(text)
        except Exception as e:  # noqa: BLE001
            errors.append(f"{f}: could not parse — {e}")
            continue

        for key in REQUIRED_KEYS:
            if key not in fm or fm[key] in (None, "", []):
                errors.append(f"{f}: missing required field '{key}'")

        skill = fm.get("skill")
        if skill and skill not in valid_slugs:
            errors.append(f"{f}: skill '{skill}' does not exist as a folder")
        elif skill:
            per_skill_count[skill] = per_skill_count.get(skill, 0) + 1

        category = fm.get("category")
        if category and category not in EVAL_CATEGORIES:
            errors.append(f"{f}: category '{category}' not in {EVAL_CATEGORIES}")

        expected = fm.get("expected_behavior")
        if isinstance(expected, list) and len(expected) == 0:
            errors.append(f"{f}: expected_behavior list is empty")

        # directory name must match the `skill:` field (keeps evals/ browsable
        # by skill without relying on the YAML body)
        if skill and f.parent.name != skill:
            errors.append(f"{f}: lives under evals/{f.parent.name}/ but declares skill: {skill}")

    if errors:
        for e in errors:
            print(f"EVAL ERROR: {e}")
        print(f"\n{len(errors)} error(s) across {len(case_files)} case file(s)")
        return 1

    covered_skills = sorted(per_skill_count)
    print(f"{len(case_files)} eval case(s) OK, covering {len(covered_skills)}/{len(skills)} skills: "
          f"{', '.join(covered_skills)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
