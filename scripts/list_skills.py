#!/usr/bin/env python3
"""List skills — either the source library, or what's installed at a target.

Two modes:
  python scripts/list_skills.py                 # every skill in this repo, grouped by domain
  python scripts/list_skills.py --target DIR     # skills installed at DIR per its manifest

With --target, every manifest entry is checked against the disk and flagged
MODIFIED (a file in the skill folder was changed, added or removed since
install; for an entry carried over from a format-1/2 manifest, SKILL.md no
longer matches its recorded sha256), UNVERIFIED (no install-time hash, or a
format-1/2 entry whose folder holds files no manifest recorded), MISSING, or
UNSAFE (the path is a symlink/junction or not a directory, or the folder
contains a symlink/junction, a non-regular file or a name with a control
character: install.sh/uninstall.sh refuse all of these and never follow
links).
Invalid manifest lines (anything that is not a lowercase-kebab slug, e.g.
'../x') are reported and ignored, exactly as install.sh/uninstall.sh do. A
manifest containing a NUL byte is refused as corrupt, as the scripts do.

Output encoding: on a console the console's encoding is used; written to a
pipe or file, the output is UTF-8 unless PYTHONIOENCODING names an encoding.
Characters the chosen encoding can't represent are printed as backslash
escapes (\\uXXXX) instead of failing.

Dependency-free (stdlib + skills_lib), matching the rest of scripts/.
"""
from __future__ import annotations

import argparse
import hashlib
import os
import re
import stat
import sys
from dataclasses import dataclass, field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import skills_lib as sl  # noqa: E402

MANIFEST_NAME = ".ml-ai-skills-manifest"
SUPPORTED_FORMATS = ("1", "2", "3")  # compared as strings, like install.sh ("02" is refused)
# Exactly bash's [:space:] under LC_ALL=C. Bare str.strip() also strips Unicode
# spaces (e.g. U+00A0) that install.sh keeps, and the parsers would disagree.
ASCII_WS = " \t\n\r\v\f"
# Must match SLUG_RE / HASH_RE in install.sh and uninstall.sh.
SLUG_RE = re.compile(r"[a-z0-9]+(-[a-z0-9]+)*")
HASH_RE = re.compile(r"[0-9a-f]{64}")
# bash's [[:cntrl:]] under LC_ALL=C: bytes 0x01-0x1f and 0x7f (NUL can't occur).
CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")

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
    # slug -> {relative path: sha256} for every file in the folder (format 3).
    # A slug without a key here was carried over from a format-1/2 manifest.
    files: dict[str, dict[str, str]] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)


def is_valid_slug(slug: str) -> bool:
    return SLUG_RE.fullmatch(slug) is not None


