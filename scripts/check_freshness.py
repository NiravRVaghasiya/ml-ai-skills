#!/usr/bin/env python3
"""Best-effort staleness scanner.

This CANNOT prove a skill's code is currently correct — it has no network
access to package registries or vendor docs. It only pattern-matches for
things that are *known, at authoring time, to already be gone or renamed*
(e.g. `sklearn.cross_validation`, `openai.Completion.create`), plus two
purely mechanical signals: (a) a `last_verified` date older than
STALE_AFTER_DAYS, and (b) Python code that imports a package but declares no
`version_constraints`. All three are FLAGS FOR HUMAN REVIEW, not proof of
breakage or proof of correctness. See docs/FRESHNESS.md.

Usage:
    python scripts/check_freshness.py                # human-readable report
    python scripts/check_freshness.py --json
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import skills_lib as sl  # noqa: E402

STALE_AFTER_DAYS = 180

# (regex, message) — every entry here is something that is *known* removed
# or renamed as of this repo's authoring; do not add speculative entries.
KNOWN_STALE_PATTERNS: list[tuple[re.Pattern, str]] = [
    (re.compile(r"sklearn\.cross_validation"), "sklearn.cross_validation was removed in scikit-learn 0.20; use sklearn.model_selection"),
    (re.compile(r"from sklearn\.preprocessing import Imputer\b"), "sklearn.preprocessing.Imputer was renamed to SimpleImputer in scikit-learn 0.20"),
    (re.compile(r"sklearn\.externals\.joblib"), "sklearn.externals.joblib was removed; import joblib directly"),
    (re.compile(r"openai\.Completion\.create"), "openai.Completion.create is the pre-1.0 openai-python API; v1+ uses client.chat.completions.create"),
    (re.compile(r"openai\.ChatCompletion\.create"), "openai.ChatCompletion.create is the pre-1.0 openai-python API; v1+ uses client.chat.completions.create"),
    (re.compile(r"tensorflow\.contrib"), "tf.contrib was removed in TensorFlow 2.x"),
    (re.compile(r"text-davinci-\d+"), "text-davinci-* completion models are retired; verify current model availability against vendor docs"),
    (re.compile(r"\bgpt-3\.5-turbo-\d{4}\b"), "pinned dated GPT-3.5 snapshot — check whether this snapshot is still served before relying on it"),
    (re.compile(r"\bgpt-4-\d{4}\b"), "pinned dated GPT-4 snapshot — check whether this snapshot is still served before relying on it"),
]

IMPORT_RE = re.compile(r"^\s*(?:import|from)\s+([a-zA-Z0-9_]+)", re.MULTILINE)
# stdlib-ish modules that don't need a version_constraints entry
IGNORE_MODULES = {
    "os", "sys", "re", "json", "math", "random", "datetime", "pathlib",
    "typing", "collections", "itertools", "functools", "argparse", "logging",
    "dataclasses", "unittest", "time", "abc", "copy",
}


def check_skill(skill: sl.Skill, today: dt.date) -> list[dict]:
    findings = []

    for pattern, message in KNOWN_STALE_PATTERNS:
        if pattern.search(skill.raw):
            findings.append({"skill": skill.slug, "level": "STALE", "message": message})

    lv = skill.frontmatter.get("last_verified")
    if lv:
        try:
            lv_date = dt.date.fromisoformat(lv)
            age = (today - lv_date).days
            if age > STALE_AFTER_DAYS:
                findings.append({"skill": skill.slug, "level": "REVIEW_DUE",
                                  "message": f"last_verified is {age} days old (threshold {STALE_AFTER_DAYS})"})
        except ValueError:
            findings.append({"skill": skill.slug, "level": "STALE", "message": f"last_verified '{lv}' is not a valid date"})
    else:
        findings.append({"skill": skill.slug, "level": "REVIEW_DUE", "message": "no last_verified date set"})

    py_blocks = skill.code_blocks(lang="python")
    if py_blocks:
        modules = set()
        for _, code in py_blocks:
            modules.update(m for m in IMPORT_RE.findall(code) if m not in IGNORE_MODULES)
        if modules and not skill.frontmatter.get("version_constraints"):
            findings.append({"skill": skill.slug, "level": "REVIEW_DUE",
                              "message": f"imports {sorted(modules)} but declares no version_constraints"})

    return findings


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    today = dt.date.today()
    skills = sl.load_all_skills()
    all_findings = []
    for s in skills:
        all_findings.extend(check_skill(s, today))

    if args.json:
        print(json.dumps(all_findings, indent=2))
    else:
        stale = [f for f in all_findings if f["level"] == "STALE"]
        review = [f for f in all_findings if f["level"] == "REVIEW_DUE"]
        for f in stale:
            print(f"STALE      {f['skill']:28s} {f['message']}")
        for f in review:
            print(f"REVIEW_DUE {f['skill']:28s} {f['message']}")
        print(f"\n{len(stale)} known-stale finding(s), {len(review)} review-due finding(s) across {len(skills)} skills")
        print("Note: absence of findings means 'nothing matched known patterns', NOT 'verified current'.")

    # STALE findings (known-broken patterns) fail CI; REVIEW_DUE is informational only.
    return 1 if any(f["level"] == "STALE" for f in all_findings) else 0


if __name__ == "__main__":
    sys.exit(main())
