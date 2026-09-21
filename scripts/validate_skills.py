#!/usr/bin/env python3
"""Validate every skill folder against the formal spec in docs/SKILL-SPEC.md.

Usage:
    python scripts/validate_skills.py                # errors only, exit 1 if any
    python scripts/validate_skills.py --warnings      # also print warnings
    python scripts/validate_skills.py --strict        # warnings also fail the run
    python scripts/validate_skills.py --check-links   # live HTTP check of References
                                                       # URLs (network-dependent, slow,
                                                       # NOT run in the default CI job)
    python scripts/validate_skills.py --json          # machine-readable output

This script is deliberately conservative about what counts as a hard ERROR
vs. a WARNING: things the spec makes non-negotiable (required frontmatter
keys, enum values, required sections, minimum gotcha/reference counts,
existing dependency slugs, no cycles) are ERRORs. Things that are useful
signals but have real false-positive rates given a hand-rolled Markdown/YAML
parser (Python-syntax-looking-wrong on an intentionally partial snippet,
missing code fence on a step that legitimately continues the previous one)
are WARNINGs. See docs/SKILL-SPEC.md "Validation philosophy".
"""
from __future__ import annotations

import argparse
import ast
import json
import re
import sys
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "schema"))

import allowed_values as av  # noqa: E402
import skills_lib as sl  # noqa: E402
import generate_index as gi  # noqa: E402
import build_dependency_graph as bdg  # noqa: E402

SLUG_RE = re.compile(r"^[a-z0-9]+(-[a-z0-9]+)*$")


class Report:
    def __init__(self):
        self.errors: list[dict] = []
        self.warnings: list[dict] = []

    def error(self, skill: str, rule: str, **extra):
        self.errors.append({"skill": skill, "rule": rule, **extra})

    def warning(self, skill: str, rule: str, **extra):
        self.warnings.append({"skill": skill, "rule": rule, **extra})

    def print_human(self, show_warnings: bool):
        for e in self.errors:
            print("ERROR:")
            print(f"  skill: {e['skill']}")
            print(f"  rule: {e['rule']}")
            for k, v in e.items():
                if k in ("skill", "rule"):
                    continue
                print(f"  {k}: {v}")
            print()
        if show_warnings:
            for w in self.warnings:
                print("WARNING:")
                print(f"  skill: {w['skill']}")
                print(f"  rule: {w['rule']}")
                for k, v in w.items():
                    if k in ("skill", "rule"):
                        continue
                    print(f"  {k}: {v}")
                print()
        print(f"--- {len(self.errors)} error(s), {len(self.warnings)} warning(s) ---")


def check_frontmatter(skill: sl.Skill, report: Report) -> None:
    fm = skill.frontmatter

    for key in av.REQUIRED_KEYS:
        if key not in fm:
            report.error(skill.slug, "missing required metadata", field=key)
        elif key not in av.LIST_KEYS and fm[key] in (None, ""):
            # list-type keys (capabilities/requires/conflicts/related) are allowed to be
            # an empty list — `key:` with no items parses to None, which is valid ("no
            # dependencies"), not "field omitted". Only non-list scalar keys must be non-empty.
            report.error(skill.slug, "missing required metadata", field=key)

    if fm.get("name") and fm["name"] != skill.slug:
        report.error(skill.slug, "name != folder name", name=fm.get("name"), folder=skill.slug)

    if not SLUG_RE.match(skill.slug):
        report.error(skill.slug, "invalid directory naming (must be lowercase-kebab)")

    enum_checks = [
        ("type", av.TYPE), ("domain", av.DOMAIN), ("level", av.LEVEL),
        ("lifecycle", av.LIFECYCLE), ("risk_level", av.RISK_LEVEL),
        ("evidence_level", av.EVIDENCE_LEVEL),
    ]
    for field, allowed in enum_checks:
        val = fm.get(field)
        if val is not None and val not in allowed:
            report.error(skill.slug, "invalid metadata value", field=field, value=val, allowed=list(allowed))

    lv = fm.get("last_verified")
    if lv and not re.match(r"^\d{4}-\d{2}-\d{2}$", str(lv)):
        report.error(skill.slug, "last_verified is not YYYY-MM-DD", value=lv)

    for key in av.LIST_KEYS:
        if key in fm and fm[key] is not None and not isinstance(fm[key], list):
            report.error(skill.slug, "field must be a YAML list", field=key)

    desc = fm.get("description", "") or ""
    if "not for" not in desc.lower():
        report.error(skill.slug, "description missing 'NOT for ...' clause")
    quote_pairs = desc.count('"') // 2
    if quote_pairs < 3:
        report.error(skill.slug, "description has fewer than 3 quoted trigger phrases", found=quote_pairs)


