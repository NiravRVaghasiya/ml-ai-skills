#!/usr/bin/env python3
"""Generate the native Claude Code plugin/marketplace packaging from the
canonical `<slug>/SKILL.md` folders at repo root.

This is a *packaging* step, not a content-authoring one: every SKILL.md byte
under `plugins/**/skills/*/SKILL.md` is copied verbatim from its canonical
`<slug>/SKILL.md`. There is exactly one place to edit a skill's content (the
repo-root folder); this script only decides which plugin(s) each skill ships
in and writes the `.claude-plugin/plugin.json` / `.claude-plugin/marketplace.json`
manifests required by Claude Code's plugin/marketplace conventions (verified
live against the installed `claude` CLI's `plugin validate`/`plugin marketplace
add`/`plugin install` commands — see PR description for the transcript).

Packaging plan:
  - one all-in-one plugin `ml-ai-skills` containing every skill
  - one plugin per `domain` in schema/allowed_values.py DOMAIN, named after
    the domain slug (e.g. `classical-ml`), containing only that domain's skills

Why generated copies instead of symlinks: `claude plugin validate` rejects any
`plugin.json` `skills` path containing `..` (path traversal guard), so a
plugin cannot point at a shared directory outside its own folder — there is
no native "reference, don't copy" mechanism. Generation keeps a single
source of truth (this script + the canonical skill folders) while still
producing real, self-contained plugin directories that work when GitHub
serves the repo to `/plugin marketplace add` with no build step.

Usage:
    python scripts/generate_plugins.py --check   # exit 1 if plugins/ or
                                                   # .claude-plugin/ are stale (CI)
    python scripts/generate_plugins.py --write    # regenerate them in place
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "schema"))

import allowed_values as av  # noqa: E402
import skills_lib as sl  # noqa: E402
from generate_readme_stats import DOMAIN_LABEL, DOMAIN_ORDER  # noqa: E402

ALL_PLUGIN_NAME = "ml-ai-skills"
MARKETPLACE_NAME = "ml-ai-skills"
REPO_URL = "https://github.com/NiravRVaghasiya/ml-ai-skills"
AUTHOR = {"name": "Nirav Vaghasiya", "url": "https://github.com/NiravRVaghasiya"}
LICENSE = "MIT"

PLUGIN_SCHEMA = "https://anthropic.com/claude-code/plugin.schema.json"
MARKETPLACE_SCHEMA = "https://anthropic.com/claude-code/marketplace.schema.json"

PLUGINS_DIR = sl.REPO_ROOT / "plugins"
MARKETPLACE_PATH = sl.REPO_ROOT / ".claude-plugin" / "marketplace.json"


def read_version() -> str:
    return (sl.REPO_ROOT / "VERSION").read_text(encoding="utf-8").strip()


def build_plan(skills: list[sl.Skill]) -> "dict[str, dict]":
    """Return {plugin_name: {"description": str, "slugs": [sorted slugs]}}."""
    by_domain: dict[str, list[str]] = {d: [] for d in av.DOMAIN}
    for s in skills:
        domain = s.frontmatter.get("domain")
        if domain in by_domain:
            by_domain[domain].append(s.slug)
    for d in by_domain:
        by_domain[d].sort()

    all_slugs = sorted(s.slug for s in skills)
    plan: dict[str, dict] = {
        ALL_PLUGIN_NAME: {
            "description": (
                f"All {len(all_slugs)} ML/AI Agent Skills from the ml-ai-skills library, "
                f"covering {', '.join(DOMAIN_LABEL[d] for d in DOMAIN_ORDER)}."
            ),
            "slugs": all_slugs,
        }
    }
    for d in DOMAIN_ORDER:
        slugs = by_domain.get(d, [])
        plan[d] = {
            "description": (
                f"{len(slugs)} {DOMAIN_LABEL[d]} Agent Skill(s) from the ml-ai-skills library: "
                f"{', '.join(slugs) if slugs else '(none yet)'}."
            ),
            "slugs": slugs,
        }
    return plan


def render_plugin_json(name: str, description: str, version: str) -> dict:
    return {
        "$schema": PLUGIN_SCHEMA,
        "name": name,
        "version": version,
        "description": description,
        "author": AUTHOR,
        "homepage": REPO_URL,
        "repository": REPO_URL,
        "license": LICENSE,
        "keywords": ["machine-learning", "ai", "claude-code-skills"] + ([] if name == ALL_PLUGIN_NAME else [name]),
    }


def render_marketplace_json(plan: "dict[str, dict]", version: str) -> dict:
    plugins = []
    for name in [ALL_PLUGIN_NAME, *DOMAIN_ORDER]:
        plugins.append({
            "name": name,
            "source": f"./plugins/{name}",
            "description": plan[name]["description"],
            "version": version,
        })
    return {
        "$schema": MARKETPLACE_SCHEMA,
        "name": MARKETPLACE_NAME,
        "owner": AUTHOR,
        "metadata": {
            "description": "38 self-contained ML/AI Agent Skills, packaged as one all-in-one plugin and one plugin per domain.",
            "version": version,
        },
        "plugins": plugins,
    }


def render_files(skills: list[sl.Skill]) -> "dict[str, bytes]":
    """Return {relative_posix_path: exact file bytes} for every generated file."""
    version = read_version()
    plan = build_plan(skills)
    by_slug = sl.by_slug(skills)
    files: dict[str, bytes] = {}

    files[".claude-plugin/marketplace.json"] = (
        json.dumps(render_marketplace_json(plan, version), indent=2) + "\n"
    ).encode("utf-8")

    for name, entry in plan.items():
        plugin_json = render_plugin_json(name, entry["description"], version)
        files[f"plugins/{name}/.claude-plugin/plugin.json"] = (
            json.dumps(plugin_json, indent=2) + "\n"
        ).encode("utf-8")
        for slug in entry["slugs"]:
            files[f"plugins/{name}/skills/{slug}/SKILL.md"] = by_slug[slug].raw.encode("utf-8")

    return files


def check(skills: list[sl.Skill]) -> list[str]:
    issues: list[str] = []
    expected = render_files(skills)

    actual: dict[str, bytes] = {}
    if MARKETPLACE_PATH.exists():
        actual[".claude-plugin/marketplace.json"] = MARKETPLACE_PATH.read_bytes()
    if PLUGINS_DIR.exists():
        for path in sorted(PLUGINS_DIR.rglob("*")):
            if path.is_file():
                actual[path.relative_to(sl.REPO_ROOT).as_posix()] = path.read_bytes()

    for rel, content in expected.items():
        if rel not in actual:
            issues.append(f"missing generated file: {rel}")
        elif actual[rel] != content:
            issues.append(f"stale generated file (content drift): {rel}")
    for rel in actual:
        if rel not in expected:
            issues.append(f"unexpected file under plugins/ or .claude-plugin/ not produced by the generator: {rel}")

    return issues


def write(skills: list[sl.Skill]) -> None:
    import shutil

    if PLUGINS_DIR.exists():
        shutil.rmtree(PLUGINS_DIR)

    for rel, content in render_files(skills).items():
        path = sl.REPO_ROOT / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true")
    ap.add_argument("--write", action="store_true")
    args = ap.parse_args()

    skills = sl.load_all_skills()

    if args.write:
        write(skills)
        print("plugins/ and .claude-plugin/marketplace.json regenerated")
        return 0

    issues = check(skills)
    if issues:
        for issue in issues:
            print(f"PLUGIN DRIFT: {issue}")
        print(f"\nRun `python scripts/generate_plugins.py --write` to fix ({len(issues)} issue(s)).")
        return 1
    print("plugins/ and .claude-plugin/marketplace.json are up to date")
    return 0


if __name__ == "__main__":
    sys.exit(main())
