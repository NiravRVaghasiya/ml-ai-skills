#!/usr/bin/env python3
"""Export canonical `<slug>/SKILL.md` folders for a stricter upload path.

The canonical skills carry about 15 repo-specific frontmatter keys (`display_name`,
`type`, `domain`, ...). Claude Code ignores keys it does not recognise, but claude.ai
skill upload, the Skills API, `package_skill.py` from anthropics/skills and the Agent
Skills reference validator (`skills-ref`) accept only six top-level keys and fail on
anything else. This script writes a *separate* copy of each skill whose frontmatter
contains only the keys the chosen target allows. It never edits the canonical folders.

What the transformation does, per skill:
  * `name` and `description` are copied unchanged (same parsed value).
  * Every other canonical key is moved, not dropped, into the spec's `metadata` map
    (string keys to string values): plain strings keep their value, list keys
    (`schema/allowed_values.py` LIST_KEYS) become a JSON array string, e.g.
    `related: "[\"a\", \"b\"]"`. Every move is printed and recorded in
    `export-report.json`. Nothing is dropped silently; nothing is dropped at all.
  * The Markdown body (every byte after the closing `---` line) is copied
    byte-for-byte, and so is every other regular file in the skill folder, except the
    build artifacts `package_skill.py` also leaves out (`__pycache__/`,
    `node_modules/`, `*.pyc`, `.DS_Store`, a top-level `evals/`), which are listed.
  * A symlink, junction or other non-regular entry in a skill folder is refused (never
    followed), as `install.sh` does.
  * The result is validated against the target's rules before anything is written; if
    any skill fails, nothing is written and the exit code is 1.

Round trip: `canonical_values()` turns an exported frontmatter back into the canonical
*values* (tested for every skill). The canonical *text* is not reproduced: block
scalars, flow vs block lists, quoting and comments are not preserved, because the
export writes every value as a double-quoted YAML string.

Targets and the rules each enforces (sources read 2026-10-29; see docs/COMPATIBILITY.md):
  agentskills-spec  https://agentskills.io/specification and the skills-ref validator:
                    the six keys; name 1-64 chars of a-z/0-9/hyphens, no leading,
                    trailing or doubled hyphen, equal to the folder name; description
                    non-empty, at most 1,024 chars; compatibility 1-500 chars;
                    metadata a string-to-string map; no `---` inside the frontmatter
                    (skills-ref's parser splits on the first two; the emitter escapes it).
  skills-api        the above, plus the Claude platform rules (Agent Skills overview and
                    the Skills API guide): no reserved word (anthropic, claude) in name,
                    no XML tags in name or description, total upload under 30 MB, and
                    SKILL.md inside one top-level folder named after the skill.
  claude-ai-upload  skills-api, plus the extra checks in anthropics/skills
                    `quick_validate.py`: no `<` or `>` anywhere in the description, LF-only
                    frontmatter. The Claude Code docs say only that claude.ai uploads
                    accept the same six fields as package_skill.py; whether claude.ai also
                    bans `<`/`>` is unknown, so this target applies the stricter rule as a
                    conservative choice. claude.ai's own validator is not published.

Passing these checks is necessary, not sufficient: no upload to claude.ai or the Skills
API has been made from this repo, so acceptance there is unverified.

Usage:
    python scripts/export_skills.py --target skills-api --out dist/skills-api
    python scripts/export_skills.py --target claude-ai-upload --out OUT --zip data-preprocessing
    python scripts/export_skills.py --list-targets

Exit codes: 0 exported, 1 a skill failed the target's rules or a post-write check
(nothing written in the first case), 2 usage error (unknown target, non-empty --out,
unknown slug, --out inside a skill folder).
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import stat
import sys
import zipfile
from dataclasses import dataclass, field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "schema"))

import allowed_values as av  # noqa: E402
import skills_lib as sl  # noqa: E402
from validate_skills import XML_TAG_RE  # noqa: E402

REPORT_NAME = "export-report.json"

# The six frontmatter keys of the Agent Skills spec, in the order they are written.
SPEC_KEYS = ("name", "description", "license", "compatibility", "allowed-tools", "metadata")
COMPATIBILITY_MAX_LENGTH = 500  # agentskills.io/specification, skills-ref, quick_validate.py
UPLOAD_MAX_BYTES = 30 * 1000 * 1000  # Skills API guide: "under 30 MB (uncompressed)"; decimal MB is the stricter reading

# Claude Code frontmatter fields (code.claude.com/docs/en/skills, read 2026-10-29). The docs
# say not to reuse them as `metadata` keys, so a canonical key with one of these names is refused.
CLAUDE_CODE_FIELDS = frozenset({
    "name", "description", "when_to_use", "argument-hint", "arguments", "disable-model-invocation",
    "user-invocable", "allowed-tools", "disallowed-tools", "model", "effort", "context", "agent",
    "background", "hooks", "paths", "shell", "metadata", "license", "compatibility",
})

# Left out of the export because package_skill.py leaves them out of a .skill archive.
EXCLUDED_DIRS = frozenset({"__pycache__", "node_modules"})
EXCLUDED_ROOT_DIRS = frozenset({"evals"})
EXCLUDED_FILES = frozenset({".DS_Store"})
EXCLUDED_SUFFIXES = (".pyc",)

# A metadata key is written as a plain YAML key, so it must not be one YAML would read as
# a bool or null (PyYAML / YAML 1.1) and must not need quoting.
METADATA_KEY_RE = re.compile(r"^[a-z][a-z0-9_]*$")
YAML_SPECIAL_WORDS = frozenset({"yes", "no", "on", "off", "true", "false", "null", "y", "n"})
NAME_RE = re.compile(av.SLUG_PATTERN)


@dataclass(frozen=True)
class Target:
    name: str
    summary: str
    sources: tuple[str, ...]
    reserved_words: bool = False
    xml_tags: bool = False
    angle_brackets: bool = False
    lf_frontmatter: bool = False
    max_upload_bytes: int | None = None
    allowed_keys: tuple[str, ...] = SPEC_KEYS


TARGETS: dict[str, Target] = {t.name: t for t in (
    Target(
        "agentskills-spec",
        "Agent Skills open specification (skills-ref validate)",
        ("https://agentskills.io/specification",
         "https://github.com/agentskills/agentskills/blob/main/skills-ref/src/skills_ref/validator.py"),
    ),
    Target(
        "skills-api",
        "Claude Skills API upload (POST /v1/skills)",
        ("https://agentskills.io/specification",
         "https://platform.claude.com/docs/en/agents-and-tools/agent-skills/overview",
         "https://platform.claude.com/docs/en/build-with-claude/skills-guide"),
        reserved_words=True, xml_tags=True, max_upload_bytes=UPLOAD_MAX_BYTES,
    ),
    Target(
        "claude-ai-upload",
        "claude.ai skill upload (conservative) and anthropics/skills package_skill.py",
        ("https://code.claude.com/docs/en/skills",
         "https://github.com/anthropics/skills/blob/main/skills/skill-creator/scripts/quick_validate.py",
         "https://platform.claude.com/docs/en/agents-and-tools/agent-skills/overview",
         "https://platform.claude.com/docs/en/build-with-claude/skills-guide"),
        reserved_words=True, xml_tags=True, angle_brackets=True, lf_frontmatter=True,
        max_upload_bytes=UPLOAD_MAX_BYTES,
    ),
)}


class UsageError(Exception):
    """Bad invocation: exit code 2, nothing written."""


@dataclass
class SkillExport:
    slug: str
    source_dir: Path
    source_sha256: str = ""
    skill_md: bytes = b""
    body_sha256: str = ""
    files: dict[str, bytes] = field(default_factory=dict)  # rel posix path -> bytes, SKILL.md included
    moved: list[dict] = field(default_factory=list)
    added: list[dict] = field(default_factory=list)
    excluded: list[str] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)


# --------------------------------------------------------------------------
# Filesystem safety
# --------------------------------------------------------------------------

IO_REPARSE_TAG_NAME_SURROGATE_BIT = 0x20000000  # IsReparseTagNameSurrogate() in the Windows SDK
LINK_REPARSE_TAGS = frozenset({
    getattr(stat, "IO_REPARSE_TAG_SYMLINK", 0xA000000C),
    getattr(stat, "IO_REPARSE_TAG_MOUNT_POINT", 0xA0000003),  # junctions
    getattr(stat, "IO_REPARSE_TAG_APPEXECLINK", 0x8000001B),
})


def is_link(path: Path) -> bool:
    """A symlink, a junction or another link-like reparse point (never followed).

    On Windows a reparse point counts as a link when its tag is a symlink, junction or
    app-execution alias, has the name-surrogate bit (it stands for another path, e.g. a WSL
    symlink), or cannot be read. Other reparse points (OneDrive placeholders, deduplicated
    files) hold the file's own data and are read like regular files, as os.stat does."""
    try:
        st = os.lstat(path)
    except OSError:
        return False
    if stat.S_ISLNK(st.st_mode):
        return True
    # Windows: junctions are not S_ISLNK before 3.12 and os.path.isjunction is 3.12+, but the
    # reparse-point attribute and st_reparse_tag are reported on every supported version.
    reparse = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    if not getattr(st, "st_file_attributes", 0) & reparse:
        return False
    tag = getattr(st, "st_reparse_tag", None)
    return tag is None or tag in LINK_REPARSE_TAGS or bool(tag & IO_REPARSE_TAG_NAME_SURROGATE_BIT)


