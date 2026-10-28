#!/usr/bin/env python3
"""List skills — either the source library, or what's installed at a target.

Two modes:
  python scripts/list_skills.py                 # every skill in this repo, grouped by domain
  python scripts/list_skills.py --target DIR     # skills installed at DIR per its manifest

With --target, every manifest entry is checked against the disk and flagged
MODIFIED (SKILL.md no longer matches the sha256 recorded at install time),
UNVERIFIED (no install-time hash: a format-1 entry), MISSING, or UNSAFE
(the path is a symlink/junction or not a directory, or the folder contains a
symlink/junction or a non-regular file: install.sh/uninstall.sh refuse all
of these and never follow links).
Invalid manifest lines (anything that is not a lowercase-kebab slug, e.g.
'../x') are reported and ignored, exactly as install.sh/uninstall.sh do. A
manifest containing a NUL byte is refused as corrupt, as the scripts do.

Dependency-free (stdlib + skills_lib), matching the rest of scripts/.
"""
from __future__ import annotations

import argparse
import hashlib
import os
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import skills_lib as sl  # noqa: E402

MANIFEST_NAME = ".ml-ai-skills-manifest"
SUPPORTED_FORMATS = ("1", "2")  # compared as strings, like install.sh ("02" is refused)
# Exactly bash's [:space:] under LC_ALL=C. Bare str.strip() also strips Unicode
# spaces (e.g. U+00A0) that install.sh keeps, and the parsers would disagree.
ASCII_WS = " \t\n\r\v\f"
# Must match SLUG_RE / HASH_RE in install.sh and uninstall.sh.
SLUG_RE = re.compile(r"[a-z0-9]+(-[a-z0-9]+)*")
HASH_RE = re.compile(r"[0-9a-f]{64}")

DOMAIN_ORDER = (
    "foundations", "classical-ml", "deep-learning", "llm",
    "specialized", "mlops", "responsible-ai", "security",
)
TYPE_TAG = {"workflow": "W", "reference": "R"}


class ManifestError(ValueError):
    """The manifest cannot be interpreted safely (e.g. unknown format)."""


@dataclass
class Manifest:
    format: int = 1
    source: str | None = None
    # slug -> recorded sha256 of the installed SKILL.md, or None if unknown (format 1)
    entries: dict[str, str | None] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)


def is_valid_slug(slug: str) -> bool:
    return SLUG_RE.fullmatch(slug) is not None


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


def read_manifest(manifest_path: Path) -> Manifest:
    """Parse a format-1 (bare slug lines) or format-2 ('# format: 2' header,
    'slug<TAB>sha256' lines) manifest. Mirrors read_manifest in install.sh,
    including its C-locale whitespace rules: CR is stripped (manifests edited on Windows), the first entry for a slug
    wins, and invalid slugs are dropped with a warning."""
    m = Manifest()
    declared = "1"
    data = manifest_path.read_bytes()
    if b"\0" in data:
        # bash's `read` silently drops NUL bytes, so the scripts refuse such a
        # manifest outright rather than let 'al\0pha' become 'alpha'.
        raise ManifestError(f"{manifest_path} contains a NUL byte; it is corrupt or not a manifest. "
                            "Inspect it (or move it aside) and re-run.")
    text = data.decode("utf-8", errors="replace")
    for lineno, raw in enumerate(text.split("\n"), 1):
        line = raw.rstrip("\r")
        if line.startswith("# format:"):
            declared = line.split(":", 1)[1].strip(ASCII_WS)
            continue
        if line.startswith("# source:"):
            m.source = line.split(":", 1)[1].strip(ASCII_WS)
            continue
        if line.startswith("#") or not line.strip(ASCII_WS):
            continue
        slug, sep, recorded = line.strip(ASCII_WS).partition("\t")
        slug, recorded = slug.strip(ASCII_WS), recorded.strip(ASCII_WS)
        if not is_valid_slug(slug):
            m.warnings.append(f"line {lineno}: {slug!r} is not a valid skill slug — ignored")
            continue
        if not HASH_RE.fullmatch(recorded):
            if sep and recorded != "-":
                m.warnings.append(f"line {lineno}: unrecognised hash for {slug!r} — treated as unverifiable")
            recorded = None
        m.entries.setdefault(slug, recorded)
    if declared not in SUPPORTED_FORMATS:
        raise ManifestError(
            f"{manifest_path} declares format {declared!r}; this tool understands "
            f"formats {', '.join(SUPPORTED_FORMATS)}. Update your ml-ai-skills checkout."
        )
    m.format = int(declared)
    return m


def _is_link(path: Path) -> bool:
    # os.path.isjunction exists on 3.12+; junctions are Windows' directory links.
    isjunction = getattr(os.path, "isjunction", None)
    return path.is_symlink() or bool(isjunction and isjunction(path))