def is_valid_relpath(rel: str) -> bool:
    """Mirrors is_valid_relpath in install.sh: relative, '/'-separated, no
    empty/'.'/'..' component, no control character."""
    if CONTROL_RE.search(rel):
        return False
    return all(part not in ("", ".", "..") for part in rel.split("/"))


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
    """Parse a format-1 (bare slug lines), format-2 ('# format: 2' header,
    'slug<TAB>sha256' lines) or format-3 (format 2 plus one
    'slug<TAB>sha256<TAB>relpath' line per file) manifest. Mirrors
    read_manifest in install.sh, including its C-locale whitespace rules: CR
    is stripped (manifests edited on Windows), the first entry for a slug (or
    for a file of a slug) wins, and invalid lines are dropped with a warning."""
    m = Manifest()
    declared = "1"
    data = manifest_path.read_bytes()
    if b"\0" in data:
        # bash's `read` silently drops NUL bytes, so the scripts refuse such a
        # manifest outright rather than let 'al\0pha' become 'alpha'.
        raise ManifestError(f"{manifest_path} contains a NUL byte; it is corrupt or not a manifest. "
                            "Inspect it (or move it aside) and re-run.")
    # surrogateescape: a non-UTF-8 file name round-trips to the same str that
    # os.scandir() returns for it on POSIX, so it still compares equal.
    text = data.decode("utf-8", errors="surrogateescape")
    file_lines: list[tuple[int, str, str, str]] = []
    for lineno, raw in enumerate(text.split("\n"), 1):
        line = raw[:-1] if raw.endswith("\r") else raw  # one CR, like ${line%$'\r'}
        if line.startswith("# format:"):
            declared = line.split(":", 1)[1].strip(ASCII_WS)
            continue
        if line.startswith("# source:"):
            m.source = line.split(":", 1)[1].strip(ASCII_WS)
            continue
        stripped = line.strip(ASCII_WS)
        if line.startswith("#") or not stripped:
            continue
        if stripped.count("\t") >= 2:
            # File line: the path is everything after the second TAB, untrimmed.
            slug, recorded, rel = line.lstrip(ASCII_WS).split("\t", 2)
            slug, recorded = slug.strip(ASCII_WS), recorded.strip(ASCII_WS)
            if not (is_valid_slug(slug) and HASH_RE.fullmatch(recorded) and is_valid_relpath(rel)):
                m.warnings.append(f"line {lineno}: not a valid 'slug<TAB>sha256<TAB>file' entry — ignored")
                continue
            file_lines.append((lineno, slug, recorded, rel))
            continue
        slug, sep, recorded = stripped.partition("\t")
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
    if file_lines and m.format != 3:
        m.warnings.append("per-file lines ignored: only a format 3 manifest has them")
        return m
    for lineno, slug, recorded, rel in file_lines:
        if slug not in m.entries:
            m.warnings.append(f"line {lineno}: no skill line for {slug!r} — ignored")
            continue
        files = m.files.setdefault(slug, {})
        if rel in files:
            m.warnings.append(f"line {lineno}: a second entry for a file of {slug!r} — ignored (the first one counts)")
            continue
        files[rel] = recorded
    return m


def _is_junction(path: Path) -> bool:
    # os.path.isjunction exists on 3.12+. Before that, read the reparse tag the
    # same way 3.12 does (Windows only; st_reparse_tag exists there since 3.8).
    isjunction = getattr(os.path, "isjunction", None)
    if isjunction is not None:
        return isjunction(path)
    if os.name != "nt":
        return False
    try:
        st = os.lstat(path)
    except OSError:
        return False
    return getattr(st, "st_reparse_tag", 0) == stat.IO_REPARSE_TAG_MOUNT_POINT


def _is_link(path: Path) -> bool:
    # Junctions are Windows' directory links; Git Bash's `[ -L ]` sees them as symlinks.
    return path.is_symlink() or _is_junction(path)


def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def tree_unsafe_reason(d: Path) -> str | None:
    """Mirrors tree_is_safe in install.sh/uninstall.sh: every entry below d
    (hidden ones included) must be a real directory or a regular file whose
    name has no control character. Never follows links. Returns why the tree
    is unsafe, or None."""
    try:
        entries = sorted(os.scandir(d), key=lambda e: e.name)
    except OSError:
        return None  # bash's glob sees an unreadable folder as empty, too
    for e in entries:
        p = Path(e.path)
        if CONTROL_RE.search(e.name):
            return f"contains a name with a control character: {p!r}"
        if _is_link(p):
            return f"contains a symlink/junction: {p}"
        if e.is_dir(follow_symlinks=False):
            reason = tree_unsafe_reason(p)
            if reason:
                return reason
        elif not e.is_file(follow_symlinks=False):
            return f"contains a non-regular file: {p}"
    return None


def tree_files(d: Path, prefix: str = "") -> dict[str, str]:
    """{relative '/'-separated path: sha256} for every regular file below d
    (call only on a tree tree_unsafe_reason accepted)."""
    out: dict[str, str] = {}
    for e in sorted(os.scandir(d), key=lambda e: e.name):
        rel = prefix + e.name
        if e.is_dir(follow_symlinks=False):
            out.update(tree_files(Path(e.path), rel + "/"))
        else:
            out[rel] = sha256_file(Path(e.path))
    return out


def _names(paths: list[str]) -> str:
    # Same order as install.sh: disk (walk) order, or manifest order for removed files.
    shown = ", ".join(paths[:3])
    return shown + (f" (+{len(paths) - 3} more)" if len(paths) > 3 else "")