def collect_files(skill_dir: Path, exp: SkillExport) -> None:
    """Read every shipped file of `skill_dir` into exp.files; record exclusions and problems."""
    def onerror(err: OSError) -> None:
        exp.problems.append(f"cannot read {err.filename}: {err.strerror}")

    for dirpath, dirnames, filenames in os.walk(skill_dir, onerror=onerror, followlinks=False):
        here = Path(dirpath)
        rel_dir = here.relative_to(skill_dir)
        keep = []
        for d in sorted(dirnames):
            rel = (rel_dir / d).as_posix()
            if is_link(here / d):
                exp.problems.append(f"{rel}: is a symlink/junction; refusing to follow it")
            elif d in EXCLUDED_DIRS or (rel_dir == Path(".") and d in EXCLUDED_ROOT_DIRS):
                exp.excluded.append(rel + "/")
            else:
                keep.append(d)
        dirnames[:] = keep
        for f in sorted(filenames):
            path = here / f
            rel = (rel_dir / f).as_posix()
            if "\\" in f:
                exp.problems.append(f"{rel}: file name contains a backslash, which is a path separator on Windows")
                continue
            if is_link(path):
                exp.problems.append(f"{rel}: is a symlink/junction; refusing to follow it")
                continue
            if not stat.S_ISREG(os.lstat(path).st_mode):
                exp.problems.append(f"{rel}: not a regular file")
                continue
            if f in EXCLUDED_FILES or f.endswith(EXCLUDED_SUFFIXES):
                exp.excluded.append(rel)
                continue
            exp.files[rel] = path.read_bytes()


