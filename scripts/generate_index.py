#!/usr/bin/env python3
"""Keep INDEX.md honest relative to the actual skill folders on disk.

INDEX.md's hand-written one-line descriptions are curated content (not
boilerplate), so this script does NOT fully regenerate the file from
scratch — that would throw away real authoring work for no reason (see
CLAUDE.md: "do not rewrite content solely for stylistic reasons"). Instead
it treats INDEX.md's per-item description text as source-of-truth prose and
the skill folders' frontmatter as source-of-truth structure, and:

  --check   reports every place the two disagree (missing entry, wrong
            checkbox, wrong [W]/[R] tag, wrong progress counter) and exits 1
            if anything disagrees. This is what CI runs.
  --fix     applies the mechanical fixes (checkbox / tag / progress counter)
            and appends a new bullet (using the skill's frontmatter
            `description`, first clause only) for any skill folder that has
            no INDEX.md entry at all under its domain's heading.

Usage:
    python scripts/generate_index.py --check
    python scripts/generate_index.py --fix
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import skills_lib as sl  # noqa: E402

ENTRY_RE = re.compile(r"^- (✅|⬜) \[(W|R)\] `([a-z0-9-]+)` — (.+)$")
PROGRESS_RE = re.compile(r"^\*\*Progress:\*\* (\d+) / (\d+) written\.(.*)$")
HEADING_RE = re.compile(r"^## \d+\.\s+(.+)$")

# Domain slug -> the exact INDEX.md section heading text used for it today.
DOMAIN_HEADING = {
    "foundations": "Foundations",
    "classical-ml": "Classical ML",
    "deep-learning": "Deep Learning",
    "llm": "LLMs & Generative AI",
    "specialized": "Specialized Domains",
    "mlops": "MLOps & Production",
    "responsible-ai": "Responsible AI",
    "security": "AI/ML Security",
}
TYPE_TAG = {"workflow": "W", "reference": "R"}


def parse_index(index_path: Path) -> tuple[list[str], dict[int, dict]]:
    """Return (lines, {line_no: {checkbox,tag,slug,desc}}) for every entry line."""
    lines = index_path.read_text(encoding="utf-8").split("\n")
    entries = {}
    for i, line in enumerate(lines):
        m = ENTRY_RE.match(line)
        if m:
            entries[i] = {"checkbox": m.group(1), "tag": m.group(2), "slug": m.group(3), "desc": m.group(4)}
    return lines, entries


def check_index_consistency(skills: list[sl.Skill], index_path: Path) -> list[str]:
    issues: list[str] = []
    if not index_path.exists():
        return [f"{index_path.name} does not exist"]

    lines, entries = parse_index(index_path)
    by_slug = sl.by_slug(skills)
    entry_slugs = {e["slug"] for e in entries.values()}

    for slug, skill in by_slug.items():
        if slug not in entry_slugs:
            issues.append(f"skill '{slug}' has no INDEX.md entry")

    for i, e in entries.items():
        slug = e["slug"]
        skill = by_slug.get(slug)
        if skill is None:
            issues.append(f"INDEX.md line {i + 1} references '{slug}' which has no skill folder")
            continue
        expected_tag = TYPE_TAG.get(skill.frontmatter.get("type"))
        if expected_tag and e["tag"] != expected_tag:
            issues.append(f"'{slug}' INDEX.md tag is [{e['tag']}] but frontmatter type is "
                           f"{skill.frontmatter.get('type')} (expected [{expected_tag}])")
        if e["checkbox"] != "✅":
            issues.append(f"'{slug}' exists on disk but INDEX.md checkbox is {e['checkbox']} (expected ✅)")

    actual_done = sum(1 for e in entries.values() if e["slug"] in by_slug)
    actual_total = len(entries)
    for i, line in enumerate(lines):
        m = PROGRESS_RE.match(line)
        if m:
            stated_done, stated_total = int(m.group(1)), int(m.group(2))
            if (stated_done, stated_total) != (actual_done, actual_total):
                issues.append(f"progress counter says {stated_done}/{stated_total} but actual is "
                               f"{actual_done}/{actual_total}")
    return issues


def apply_fix(skills: list[sl.Skill], index_path: Path) -> None:
    lines, entries = parse_index(index_path)
    by_slug = sl.by_slug(skills)

    for i, e in list(entries.items()):
        skill = by_slug.get(e["slug"])
        if skill is None:
            continue
        expected_tag = TYPE_TAG.get(skill.frontmatter.get("type"), e["tag"])
        lines[i] = f"- ✅ [{expected_tag}] `{e['slug']}` — {e['desc']}"

    # re-parse after in-place edits to recompute counts / find missing entries
    _, entries = parse_index_from_lines(lines)
    entry_slugs = {e["slug"] for e in entries.values()}
    missing = [s for s in skills if s.slug not in entry_slugs]

    if missing:
        # group missing skills by domain, append under the matching heading
        # (or a new one at the end if that heading doesn't exist yet)
        heading_line_idx = {}
        for i, line in enumerate(lines):
            m = HEADING_RE.match(line)
            if m:
                heading_line_idx[m.group(1)] = i

        by_domain: dict[str, list[sl.Skill]] = {}
        for s in missing:
            by_domain.setdefault(s.frontmatter.get("domain", "specialized"), []).append(s)

        for domain, skill_list in by_domain.items():
            heading_text = DOMAIN_HEADING.get(domain, domain)
            if heading_text in heading_line_idx:
                insert_at = heading_line_idx[heading_text] + 1
                while insert_at < len(lines) and lines[insert_at].strip() and not lines[insert_at].startswith("##"):
                    insert_at += 1
                new_bullets = [
                    f"- ✅ [{TYPE_TAG.get(s.frontmatter.get('type'), 'W')}] `{s.slug}` — "
                    f"{first_clause(s.frontmatter.get('description', ''))}"
                    for s in skill_list
                ]
                lines[insert_at:insert_at] = new_bullets
            else:
                next_num = max([int(re.match(r'^## (\d+)\.', l).group(1)) for l in lines if re.match(r'^## \d+\.', l)] or [0]) + 1
                new_section = ["", f"## {next_num}. {heading_text}"]
                for s in skill_list:
                    new_section.append(f"- ✅ [{TYPE_TAG.get(s.frontmatter.get('type'), 'W')}] `{s.slug}` — "
                                        f"{first_clause(s.frontmatter.get('description', ''))}")
                # insert BEFORE the trailing `---` / Progress footer, not at the
                # absolute end of the file, so new domain sections land with the
                # other domain sections instead of after the progress line.
                footer_idx = next((i for i, l in enumerate(lines) if PROGRESS_RE.match(l)), len(lines))
                sep_idx = next((i for i in range(footer_idx - 1, -1, -1) if lines[i].strip() == "---"), footer_idx)
                lines[sep_idx:sep_idx] = new_section + [""]

    _, entries = parse_index_from_lines(lines)
    total = len(entries)
    done = sum(1 for e in entries.values() if e["slug"] in by_slug)
    for i, line in enumerate(lines):
        if PROGRESS_RE.match(line):
            lines[i] = f"**Progress:** {done} / {total} written. Clone `_TEMPLATE/SKILL.md` to fill any ⬜ item."

    index_path.write_text("\n".join(lines), encoding="utf-8")


def parse_index_from_lines(lines: list[str]) -> tuple[list[str], dict[int, dict]]:
    entries = {}
    for i, line in enumerate(lines):
        m = ENTRY_RE.match(line)
        if m:
            entries[i] = {"checkbox": m.group(1), "tag": m.group(2), "slug": m.group(3), "desc": m.group(4)}
    return lines, entries


def first_clause(description: str) -> str:
    desc = description.strip()
    m = re.search(r"Use when the user wants to (.+?)\.", desc)
    return m.group(1) if m else (desc[:80] + "...")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true")
    ap.add_argument("--fix", action="store_true")
    args = ap.parse_args()

    skills = sl.load_all_skills()
    index_path = sl.REPO_ROOT / "INDEX.md"

    if args.fix:
        apply_fix(skills, index_path)
        print("INDEX.md updated")
        return 0

    issues = check_index_consistency(skills, index_path)
    if issues:
        for issue in issues:
            print(f"INDEX DRIFT: {issue}")
        return 1
    print("INDEX.md is consistent with skill folders")
    return 0


if __name__ == "__main__":
    sys.exit(main())
