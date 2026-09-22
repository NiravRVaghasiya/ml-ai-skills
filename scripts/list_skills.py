#!/usr/bin/env python3
"""List skills — either the source library, or what's installed at a target.

Two modes:
  python scripts/list_skills.py                 # every skill in this repo, grouped by domain
  python scripts/list_skills.py --target DIR     # skills installed at DIR per its manifest

Dependency-free (stdlib + skills_lib), matching the rest of scripts/.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import skills_lib as sl  # noqa: E402

MANIFEST_NAME = ".ml-ai-skills-manifest"

DOMAIN_ORDER = (
    "foundations", "classical-ml", "deep-learning", "llm",
    "specialized", "mlops", "responsible-ai", "security",
)
TYPE_TAG = {"workflow": "W", "reference": "R"}


def list_source() -> int:
    skills = sl.load_all_skills()
    by_domain: dict[str, list[sl.Skill]] = {}
    for s in skills:
        by_domain.setdefault(s.frontmatter.get("domain", "specialized"), []).append(s)

    print(f"{len(skills)} skill(s) in this repo:\n")
    for domain in DOMAIN_ORDER:
        group = sorted(by_domain.get(domain, []), key=lambda s: s.slug)
        if not group:
            continue
        print(f"## {domain}")
        for s in group:
            tag = TYPE_TAG.get(s.frontmatter.get("type"), "?")
            print(f"  [{tag}] {s.slug} — {s.frontmatter.get('display_name', s.slug)}")
        print()
    return 0


def _read_manifest(manifest_path: Path) -> tuple[list[str], str | None]:
    """Plain-text manifest: '#'-prefixed lines are metadata comments, the
    rest are managed skill slugs, one per line. Mirrors install.sh/uninstall.sh."""
    slugs = []
    source = None
    for line in manifest_path.read_text(encoding="utf-8").splitlines():
        if line.startswith("# source:"):
            source = line.split(":", 1)[1].strip()
        elif line.startswith("#") or not line.strip():
            continue
        else:
            slugs.append(line.strip())
    return sorted(slugs), source


def list_installed(target: Path) -> int:
    manifest_path = target / MANIFEST_NAME
    if not manifest_path.exists():
        print(f"No ml-ai-skills manifest found at {manifest_path}")
        print("(nothing installed here by install.sh, or wrong --target)")
        return 1

    slugs, source = _read_manifest(manifest_path)
    print(f"{len(slugs)} skill(s) installed at {target} (source: {source or 'unknown'}):\n")
    for slug in slugs:
        skill_md = target / slug / "SKILL.md"
        if not skill_md.exists():
            print(f"  [MISSING] {slug} — listed in manifest but SKILL.md not found on disk")
            continue
        try:
            skill = sl.load_skill(target / slug)
            tag = TYPE_TAG.get(skill.frontmatter.get("type"), "?")
            print(f"  [{tag}] {slug} — {skill.frontmatter.get('display_name', slug)}")
        except sl.FrontmatterError as e:
            print(f"  [INVALID] {slug} — {e}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--target", help="Show installed skills at this directory instead of the source repo")
    args = ap.parse_args()

    if args.target:
        return list_installed(Path(args.target).expanduser())
    return list_source()


if __name__ == "__main__":
    sys.exit(main())