# --------------------------------------------------------------------------
# SKILL.md split / YAML emission
# --------------------------------------------------------------------------

def split_skill_md(data: bytes) -> tuple[str, bytes]:
    """Return (frontmatter text with LF newlines, body bytes exactly as on disk).

    The fences are whole `---` lines, as in skills_lib; the body is every byte after the
    closing fence's line ending (no stripping)."""
    if data.startswith(b"\xef\xbb\xbf"):
        raise ValueError("SKILL.md starts with a UTF-8 byte-order mark; uploads expect `---` as the first bytes")
    try:
        data.decode("utf-8")
    except UnicodeDecodeError as e:
        raise ValueError(f"SKILL.md is not valid UTF-8 ({e})") from e
    lines = data.split(b"\n")
    if lines[0].rstrip() != b"---":
        raise ValueError("SKILL.md does not start with a `---` frontmatter fence")
    for idx in range(1, len(lines)):
        if lines[idx].rstrip() == b"---":
            fm = b"".join(line + b"\n" for line in lines[1:idx]).replace(b"\r\n", b"\n")
            body = b"\n".join(lines[idx + 1:])
            return fm.decode("utf-8"), body
    raise ValueError("SKILL.md has no closing `---` frontmatter fence")


def _is_yaml_printable(ch: str) -> bool:
    cp = ord(ch)
    return (0x20 <= cp <= 0x7E or 0xA0 <= cp <= 0xD7FF or 0xE000 <= cp <= 0xFFFD
            or 0x10000 <= cp <= 0x10FFFF) and cp not in (0x2028, 0x2029, 0xFEFF)  # YAML 1.1 line breaks, BOM