def files_diff(recorded: dict[str, str], current: dict[str, str]) -> str:
    """'' if equal, else which files were changed/added/removed (as install.sh says it)."""
    changed = [p for p in current if p in recorded and current[p] != recorded[p]]
    added = [p for p in current if p not in recorded]
    removed = [p for p in recorded if p not in current]
    parts = []
    for label, paths in (("changed", changed), ("added", added), ("removed", removed)):
        if paths:
            parts.append(f"{label}: {_names(paths)}")
    return "; ".join(parts)


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


def skill_check(target: Path, slug: str, recorded: str | None,
                files: dict[str, str] | None = None) -> tuple[str, str]:
    """(status, detail): status is one of ok, modified, unverified, missing,
    unsafe; detail names files where that helps. Mirrors entry_status in
    install.sh/uninstall.sh (without a source checkout, like uninstall.sh)."""
    skill_md = target / slug / "SKILL.md"
    if unsafe_reason(target, slug):
        return "unsafe", ""
    if not skill_md.is_file():
        return "missing", ""
    current = tree_files(target / slug)
    if files is not None:
        detail = files_diff(files, current)
        if not detail and recorded != current.get("SKILL.md"):
            detail = "SKILL.md does not match the hash on its skill line"
        return ("modified", detail) if detail else ("ok", "")
    if recorded is None:
        return "unverified", ""
    if current["SKILL.md"] != recorded:
        return "modified", "changed: SKILL.md"
    extra = [p for p in current if p != "SKILL.md"]
    return ("unverified", _names(extra)) if extra else ("ok", "")


def skill_status(target: Path, slug: str, recorded: str | None, files: dict[str, str] | None = None) -> str:
    """One of: ok, modified, unverified, missing, unsafe."""
    return skill_check(target, slug, recorded, files)[0]


def unverified_note(manifest_format: int, slug: str, recorded: str | None = None, extra: str = "") -> str:
    if recorded is not None:
        # SKILL.md matches, but other files predate per-file tracking (format 1/2 entry).
        return (f"UNVERIFIED — the manifest recorded only SKILL.md; other files here ({extra}) were never "
                "recorded. Re-run install.sh (it records per-file hashes when they match the repo copy), "
                f"or inspect them, then install.sh/uninstall.sh --force --skill {slug}")
    # Same split as uninstall.sh: only a still-format-1 manifest can be helped
    # by re-running install.sh; a '-' in format 2/3 is one install.sh already
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
        recorded = manifest.entries[slug]
        status, detail = skill_check(target, slug, recorded, manifest.files.get(slug))
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
            line += f"  [{unverified_note(manifest.format, slug, recorded, detail)}]"
        elif status == "modified":
            line += ("  [MODIFIED locally since install"
                     f"{' (' + detail + ')' if detail else ''}"
                     " — install.sh/uninstall.sh will leave it alone unless --force]")
        print(line)
    problems = {k: v for k, v in counts.items() if k != "ok"}
    if problems:
        print("\n" + ", ".join(f"{v} {k}" for k, v in sorted(problems.items())))
    return 0


def _configure_output() -> None:
    """Make the listing readable and never crash on encoding (docs/INSTALLER.md).

    On Windows a pipe, file or Git Bash's mintty gives Python a legacy code page
    (e.g. cp1252) as stdout encoding, so a non-Latin profile path or display
    name used to come out as \\uXXXX escapes (or, earlier, a UnicodeEncodeError).
    A console keeps its own encoding. A pipe or file gets UTF-8 unless
    PYTHONIOENCODING names an encoding, which is honoured. Either way,
    characters the encoding can't represent become backslash escapes.
    """
    explicit = os.environ.get("PYTHONIOENCODING", "").split(":", 1)[0].strip()
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:
            continue
        try:
            interactive = stream.isatty()
        except (AttributeError, OSError, ValueError):
            interactive = False
        if interactive or explicit:
            reconfigure(errors="backslashreplace")
        else:
            reconfigure(encoding="utf-8", errors="backslashreplace")


def main() -> int:
    _configure_output()
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--target", help="Show installed skills at this directory instead of the source repo")
    args = ap.parse_args()

    if args.target:
        return list_installed(Path(args.target).expanduser())
    return list_source()


if __name__ == "__main__":
    sys.exit(main())
