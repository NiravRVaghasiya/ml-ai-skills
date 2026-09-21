"""Shared test fixtures. Not a test module itself (no Test* classes)."""
from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts"))
sys.path.insert(0, str(REPO_ROOT / "schema"))

import skills_lib as sl  # noqa: E402


def make_skill(slug: str, body: str = "", **fm_overrides) -> sl.Skill:
    fm = {
        "name": slug,
        "display_name": slug.replace("-", " ").title(),
        "description": f'Use when the user wants to do {slug} things. Trigger phrases: "a", "b", "c". NOT for other things (see other-skill).',
        "type": "workflow",
        "domain": "specialized",
        "level": "intermediate",
        "lifecycle": "stable",
        "risk_level": "low",
        "evidence_level": "established-practice",
        "last_verified": "2026-01-01",
        "capabilities": [],
        "requires": [],
        "conflicts": [],
        "related": [],
        "inputs": "n/a",
        "outputs": "n/a",
    }
    fm.update(fm_overrides)
    default_body = (
        "\n## Overview\nSome overview.\n\n## Workflow\n1. **Step** — do it.\n```python\nprint('ok')\n```\n\n"
        "## Gotchas\n- one\n- two\n- three\n\n## References\n- [a](https://example.com/a) — reason\n"
        "- [b](https://example.com/b) — reason\n"
    )
    body = body or default_body
    raw = "---\n" + "".join(f"{k}: {v}\n" for k, v in fm.items() if not isinstance(v, list)) + "---\n" + body
    return sl.Skill(slug=slug, path=Path(f"/tmp/{slug}/SKILL.md"), frontmatter=fm, body=body, raw=raw)