def yaml_quote(value: str) -> str:
    """A single-line YAML double-quoted scalar for `value`.

    Only `\\"`, `\\\\`, `\\n`, `\\t`, `\\r` and `\\uXXXX` escapes are produced, all of which mean
    the same in JSON, so json.loads reads the scalar back. Characters YAML would treat as
    line breaks (NEL, U+2028, U+2029) or rejects (controls, U+FEFF) are escaped.

    Three hyphens in a row are never written literally: skills-ref's parser finds the
    frontmatter with `content.split("---", 2)`, so a literal `---` inside a value would
    end the frontmatter there. Every third consecutive `-` is written as `\\u002d`."""
    out = ['"']
    hyphens = 0  # literal '-' characters just written
    for ch in value:
        if ch == "-":
            if hyphens == 2:
                out.append("\\u002d")
                hyphens = 0
            else:
                out.append("-")
                hyphens += 1
            continue
        hyphens = 0
        if ch == '"':
            out.append('\\"')
        elif ch == "\\":
            out.append("\\\\")
        elif ch == "\n":
            out.append("\\n")
        elif ch == "\t":
            out.append("\\t")
        elif ch == "\r":
            out.append("\\r")
        elif _is_yaml_printable(ch):
            out.append(ch)
        else:  # every remaining non-printable code point is in the BMP
            out.append(f"\\u{ord(ch):04x}")
    out.append('"')
    return "".join(out)


def render_frontmatter(fm: dict) -> str:
    lines = ["---"]
    for key in SPEC_KEYS:
        if key not in fm:
            continue
        if key == "metadata":
            lines.append("metadata:")
            lines.extend(f"  {k}: {yaml_quote(v)}" for k, v in fm["metadata"].items())
        else:
            lines.append(f"{key}: {yaml_quote(fm[key])}")
    lines.append("---")
    return "\n".join(lines) + "\n"


_TOP_RE = re.compile(r'^(?P<key>[a-z][a-z-]*):(?: (?P<val>".*"))?$')
_META_RE = re.compile(r'^  (?P<key>[a-z][a-z0-9_]*): (?P<val>".*")$')


def parse_exported_frontmatter(text: str) -> dict:
    """Read back exactly the grammar render_frontmatter writes (raises ValueError otherwise).

    Used to validate the bytes actually written, and by tests for the round trip. Not a
    general YAML parser: anything else is an error, never a guess."""
    lines = text.split("\n")
    if lines[0] != "---":
        raise ValueError("exported SKILL.md does not start with `---`")
    out: dict = {}
    in_meta = False
    for n, line in enumerate(lines[1:], start=2):
        if line == "---":
            return out
        m = _META_RE.match(line)
        if m and in_meta:
            key = m.group("key")
            if key in out["metadata"]:
                raise ValueError(f"line {n}: duplicate metadata key {key!r}")
            out["metadata"][key] = json.loads(m.group("val"))
            continue
        m = _TOP_RE.match(line)
        if not m:
            raise ValueError(f"line {n}: not in the exported frontmatter grammar: {line!r}")
        key = m.group("key")
        if key in out:
            raise ValueError(f"line {n}: duplicate key {key!r}")
        if key == "metadata" and m.group("val") is None:
            out["metadata"] = {}
            in_meta = True
        elif m.group("val") is not None:
            out[key] = json.loads(m.group("val"))
            in_meta = False
        else:
            raise ValueError(f"line {n}: key {key!r} has no value")
    raise ValueError("exported SKILL.md has no closing `---`")