def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def tree_unsafe_reason(d: Path) -> str | None:
    """Mirrors tree_is_safe in install.sh/uninstall.sh: every entry below d
    (hidden ones included) must be a real directory or a regular file. Never
    follows links. Returns why the tree is unsafe, or None."""
    try:
        entries = sorted(os.scandir(d), key=lambda e: e.name)
    except OSError:
        return None  # bash's glob sees an unreadable folder as empty, too
    for e in entries:
        p = Path(e.path)
        if _is_link(p):
            return f"contains a symlink/junction: {p}"
        if e.is_dir(follow_symlinks=False):
            reason = tree_unsafe_reason(p)
            if reason:
                return reason
        elif not e.is_file(follow_symlinks=False):
            return f"contains a non-regular file: {p}"
    return None


def unsafe_reason(target: Path, slug: str) -> str | None:
    """Why install.sh/uninstall.sh would refuse target/slug, or None."""
    skill_dir = target / slug
    if _is_link(skill_dir):
        return "path is a symlink/junction; not followed"
    if not skill_dir.exists():
        return None
    if not skill_dir.is_dir():
        return "exists but is not a directory"
    reason = tree_unsafe_reason(skill_dir)
    return f"installed folder {reason}; not followed" if reason else None


def skill_status(target: Path, slug: str, recorded: str | None) -> str:
    """One of: ok, modified, unverified, missing, unsafe."""
    skill_md = target / slug / "SKILL.md"
    if unsafe_reason(target, slug):
        return "unsafe"
    if not skill_md.is_file():
        return "missing"
    if recorded is None:
        return "unverified"
    return "ok" if sha256_file(skill_md) == recorded else "modified"


STATUS_NOTE = {
    "modified": "MODIFIED locally since install — install.sh/uninstall.sh will leave it alone unless --force",
}


def unverified_note(manifest_format: int, slug: str) -> str:
    # Same split as uninstall.sh: only a still-format-1 manifest can be helped
    # by re-running install.sh; a '-' in format 2 is one install.sh already
    # failed to prove unedited, so re-running it would not change anything.
    if manifest_format == 1:
        return "UNVERIFIED — format-1 manifest has no hash; re-run install.sh to record one"
    return ("UNVERIFIED — no install-time hash (carried over from a format-1 manifest; install.sh could not "
            f"prove it unedited); inspect it, then install.sh/uninstall.sh --force --skill {slug}")


def list_installed(target: Path) -> int:
    manifest_path = target / MANIFEST_NAME
    if _is_link(manifest_path):
        print(f"ERROR: {manifest_path} is a symlink; refusing to read it", file=sys.stderr)
        return 1
    if not manifest_path.is_file():
        print(f"No ml-ai-skills manifest found at {manifest_path}")
        print("(nothing installed here by install.sh, or wrong --target)")
        return 1

    try:
        manifest = read_manifest(manifest_path)
    except ManifestError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 1
    for w in manifest.warnings:
        print(f"WARN: manifest {w}", file=sys.stderr)

    slugs = sorted(manifest.entries)
    print(f"{len(slugs)} skill(s) installed at {target} "
          f"(source: {manifest.source or 'unknown'}, manifest format {manifest.format}):\n")
    counts: dict[str, int] = {}
    for slug in slugs:
        status = skill_status(target, slug, manifest.entries[slug])
        counts[status] = counts.get(status, 0) + 1
        if status == "unsafe":
            print(f"  [UNSAFE] {slug} — {unsafe_reason(target, slug)}")
            continue
        if status == "missing":
            print(f"  [MISSING] {slug} — listed in manifest but SKILL.md not found on disk")
            continue
        try:
            skill = sl.load_skill(target / slug)
            tag = TYPE_TAG.get(skill.frontmatter.get("type"), "?")
            line = f"  [{tag}] {slug} — {skill.frontmatter.get('display_name', slug)}"
        except (sl.FrontmatterError, OSError, UnicodeDecodeError) as e:
            line = f"  [INVALID] {slug} — {e}"
        if status == "unverified":
            line += f"  [{unverified_note(manifest.format, slug)}]"
        elif status in STATUS_NOTE:
            line += f"  [{STATUS_NOTE[status]}]"
        print(line)
    problems = {k: v for k, v in counts.items() if k != "ok"}
    if problems:
        print("\n" + ", ".join(f"{v} {k}" for k, v in sorted(problems.items())))
    return 0


def _never_crash_on_encoding() -> None:
    # On Windows, a pipe/Git Bash/CI log gives Python a legacy code page
    # (e.g. cp1252) as stdout encoding; a target path or display_name outside
    # it must not turn a listing into a UnicodeEncodeError (exit 1).
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            reconfigure(errors="backslashreplace")


def main() -> int:
    _never_crash_on_encoding()
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--target", help="Show installed skills at this directory instead of the source repo")
    args = ap.parse_args()

    if args.target:
        return list_installed(Path(args.target).expanduser())
    return list_source()


if __name__ == "__main__":
    sys.exit(main())
