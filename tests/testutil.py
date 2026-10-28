"""Shared test fixtures. Not a test module itself (no Test* classes)."""
from __future__ import annotations

import re
import shutil
import subprocess
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


# --------------------------------------------------------------------------
# Fixture trees for validator tests (tests/test_validator_mutations.py)
# --------------------------------------------------------------------------

def write_minimal_index(root: Path) -> None:
    """Write an INDEX.md listing exactly the skill folders under `root`.

    Generated rather than copied so a fixture tree never inherits drift from
    the real INDEX.md while other work is in flight.
    """
    lines = ["# Fixture index", ""]
    for d in sl.iter_skill_dirs(root):
        skill = sl.load_skill(d)
        tag = {"workflow": "W", "reference": "R"}.get(skill.frontmatter.get("type"), "W")
        lines.append(f"- ✅ [{tag}] `{d.name}` — fixture entry")
    (root / "INDEX.md").write_text("\n".join(lines) + "\n", encoding="utf-8", newline="\n")


def copy_skill_tree(dest: Path, src_root: Path = REPO_ROOT) -> Path:
    """Copy every real skill folder into `dest` and give it a matching INDEX.md."""
    dest.mkdir(parents=True, exist_ok=True)
    for d in sl.iter_skill_dirs(src_root):
        shutil.copytree(d, dest / d.name, ignore=shutil.ignore_patterns("__pycache__"))
    write_minimal_index(dest)
    return dest


def read_skill(root: Path, slug: str) -> str:
    return (root / slug / "SKILL.md").read_text(encoding="utf-8")


def write_skill(root: Path, slug: str, text: str) -> None:
    (root / slug / "SKILL.md").write_text(text, encoding="utf-8", newline="\n")


def sub_once(text: str, pattern: str, repl: str) -> str:
    """re.sub that must match exactly once, so a mutation can never silently no-op."""
    new, n = re.subn(pattern, lambda _m: repl, text, flags=re.MULTILINE)
    if n != 1:
        raise AssertionError(f"mutation pattern {pattern!r} matched {n} times (expected exactly 1)")
    return new


def frontmatter_bounds(text: str) -> tuple[int, int]:
    """(start, end) character offsets of the frontmatter text between the fences."""
    first = text.index("\n") + 1
    m = re.compile(r"^---[ \t]*$", re.MULTILINE).search(text, first)
    return first, m.start()


def _edit_key(text: str, key: str, replacement: str | None) -> str:
    """Replace a top-level frontmatter key and its indented/list lines (None = delete)."""
    start, end = frontmatter_bounds(text)
    lines = text[start:end].split("\n")
    for i, line in enumerate(lines):
        if re.match(rf"^{re.escape(key)}:(\s|$)", line):
            j = i + 1
            while j < len(lines) and lines[j][:1] in (" ", "\t"):
                j += 1
            lines[i:j] = [] if replacement is None else replacement.split("\n")
            return text[:start] + "\n".join(lines) + text[end:]
    raise AssertionError(f"key {key!r} not found in frontmatter")


def remove_key(text: str, key: str) -> str:
    """Delete a top-level frontmatter key together with its indented/list lines."""
    return _edit_key(text, key, None)


def set_key(text: str, key: str, value_text: str) -> str:
    """Replace a key (and its block/list lines) with `key:<value_text>` verbatim.

    e.g. value_text=" low", " [a, b]" or "\\n  - a\\n  - b".
    """
    return _edit_key(text, key, f"{key}:{value_text}")


def add_key(text: str, line: str) -> str:
    """Append a raw line at the end of the frontmatter."""
    _start, end = frontmatter_bounds(text)
    return text[:end] + line.rstrip("\n") + "\n" + text[end:]


def run_validator_cli(root: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(REPO_ROOT / "scripts" / "validate_skills.py"), "--root", str(root), *args],
        capture_output=True, text=True, encoding="utf-8", timeout=120,
    )