def canonical_values(exported: dict) -> dict:
    """Invert the metadata fold: the canonical frontmatter values an export came from.

    `license`/`compatibility`/`allowed-tools` added by the export (e.g. --license) are
    returned as-is; they have no canonical counterpart."""
    values = {k: v for k, v in exported.items() if k != "metadata"}
    for key, raw in exported.get("metadata", {}).items():
        values[key] = json.loads(raw) if key in av.LIST_KEYS else raw
    return values


# --------------------------------------------------------------------------
# Transformation and validation
# --------------------------------------------------------------------------

def build_frontmatter(canonical: dict, exp: SkillExport, license_text: str | None) -> dict:
    out: dict = {}
    for key in ("name", "description"):
        value = canonical.get(key)
        if not isinstance(value, str):
            exp.problems.append(f"frontmatter `{key}` is missing or not a string")
        else:
            out[key] = value
    metadata: dict[str, str] = {}
    for key, value in canonical.items():
        if key in ("name", "description"):
            continue
        if key in SPEC_KEYS:
            if key == "metadata" or not isinstance(value, str):
                exp.problems.append(f"canonical `{key}` cannot be carried over (not a plain string)")
            else:
                out[key] = value
            continue
        if not METADATA_KEY_RE.fullmatch(key) or key in YAML_SPECIAL_WORDS:
            exp.problems.append(f"canonical key `{key}` cannot be written as a plain metadata key")
            continue
        if key in CLAUDE_CODE_FIELDS:
            exp.problems.append(f"canonical key `{key}` is a Claude Code field name; the docs say not to reuse it in metadata")
            continue
        if key in av.LIST_KEYS:
            items = [] if value is None else value
            if not isinstance(items, list) or not all(isinstance(i, str) for i in items):
                exp.problems.append(f"canonical `{key}` is not a list of strings")
                continue
            metadata[key] = json.dumps(items, ensure_ascii=False)
            encoding = "json-array"
        elif isinstance(value, str):
            metadata[key] = value
            encoding = "string"
        else:
            exp.problems.append(f"canonical `{key}` is neither a string nor a list key")
            continue
        exp.moved.append({"key": key, "to": f"metadata.{key}", "encoding": encoding})
    if license_text is not None:
        action = "replaced" if "license" in out else "added"
        out["license"] = license_text
        exp.added.append({"key": "license", "action": action, "value": license_text})
    if metadata:
        out["metadata"] = metadata
    return out