def check_sections(skill: sl.Skill, report: Report) -> None:
    headers = skill.headers()
    t = skill.frontmatter.get("type")
    has_overview = "Overview" in headers
    has_workflow = "Workflow" in headers
    has_key_concepts = "Key Concepts" in headers
    has_gotchas = "Gotchas" in headers
    has_refs = "References" in headers

    if not has_overview:
        report.error(skill.slug, "missing required section", section="Overview")
    if not has_gotchas:
        report.error(skill.slug, "missing required section", section="Gotchas")
    if not has_refs:
        report.error(skill.slug, "missing required section", section="References")

    if t == "workflow":
        if not has_workflow:
            report.error(skill.slug, "workflow skill missing ## Workflow section")
        if has_key_concepts:
            report.error(skill.slug, "workflow skill must not have ## Key Concepts (that's the reference-skill contract)")
    elif t == "reference":
        if not has_key_concepts:
            report.error(skill.slug, "reference skill missing ## Key Concepts section")
        if has_workflow:
            report.error(skill.slug, "reference skill must not have ## Workflow (that's the workflow-skill contract)")

    gotchas = skill.bullets_under("Gotchas")
    if len(gotchas) < 3:
        report.error(skill.slug, "fewer than 3 gotchas", found=len(gotchas))

    refs = skill.bullets_under("References")
    if len(refs) < 2:
        report.error(skill.slug, "fewer than 2 references", found=len(refs))

    if t == "workflow":
        code_blocks = skill.code_blocks(lang=None)
        if len(code_blocks) == 0:
            report.error(skill.slug, "workflow skill has zero code blocks")
        step_count = len(re.findall(r"^\d+\.\s+\*\*", skill.body, flags=re.MULTILINE))
        if step_count and len(code_blocks) < step_count:
            report.warning(skill.slug, "fewer code blocks than numbered steps (some steps may lack runnable code)",
                            steps=step_count, code_blocks=len(code_blocks))


def check_code_fences(skill: sl.Skill, report: Report) -> None:
    fence_count = skill.body.count("```")
    if fence_count % 2 != 0:
        report.error(skill.slug, "unbalanced ``` code fence (odd count)", count=fence_count)

    for lang, code in skill.code_blocks(lang="python"):
        try:
            ast.parse(code)
        except SyntaxError as e:
            report.warning(skill.slug, "python code block fails ast.parse (may be an intentionally partial snippet)",
                            error=str(e))


def check_links(skill: sl.Skill, report: Report, live: bool) -> None:
    for text, url in skill.markdown_links():
        if url.startswith("http://") or url.startswith("https://"):
            if not live:
                continue
            try:
                req = urllib.request.Request(url, method="HEAD", headers={"User-Agent": "skill-validator/1.0"})
                urllib.request.urlopen(req, timeout=6)
            except Exception as e:  # noqa: BLE001 - any failure is a WARNING, not fatal
                report.warning(skill.slug, "reference URL unreachable (checked live)", url=url, error=str(e))
        else:
            # internal-looking relative link — must resolve within the repo
            target = (skill.path.parent / url).resolve()
            if not target.exists():
                report.error(skill.slug, "broken internal link", url=url)


def check_relationships(skills: list[sl.Skill], report: Report) -> None:
    slugs = {s.slug for s in skills}
    seen_names: dict[str, str] = {}
    for s in skills:
        name = s.frontmatter.get("name")
        if name in seen_names and seen_names[name] != s.slug:
            report.error(s.slug, "duplicate skill name", name=name, also_in=seen_names[name])
        elif name:
            seen_names[name] = s.slug

        for field in ("related", "requires", "conflicts"):
            for target in (s.frontmatter.get(field) or []):
                if target not in slugs:
                    report.error(s.slug, f"'{field}' references unknown skill", target=target)

    cycles = bdg.find_cycles(skills)
    for cyc in cycles:
        report.error(cyc[0], "dependency cycle in 'requires' graph", cycle=" -> ".join(cyc))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--warnings", action="store_true", help="also print warnings")
    ap.add_argument("--strict", action="store_true", help="warnings also cause a non-zero exit")
    ap.add_argument("--check-links", action="store_true", help="live-check https:// reference URLs (network required)")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    report = Report()
    try:
        skills = sl.load_all_skills()
    except sl.FrontmatterError as e:
        report.error("<repo>", "malformed frontmatter", error=str(e))
        skills = []

    for skill in skills:
        check_frontmatter(skill, report)
        check_sections(skill, report)
        check_code_fences(skill, report)
        check_links(skill, report, live=args.check_links)

    check_relationships(skills, report)

    index_issues = gi.check_index_consistency(skills, sl.REPO_ROOT / "INDEX.md")
    for issue in index_issues:
        report.error("INDEX.md", issue)

    if args.json:
        print(json.dumps({"errors": report.errors, "warnings": report.warnings}, indent=2))
    else:
        report.print_human(show_warnings=args.warnings or args.strict)

    if report.errors:
        return 1
    if args.strict and report.warnings:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