def validate_export(fm: dict, folder_name: str, files: dict[str, bytes], skill_md: bytes,
                    target: Target) -> list[str]:
    """Every rule `target` enforces, checked against what would be written."""
    problems = []
    extra = sorted(set(fm) - set(target.allowed_keys))
    if extra:
        problems.append(f"keys not allowed by {target.name}: {', '.join(extra)}")
    name = fm.get("name")
    if not isinstance(name, str) or not name:
        problems.append("name must be a non-empty string")
    else:
        if len(name) > av.NAME_MAX_LENGTH:
            problems.append(f"name is {len(name)} characters (max {av.NAME_MAX_LENGTH})")
        if not NAME_RE.fullmatch(name):
            problems.append(f"name {name!r} must be lowercase letters, digits and single inner hyphens")
        if name != folder_name:
            problems.append(f"name {name!r} must equal its folder name {folder_name!r}")
        if target.reserved_words:
            hits = [w for w in av.RESERVED_NAME_WORDS if w in name]
            if hits:
                problems.append(f"name contains reserved word(s): {', '.join(hits)}")
        if target.xml_tags and XML_TAG_RE.search(name):
            problems.append("name contains an XML tag")
    desc = fm.get("description")
    if not isinstance(desc, str) or not desc.strip():
        problems.append("description must be a non-empty string")
    else:
        if len(desc) > av.DESCRIPTION_MAX_LENGTH:
            problems.append(f"description is {len(desc)} characters (max {av.DESCRIPTION_MAX_LENGTH})")
        if target.xml_tags and XML_TAG_RE.search(desc):
            problems.append(f"description contains an XML tag: {XML_TAG_RE.search(desc).group(0)}")
        if target.angle_brackets and ("<" in desc or ">" in desc):
            problems.append("description contains `<` or `>` (quick_validate.py rejects both)")
    if "compatibility" in fm:
        comp = fm["compatibility"]
        if not isinstance(comp, str) or not 1 <= len(comp) <= COMPATIBILITY_MAX_LENGTH:
            problems.append(f"compatibility must be 1-{COMPATIBILITY_MAX_LENGTH} characters")
    for key in ("license", "allowed-tools"):
        if key in fm and (not isinstance(fm[key], str) or not fm[key].strip()):
            problems.append(f"{key} must be a non-empty string")
    meta = fm.get("metadata")
    if meta is not None and (not isinstance(meta, dict)
                             or not all(isinstance(k, str) and isinstance(v, str) for k, v in meta.items())):
        problems.append("metadata must be a map from string keys to string values")
    if target.max_upload_bytes is not None:
        total = sum(len(b) for b in files.values())
        if total >= target.max_upload_bytes:
            problems.append(f"skill is {total} bytes uncompressed (must be under {target.max_upload_bytes})")
    # skills-ref takes the frontmatter as `content.split("---", 2)[1]`, quick_validate.py as
    # `^---\n(.*?)\n---`; both must end at the closing fence, so no `---` may come before it.
    head = skill_md.split(b"\n---\n", 1)[0]
    if b"---" in head[3:]:
        problems.append("frontmatter contains `---` before its closing fence (skills-ref splits SKILL.md "
                        "on the first two `---`)")
    # The bytes that will be written must say the same thing (guards the emitter).
    try:
        fm_text = head.decode("utf-8") + "\n---\n"
        if parse_exported_frontmatter(fm_text) != fm:
            problems.append("internal: written frontmatter does not read back to the planned values")
    except ValueError as e:
        problems.append(f"internal: written frontmatter does not parse ({e})")
    if target.lf_frontmatter and not re.match(rb"^---\n.*?\n---", skill_md, re.DOTALL):
        problems.append("frontmatter must be delimited by LF-terminated `---` lines")
    return problems


def plan_skill(root: Path, slug: str, target: Target, license_text: str | None) -> SkillExport:
    src = root / slug
    exp = SkillExport(slug=slug, source_dir=src)
    if is_link(src):
        exp.problems.append("skill folder is a symlink/junction; refusing to follow it")
        return exp
    skill_md_path = src / "SKILL.md"
    if is_link(skill_md_path) or not skill_md_path.is_file():
        exp.problems.append("SKILL.md is missing, not a regular file, or a link")
        return exp
    collect_files(src, exp)
    if not exp.problems and "SKILL.md" not in exp.files:
        exp.problems.append("the skill file must be named exactly SKILL.md")
    if exp.problems:
        return exp
    original = exp.files["SKILL.md"]
    exp.source_sha256 = hashlib.sha256(original).hexdigest()
    try:
        fm_text, body = split_skill_md(original)
    except ValueError as e:
        exp.problems.append(str(e))
        return exp
    parsed = sl.parse_frontmatter_with_diagnostics(fm_text, line_offset=1)
    if parsed.diagnostics:
        exp.problems.extend(f"frontmatter {d}" for d in parsed.diagnostics)
        return exp
    fm = build_frontmatter(parsed.data, exp, license_text)
    if exp.problems:
        return exp
    exp.skill_md = render_frontmatter(fm).encode("utf-8") + body
    exp.body_sha256 = hashlib.sha256(body).hexdigest()
    exp.files["SKILL.md"] = exp.skill_md
    exp.problems.extend(validate_export(fm, slug, exp.files, exp.skill_md, target))
    if not exp.skill_md.endswith(body):
        exp.problems.append("internal: body bytes changed")
    return exp


# --------------------------------------------------------------------------
# Output
# --------------------------------------------------------------------------

def write_zip(path: Path, folder: str, files: dict[str, bytes]) -> None:
    """Deterministic zip: sorted entries, fixed timestamp/permissions/creator, level 9.

    Byte-identical across runs with the same zlib; zlib versions can differ."""
    with zipfile.ZipFile(path, "w") as zf:
        for rel in sorted(files):
            info = zipfile.ZipInfo(f"{folder}/{rel}", date_time=(1980, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.create_system = 3
            info.external_attr = (stat.S_IFREG | 0o644) << 16
            zf.writestr(info, files[rel], compresslevel=9)


def report_entry(exp: SkillExport, target: Target) -> dict:
    return {
        "slug": exp.slug,
        "source": exp.source_dir.as_posix(),
        "source_sha256": exp.source_sha256,
        "exported_sha256": hashlib.sha256(exp.skill_md).hexdigest() if exp.skill_md else None,
        "body_sha256": exp.body_sha256 or None,
        "moved_keys": exp.moved,
        "dropped_keys": [],
        "added_keys": exp.added,
        "excluded_files": exp.excluded,
        "files": sorted(exp.files),
        "problems": exp.problems,
        "target": target.name,
    }


def resolve_slugs(root: Path, slugs: list[str]) -> list[str]:
    if not slugs:
        return [d.name for d in sl.iter_skill_dirs(root)]
    out = []
    for slug in slugs:
        if not NAME_RE.fullmatch(slug):
            raise UsageError(f"not a skill slug: {slug!r} (lowercase-kebab only; paths are not accepted)")
        if not (root / slug / "SKILL.md").exists() and not is_link(root / slug):
            raise UsageError(f"no skill folder {slug!r} under {root}")
        if slug not in out:
            out.append(slug)
    return out


def check_out_dir(out: Path, root: Path, slugs: list[str]) -> None:
    if is_link(out):
        raise UsageError(f"--out {out} is a symlink/junction; give a real directory")
    if out.exists() and (not out.is_dir() or is_link(out)):
        raise UsageError(f"--out {out} exists and is not a plain directory")
    if out.exists() and any(out.iterdir()):
        raise UsageError(f"--out {out} is not empty; the export never overwrites or merges")
    res = out.resolve()
    if res == root.resolve():
        raise UsageError("--out must not be the skills root (the export is never in place)")
    for slug in slugs:
        src = (root / slug).resolve()
        if res == src or src in res.parents:
            raise UsageError(f"--out {out} is inside the canonical skill folder {slug}/")


def export(root: Path, slugs: list[str], target_name: str, out: Path | None, *,
           license_text: str | None = None, make_zip: bool = False) -> tuple[int, dict]:
    """Plan, validate and (if everything passes and `out` is given) write the export.

    Returns (exit code, report). Raises UsageError for usage problems."""
    if target_name not in TARGETS:
        raise UsageError(f"unknown target {target_name!r}; choose one of: {', '.join(TARGETS)}")
    target = TARGETS[target_name]
    if license_text is not None and not license_text.strip():
        raise UsageError("--license must not be empty")
    slugs = resolve_slugs(root, slugs)
    if out is not None:
        check_out_dir(out, root, slugs)
    before = {s: _sha256_or_none(root / s / "SKILL.md") for s in slugs}
    plans = [plan_skill(root, s, target, license_text) for s in slugs]
    report = {
        "target": target.name,
        "target_sources": list(target.sources),
        "version": _read_version(root),
        "skills": [report_entry(p, target) for p in plans],
    }
    if any(p.problems for p in plans) or out is None:
        return (1 if any(p.problems for p in plans) else 0), report
    out.mkdir(parents=True, exist_ok=True)
    post_problems = []
    for p in plans:
        dest = out / p.slug
        for rel, data in p.files.items():
            path = dest / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
        if (dest / "SKILL.md").read_bytes() != p.skill_md:
            post_problems.append(f"{p.slug}: written SKILL.md differs from the planned bytes")
        if make_zip:
            write_zip(out / f"{p.slug}.zip", p.slug, p.files)
    after = {s: _sha256_or_none(root / s / "SKILL.md") for s in slugs}
    post_problems += [f"{s}: canonical SKILL.md changed during the export" for s in slugs if before[s] != after[s]]
    report["post_write_problems"] = post_problems
    (out / REPORT_NAME).write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8",
                                   newline="\n")
    return (1 if post_problems else 0), report


def _sha256_or_none(path: Path) -> str | None:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return None


def _read_version(root: Path) -> str | None:
    try:
        return (root / "VERSION").read_text(encoding="utf-8").strip()
    except OSError:
        return None


def print_human(report: dict, out: Path | None, code: int) -> None:
    for entry in report["skills"]:
        if entry["problems"]:
            print(f"FAIL  {entry['slug']}")
            for prob in entry["problems"]:
                print(f"        - {prob}")
            continue
        moved = ", ".join(m["key"] for m in entry["moved_keys"]) or "none"
        print(f"ok    {entry['slug']}: moved to metadata: {moved}")
        for a in entry["added_keys"]:
            print(f"        {a['action']} {a['key']}: {a['value']}")
        for x in entry["excluded_files"]:
            print(f"        excluded (package_skill.py leaves it out): {x}")
    for prob in report.get("post_write_problems", []):
        print(f"FAIL  {prob}")
    n = len(report["skills"])
    if code:
        print(f"\n{sum(1 for e in report['skills'] if e['problems'])} of {n} skill(s) do not meet "
              f"{report['target']}; nothing was written." if not report.get("post_write_problems")
              else "\npost-write checks failed")
    elif out is None:
        print(f"\n{n} skill(s) meet {report['target']} (dry run, nothing written).")
    else:
        print(f"\n{n} skill(s) exported for {report['target']} to {out} (report: {out / REPORT_NAME}). "
              "No key was dropped. Acceptance by the real upload path is not verified by this script.")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("slugs", nargs="*", help="skill slugs to export (default: every skill)")
    ap.add_argument("--target", help=f"one of: {', '.join(TARGETS)}")
    ap.add_argument("--out", type=Path, help="new or empty output directory (omit for a dry run)")
    ap.add_argument("--root", type=Path, default=sl.REPO_ROOT, help="skills root (default: this repo)")
    ap.add_argument("--zip", action="store_true", help="also write a deterministic <slug>.zip per skill")
    ap.add_argument("--license", dest="license_text", help="set the spec `license` field, e.g. MIT")
    ap.add_argument("--json", action="store_true", help="print the report as JSON")
    ap.add_argument("--list-targets", action="store_true")
    args = ap.parse_args(argv)

    if args.list_targets:
        for t in TARGETS.values():
            print(f"{t.name:18} {t.summary}")
            for s in t.sources:
                print(f"{'':18}   {s}")
        return 0
    if not args.target:
        print("error: --target is required (see --list-targets)", file=sys.stderr)
        return 2
    if args.zip and args.out is None:
        print("error: --zip needs --out", file=sys.stderr)
        return 2
    try:
        code, report = export(args.root, args.slugs, args.target, args.out,
                              license_text=args.license_text, make_zip=args.zip)
    except UsageError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    if args.json:
        print(json.dumps(report, indent=2, ensure_ascii=False))
    else:
        print_human(report, args.out, code)
    return code


if __name__ == "__main__":
    sys.exit(main())
