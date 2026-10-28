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
    python scripts/validate_skills.py --root DIR      # validate a different tree (fixtures)

This script is deliberately conservative about what counts as a hard ERROR
vs. a WARNING: things the spec makes non-negotiable (frontmatter matching
schema/skill-frontmatter.schema.json, YAML the parser can represent exactly,
required sections, minimum gotcha/reference counts, existing dependency slugs,
no cycles, links that survive packaging) are ERRORs. Things that are useful
signals but have real false-positive rates (Python-syntax-looking-wrong on an
intentionally partial snippet, missing code fence on a step that legitimately
continues the previous one, routing-tag overlap) are WARNINGs. See
docs/SKILL-SPEC.md "Validation rules" for the full rule table.

Every finding carries a stable `code`, the human `rule` text, the `file`
(relative to --root), the frontmatter `field` when there is one, a `line`
when it is known, and a `fix` remediation string.
"""
from __future__ import annotations

import argparse
import ast
import datetime as dt
import difflib
import functools
import json
import re
import sys
import urllib.parse
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "schema"))

import allowed_values as av  # noqa: E402
import skills_lib as sl  # noqa: E402
import generate_index as gi  # noqa: E402
import generate_schema as gs  # noqa: E402
import build_dependency_graph as bdg  # noqa: E402

SLUG_RE = re.compile(av.SLUG_PATTERN)
XML_TAG_RE = re.compile(r"</?[A-Za-z][A-Za-z0-9_.:-]*(?:\s[^<>]*)?/?>")
# Link destinations in every form CommonMark/HTML renders as a link, so a link
# that leaves the skill folder can't slip past by switching syntax: inline
# `[t](url "title")` / `[t](<url>)`, reference definitions `[id]: url`, and raw
# HTML href/src attributes.
LINK_RE = re.compile(r"""\[([^\]]*)\]\(\s*(?:<([^<>\n]*)>|((?:[^\s()<>]|\([^\s()<>]*\))+))(?:\s+(?:"[^"]*"|'[^']*'|\([^)]*\)))?\s*\)""")
REF_DEF_RE = re.compile(r"^ {0,3}\[(?!\^)[^\]]+\]:[ \t]*(?:<([^<>\n]*)>|(\S+))")  # `[^1]:` is a footnote
HTML_LINK_RE = re.compile(r"""<[A-Za-z][^<>]*?\b(?:href|src)\s*=\s*(?:"([^"]*)"|'([^']*)'|([^\s"'>]+))""", re.I)
WIKI_LINK_RE = re.compile(r"\[\[([^\[\]\n]+)\]\]")
SCHEME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9+.-]*:")
# Absolute local paths: `file:` URLs and Windows drive paths (`C:/x`, `C:\x`),
# which SCHEME_RE would otherwise mistake for a harmless scheme like mailto:.
LOCAL_ABS_RE = re.compile(r"^(?:file:|[A-Za-z]:[\\/])", re.I)
# "<dist><specifier>: <API> (<verification>)" — see docs/SKILL-SPEC.md.
_SPEC = r"(?:===|==|!=|~=|>=|<=|<|>)\s*[A-Za-z0-9.*+!_-]+"
VERSION_CONSTRAINT_RE = re.compile(
    rf"^[A-Za-z0-9][A-Za-z0-9._-]*(?:\s*{_SPEC}(?:\s*,\s*{_SPEC})*)?: \S.*\(.+\)\.?$")
# last_verified may be at most this far ahead of the validator's clock, so a
# reviewer east of UTC dating a skill "today" doesn't fail a UTC CI runner.
FUTURE_DATE_GRACE = dt.timedelta(days=1)
KNOWN_KEYS = tuple(av.REQUIRED_KEYS) + tuple(k for k in av.OPTIONAL_KEYS if k not in av.REQUIRED_KEYS)

# code -> (severity, rule text, default fix). The code is the stable,
# machine-matchable id; docs/SKILL-SPEC.md documents every one of them
# (tests/test_validate_skills.py fails if one is missing there).
RULES: dict[str, tuple[str, str, str]] = {
    "frontmatter-malformed": ("error", "malformed frontmatter",
                              "start the file with a `---` line and close the frontmatter with another `---` line"),
    "yaml-syntax": ("error", "frontmatter YAML outside the supported subset",
                    "rewrite the line using the YAML subset in docs/SKILL-SPEC.md (quote values containing ': ', "
                    "use spaces not tabs, one key per line)"),
    "missing-required-key": ("error", "missing required metadata",
                             "add the key with a non-empty value (see docs/SKILL-SPEC.md 'Frontmatter contract')"),
    "unknown-key": ("error", "unknown frontmatter key",
                    "rename the key to a documented one, or add it deliberately to OPTIONAL_KEYS in "
                    "schema/allowed_values.py and regenerate the schema (Claude Code silently ignores keys it "
                    "does not recognise)"),
    "invalid-enum": ("error", "invalid metadata value", "use one of the allowed values listed in `allowed`"),
    "invalid-type": ("error", "wrong YAML type for field",
                     "list keys take `- item` lines (or `[a, b]`); scalar keys take a single string"),
    "invalid-date": ("error", "last_verified is not a valid calendar date (YYYY-MM-DD)",
                     "use a real date in YYYY-MM-DD form, e.g. 2026-09-21"),
    "future-date": ("error", "last_verified is in the future",
                    "set last_verified to the date the review actually happened (today or earlier)"),
    "invalid-list-item": ("error", "list item is not a lowercase-kebab slug/tag",
                          "use lowercase letters, digits and single hyphens, e.g. `bbox-mask-aware-augmentation`"),
    "duplicate-list-item": ("error", "duplicate list item", "remove the repeated item"),
    "name-mismatch": ("error", "name != folder name", "make `name` equal the folder name (or rename the folder)"),
    "invalid-folder-name": ("error", "invalid directory naming (must be lowercase-kebab)",
                            "rename the folder to lowercase-kebab and update `name` and INDEX.md to match"),
    "invalid-name": ("error", "name is not lowercase-kebab", "use lowercase letters, digits and single hyphens"),
    "name-too-long": ("error", f"name longer than {av.NAME_MAX_LENGTH} characters",
                      f"shorten the slug to at most {av.NAME_MAX_LENGTH} characters (Claude Agent Skills limit)"),
    "name-reserved-word": ("error", "name contains a reserved word",
                           f"remove {' / '.join(av.RESERVED_NAME_WORDS)} from the name (reserved by Claude Agent Skills)"),
    "description-too-long": ("error", f"description longer than {av.DESCRIPTION_MAX_LENGTH} characters",
                             f"tighten the description to at most {av.DESCRIPTION_MAX_LENGTH} characters "
                             "(Claude Agent Skills limit); move detail into ## Overview"),
    "description-xml-tag": ("error", "description contains an XML-like tag",
                            "remove `<tag>`-style text from the description (Claude Agent Skills forbids XML "
                            "tags there); write placeholders as plain words"),
    "description-not-for": ("error", "description missing 'NOT for ...' clause",
                            "end the description with 'NOT for <adjacent task> (see <sibling-slug>).'"),
    "description-trigger-phrases": ("error", "description has fewer than 3 quoted trigger phrases",
                                    'add at least three "quoted" phrases a user would actually type'),
    "self-reference": ("error", "skill references itself", "remove the skill's own slug from the list"),
    "requires-conflicts-overlap": ("error", "slug listed in both requires and conflicts",
                                   "a skill cannot be both a hard prerequisite and contradictory; keep one"),
    "unknown-skill-reference": ("error", "relationship references unknown skill",
                                "fix the slug or remove it; it must name an existing skill folder"),
    "duplicate-name": ("error", "duplicate skill name", "give each skill a unique `name` equal to its folder"),
    "requires-cycle": ("error", "dependency cycle in 'requires' graph",
                       "break the cycle: demote one edge from `requires` to `related`"),
    "missing-section": ("error", "missing required section", "add the `## <section>` header with its content"),
    "wrong-section-for-type": ("error", "section set does not match the skill type",
                               "workflow skills use ## Workflow (no ## Key Concepts); reference skills use "
                               "## Key Concepts (no ## Workflow)"),
    "too-few-gotchas": ("error", "fewer than 3 gotchas", "add specific, real pitfalls as top-level `- ` bullets"),
    "too-few-references": ("error", "fewer than 2 references",
                           "add authoritative links as top-level `- [title](url) — reason` bullets"),
    "no-code-blocks": ("error", "workflow skill has zero code blocks", "add runnable fenced code to the Workflow steps"),
    "unbalanced-fence": ("error", "unbalanced ``` code fence (odd count)", "close every ``` fence"),
    "broken-link": ("error", "broken internal link", "fix the relative path or remove the link"),
    "link-escapes-skill": ("error", "relative link escapes the skill folder",
                           "link to an absolute https:// URL or a [[slug]] wiki-link instead; `../` paths break "
                           "when the folder is packaged or installed on its own"),
    "unresolved-wiki-link": ("error", "wiki-link does not resolve to a skill",
                             "use an existing skill slug inside [[...]]"),
    "index-mismatch": ("error", "INDEX.md out of sync with skill folders",
                       "run: python scripts/generate_index.py --fix (or edit INDEX.md by hand)"),
    "schema-stale": ("error", "schema/skill-frontmatter.schema.json is stale",
                     "run: python scripts/generate_schema.py --write"),
    # --- warnings ---
    "fewer-code-blocks-than-steps": ("warning",
                                     "fewer code blocks than numbered steps (some steps may lack runnable code)",
                                     "give each numbered step runnable code, or confirm it continues the previous block"),
    "python-syntax": ("warning", "python code block fails ast.parse (may be an intentionally partial snippet)",
                      "make the block valid Python, or mark it `# illustrative` if it is a deliberate fragment"),
    "url-unreachable": ("warning", "reference URL unreachable (checked live)",
                        "confirm the URL by hand; replace it if it has moved"),
    "conflicts-asymmetric": ("warning", "conflicts is not symmetric",
                             "add the reverse `conflicts` entry to the other skill (contradiction is mutual)"),
    "capability-collision": ("warning", "capability tag shared by multiple skills (routing collision)",
                             "make each skill's capability tag specific enough to tell the skills apart"),
    "version-constraint-format": ("warning", "version_constraints entry not in the documented format",
                                  'use "<dist><specifier>: <API> (<verification>)", e.g. '
                                  '"scikit-learn>=1.4: GridSearchCV(cv=...) (model-knowledge estimate, not '
                                  'live-verified)"'),
}

YAML_FIXES = {
    "duplicate-key": "delete one of the two declarations; YAML keeps only the last",
    "tab-indentation": "replace the tab(s) with spaces",
    "tab-character": "replace the tab(s) with spaces",
    "mapping-in-scalar": "wrap the whole value in double quotes",
    "implicit-type": "wrap the value in quotes so it stays a string",
    "unparseable-line": "write the line as `key: value` (keys at column 0, values on the same line)",
    "unexpected-indentation": "remove the indentation, or attach the line to a `- item`/block scalar",
    "unsupported-syntax": "quote the value, or use only the constructs in docs/SKILL-SPEC.md 'Supported YAML subset'",
    "invalid-escape": "use a valid YAML escape (\\\\, \\\", \\n, \\t, \\uXXXX) or a single-quoted value",
}


class Report:
    def __init__(self):
        self.errors: list[dict] = []
        self.warnings: list[dict] = []
        self._seen: set[tuple] = set()

    def add(self, skill: str, code: str, *, file: str, field: str | None = None, line: int | None = None,
            fix: str | None = None, **extra) -> None:
        severity, rule, default_fix = RULES[code]
        key = (skill, code, file, field, line, tuple(sorted((k, str(v)) for k, v in extra.items())))
        if key in self._seen:  # e.g. pattern + format both failing on one date
            return
        self._seen.add(key)
        entry = {"skill": skill, "rule": rule, "code": code, "file": file}
        if field is not None:
            entry["field"] = field
        if line is not None:
            entry["line"] = line
        entry.update(extra)
        entry["fix"] = fix or default_fix
        (self.errors if severity == "error" else self.warnings).append(entry)

    # Backwards-compatible aliases (severity comes from RULES, not the method).
    error = add
    warning = add

    def print_human(self, show_warnings: bool):
        groups = [("ERROR", self.errors)] + ([("WARNING", self.warnings)] if show_warnings else [])
        for label, items in groups:
            for e in items:
                loc = e["file"] + (f":{e['line']}" if "line" in e else "")
                print(f"{label} [{e['code']}] {loc}")
                print(f"  rule: {e['rule']}")
                for k, v in e.items():
                    if k in ("skill", "rule", "code", "file", "line", "fix"):
                        continue
                    print(f"  {k}: {v}")
                print(f"  fix: {e['fix']}")
                print()
        print(f"--- {len(self.errors)} error(s), {len(self.warnings)} warning(s) ---")


def skill_file(skill: sl.Skill) -> str:
    # Skill folders are always exactly one level below the root.
    return f"{skill.slug}/SKILL.md"


def _suggest_key(key: str) -> str | None:
    norm = key.strip().lower().replace("-", "_").replace(" ", "_")
    if norm in KNOWN_KEYS:
        return norm
    close = difflib.get_close_matches(norm, KNOWN_KEYS, n=1, cutoff=0.6)
    return close[0] if close else None


def load_schema() -> tuple[dict, list[str]]:
    """Return (schema, staleness problems). Uses the committed file when it is fresh."""
    problems = gs.check()
    if problems:
        return gs.build_schema(), problems
    return json.loads(gs.SCHEMA_PATH.read_text(encoding="utf-8")), []


def check_frontmatter(skill: sl.Skill, report: Report, today: dt.date | None = None,
                      schema: dict | None = None) -> None:
    fm = skill.frontmatter
    f = skill_file(skill)
    lines = skill.key_lines
    schema = schema if schema is not None else gs.build_schema()
    today = today or dt.date.today()

    for d in skill.diagnostics:
        report.add(skill.slug, "yaml-syntax", file=f, field=d.key, line=d.line, problem=d.code,
                   detail=d.message, fix=YAML_FIXES.get(d.code))

    for err in sl.schema_errors(schema, fm):
        field = err.property if err.keyword in ("required", "additionalProperties") else (
            str(err.path[0]) if err.path else None)
        line = lines.get(field) if field else None
        value = fm.get(field) if field else None
        item_level = len(err.path) > 1
        if err.keyword == "required":
            report.add(skill.slug, "missing-required-key", file=f, field=field)
        elif err.keyword == "additionalProperties":
            suggestion = _suggest_key(field)
            report.add(skill.slug, "unknown-key", file=f, field=field, line=line,
                       **({"suggestion": suggestion} if suggestion else {}),
                       fix=(f"rename `{field}` to `{suggestion}`" if suggestion else None))
        elif item_level:
            item = value[err.path[1]] if isinstance(value, list) else None
            if err.keyword == "pattern":
                report.add(skill.slug, "invalid-list-item", file=f, field=field, line=line, value=item)
            else:
                report.add(skill.slug, "invalid-type", file=f, field=field, line=line, value=item,
                           detail=err.message, fix="every list item must be a non-empty string")
        elif err.keyword in ("type", "minLength") and (value is None or value == "") and field in av.REQUIRED_KEYS:
            report.add(skill.slug, "missing-required-key", file=f, field=field, line=line)
        elif err.keyword == "type":
            report.add(skill.slug, "invalid-type", file=f, field=field, line=line, detail=err.message)
        elif err.keyword == "enum":
            report.add(skill.slug, "invalid-enum", file=f, field=field, line=line, value=value,
                       allowed=list(av.ENUM_KEYS.get(field, ())))
        elif err.keyword in ("pattern", "format") and field == av.DATE_KEY:
            report.add(skill.slug, "invalid-date", file=f, field=field, line=line, value=value)
        elif err.keyword == "pattern" and field == "name":
            report.add(skill.slug, "invalid-name", file=f, field=field, line=line, value=value)
        elif err.keyword == "maxLength" and field == "name":
            report.add(skill.slug, "name-too-long", file=f, field=field, line=line, length=len(value))
        elif err.keyword == "maxLength" and field == "description":
            report.add(skill.slug, "description-too-long", file=f, field=field, line=line, length=len(value))
        elif err.keyword == "uniqueItems":
            # Items may be unhashable (a nested list the parser already flagged),
            # so find duplicates by equality instead of through a set.
            repeats = [x for i, x in enumerate(value) if x in value[:i]]
            dupes = sorted((x for i, x in enumerate(repeats) if x not in repeats[:i]), key=str)
            report.add(skill.slug, "duplicate-list-item", file=f, field=field, line=line, value=dupes)
        else:  # a keyword the generator emits but this mapping doesn't know yet: still an error
            report.add(skill.slug, "invalid-type", file=f, field=field, line=line, detail=err.message,
                       fix="see schema/skill-frontmatter.schema.json")

    name = fm.get("name")
    if isinstance(name, str) and name:
        if name != skill.slug:
            report.add(skill.slug, "name-mismatch", file=f, field="name", line=lines.get("name"),
                       name=name, folder=skill.slug)
        hits = [w for w in av.RESERVED_NAME_WORDS if w in name.lower()]
        if hits:
            report.add(skill.slug, "name-reserved-word", file=f, field="name", line=lines.get("name"), value=hits)

    if not SLUG_RE.match(skill.slug):
        report.add(skill.slug, "invalid-folder-name", file=f)

    lv = fm.get(av.DATE_KEY)
    if isinstance(lv, str) and sl.is_calendar_date(lv) and dt.date.fromisoformat(lv) > today + FUTURE_DATE_GRACE:
        report.add(skill.slug, "future-date", file=f, field=av.DATE_KEY, line=lines.get(av.DATE_KEY),
                   value=lv, today=today.isoformat())

    desc = fm.get("description")
    if isinstance(desc, str):
        dline = lines.get("description")
        if XML_TAG_RE.search(desc):
            report.add(skill.slug, "description-xml-tag", file=f, field="description", line=dline,
                       value=XML_TAG_RE.search(desc).group(0))
        if "not for" not in desc.lower():
            report.add(skill.slug, "description-not-for", file=f, field="description", line=dline)
        quote_pairs = desc.count('"') // 2
        if quote_pairs < 3:
            report.add(skill.slug, "description-trigger-phrases", file=f, field="description", line=dline,
                       found=quote_pairs)

    vcs = fm.get("version_constraints")
    for vc in vcs if isinstance(vcs, list) else []:  # a scalar is already invalid-type; don't walk its chars
        if isinstance(vc, str) and vc and not VERSION_CONSTRAINT_RE.match(vc):
            report.add(skill.slug, "version-constraint-format", file=f, field="version_constraints",
                       line=lines.get("version_constraints"), value=vc[:80])


def check_sections(skill: sl.Skill, report: Report) -> None:
    f = skill_file(skill)
    headers = skill.headers()
    t = skill.frontmatter.get("type")

    for section in ("Overview", "Gotchas", "References"):
        if section not in headers:
            report.add(skill.slug, "missing-section", file=f, section=section)

    if t == "workflow":
        if "Workflow" not in headers:
            report.add(skill.slug, "missing-section", file=f, section="Workflow",
                       fix="workflow skills must have a ## Workflow section with numbered, runnable steps")
        if "Key Concepts" in headers:
            report.add(skill.slug, "wrong-section-for-type", file=f, section="Key Concepts", type=t)
    elif t == "reference":
        if "Key Concepts" not in headers:
            report.add(skill.slug, "missing-section", file=f, section="Key Concepts",
                       fix="reference skills must have a ## Key Concepts section")
        if "Workflow" in headers:
            report.add(skill.slug, "wrong-section-for-type", file=f, section="Workflow", type=t)

    gotchas = skill.bullets_under("Gotchas")
    if len(gotchas) < 3:
        report.add(skill.slug, "too-few-gotchas", file=f, section="Gotchas", found=len(gotchas))

    refs = skill.bullets_under("References")
    if len(refs) < 2:
        report.add(skill.slug, "too-few-references", file=f, section="References", found=len(refs))

    if t == "workflow":
        code_blocks = skill.code_blocks(lang=None)
        if len(code_blocks) == 0:
            report.add(skill.slug, "no-code-blocks", file=f, section="Workflow")
        step_count = len(re.findall(r"^\d+\.\s+\*\*", skill.body, flags=re.MULTILINE))
        if step_count and len(code_blocks) < step_count:
            report.add(skill.slug, "fewer-code-blocks-than-steps", file=f, section="Workflow",
                       steps=step_count, code_blocks=len(code_blocks))


def check_code_fences(skill: sl.Skill, report: Report) -> None:
    f = skill_file(skill)
    fence_count = skill.body.count("```")
    if fence_count % 2 != 0:
        report.add(skill.slug, "unbalanced-fence", file=f, count=fence_count)

    for _lang, code in skill.code_blocks(lang="python"):
        error = _python_syntax_error(code)
        if error:
            report.add(skill.slug, "python-syntax", file=f, error=error)


@functools.lru_cache(maxsize=4096)
def _python_syntax_error(code: str) -> str | None:
    # Memoized: compiling every block dominates a run, and fixture/mutation
    # tests re-validate the same unchanged blocks many times per process.
    try:
        ast.parse(code)
    except SyntaxError as e:
        return str(e)
    return None


def _prose_lines(skill: sl.Skill):
    offset = skill.body_line_offset()
    for n, line in enumerate(skill.prose().split("\n"), start=1):
        yield offset + n, line


def _link_urls(line: str):
    """Every link destination on one prose line (inline, reference definition, HTML)."""
    if "](" in line:
        for _text, angle, bare in LINK_RE.findall(line):
            yield angle or bare
    if "]:" in line:
        m = REF_DEF_RE.match(line)
        if m:
            yield m.group(1) or m.group(2)
    if "<" in line and "=" in line:
        for groups in HTML_LINK_RE.findall(line):
            yield "".join(groups)  # exactly one alternative matched


def check_links(skill: sl.Skill, report: Report, live: bool) -> None:
    f = skill_file(skill)
    skill_dir = None  # resolved lazily: most skills have no relative links
    for line_no, line in _prose_lines(skill):
        for url in _link_urls(line):
            url = url.strip()
            if url.startswith(("http://", "https://")):
                if not live:
                    continue
                try:
                    # S310 is suppressed below: the startswith(("http://", "https://")) guard rules out file:/custom schemes.
                    req = urllib.request.Request(url, method="HEAD", headers={"User-Agent": "skill-validator/1.0"})  # noqa: S310
                    urllib.request.urlopen(req, timeout=6)  # noqa: S310
                except Exception as e:  # noqa: BLE001 - any failure is a WARNING, not fatal
                    report.add(skill.slug, "url-unreachable", file=f, line=line_no, url=url, error=str(e))
                continue
            if LOCAL_ABS_RE.match(url):
                # Can't resolve once the folder is installed elsewhere; don't probe the local disk.
                report.add(skill.slug, "link-escapes-skill", file=f, line=line_no, url=url)
                continue
            if not url or url.startswith("#") or SCHEME_RE.match(url):
                continue  # in-page anchor, mailto:, etc.
            # Browsers and most renderers treat `\` in a URL path as `/`; normalise
            # so `..\x` is caught the same way on every OS.
            path = urllib.parse.unquote(url.split("#", 1)[0].split("?", 1)[0]).replace("\\", "/")
            if not path:
                continue
            skill_dir = skill_dir or skill.path.parent.resolve()
            target = (skill_dir / path).resolve()
            if path.startswith("/") or not target.is_relative_to(skill_dir):
                report.add(skill.slug, "link-escapes-skill", file=f, line=line_no, url=url)
            if not target.exists():
                report.add(skill.slug, "broken-link", file=f, line=line_no, url=url)


def check_wiki_links(skill: sl.Skill, report: Report, slugs: set[str]) -> None:
    f = skill_file(skill)
    for line_no, line in _prose_lines(skill):
        for inner in WIKI_LINK_RE.findall(line):
            target = inner.split("|", 1)[0].split("#", 1)[0].strip()
            if target not in slugs:
                close = difflib.get_close_matches(target, sorted(slugs), n=1)
                report.add(skill.slug, "unresolved-wiki-link", file=f, line=line_no, target=target,
                           fix=(f"did you mean [[{close[0]}]]?" if close else None))


def _list(skill: sl.Skill, key: str) -> list:
    v = skill.frontmatter.get(key)
    return [x for x in v if isinstance(x, str)] if isinstance(v, list) else []


def check_relationships(skills: list[sl.Skill], report: Report, known_slugs: set[str] | None = None) -> None:
    slugs = {s.slug for s in skills} | (known_slugs or set())
    by_slug = sl.by_slug(skills)
    seen_names: dict[str, str] = {}
    capability_owners: dict[str, list[str]] = {}
    for s in skills:
        f = skill_file(s)
        name = s.frontmatter.get("name")
        if isinstance(name, str) and name in seen_names and seen_names[name] != s.slug:
            report.add(s.slug, "duplicate-name", file=f, field="name", line=s.key_lines.get("name"),
                       name=name, also_in=seen_names[name])
        elif isinstance(name, str) and name:
            seen_names[name] = s.slug

        for field in ("related", "requires", "conflicts"):
            line = s.key_lines.get(field)
            for target in _list(s, field):
                if target == s.slug:
                    report.add(s.slug, "self-reference", file=f, field=field, line=line, target=target)
                elif target not in slugs:
                    close = difflib.get_close_matches(target, sorted(slugs), n=1)
                    report.add(s.slug, "unknown-skill-reference", file=f, field=field, line=line, target=target,
                               fix=(f"did you mean `{close[0]}`?" if close else None))
        for target in sorted(set(_list(s, "requires")) & set(_list(s, "conflicts"))):
            report.add(s.slug, "requires-conflicts-overlap", file=f, field="conflicts",
                       line=s.key_lines.get("conflicts"), target=target)
        for target in _list(s, "conflicts"):
            other = by_slug.get(target)
            if other is not None and target != s.slug and s.slug not in _list(other, "conflicts"):
                report.add(s.slug, "conflicts-asymmetric", file=f, field="conflicts",
                           line=s.key_lines.get("conflicts"), target=target,
                           fix=f"add `{s.slug}` to `conflicts` in {target}/SKILL.md")
        for cap in set(_list(s, "capabilities")):
            capability_owners.setdefault(cap, []).append(s.slug)

    for cap, owners in sorted(capability_owners.items()):
        if len(owners) > 1:
            for owner in owners:
                report.add(owner, "capability-collision", file=f"{owner}/SKILL.md", field="capabilities",
                           line=by_slug[owner].key_lines.get("capabilities"), value=cap,
                           shared_with=[o for o in owners if o != owner])

    for cyc in bdg.find_cycles(skills):
        head = by_slug.get(cyc[0])
        report.add(cyc[0], "requires-cycle", file=f"{cyc[0]}/SKILL.md", field="requires",
                   line=head.key_lines.get("requires") if head else None, cycle=" -> ".join(cyc))


_SKILL_CACHE: dict[tuple[str, str], sl.Skill] = {}


def _load_skill_cached(skill_dir: Path) -> sl.Skill:
    """sl.load_skill, memoized on (path, full file text) so it can never be stale.

    Only matters when run() is called repeatedly in one process (the fixture
    mutation tests); the checks never mutate a Skill, so sharing is safe.
    """
    raw = (skill_dir / "SKILL.md").read_text(encoding="utf-8")
    key = (str(skill_dir), raw)
    skill = _SKILL_CACHE.get(key)
    if skill is None:
        if len(_SKILL_CACHE) > 2048:
            _SKILL_CACHE.clear()
        skill = sl.load_skill(skill_dir)
        if skill.raw != raw:  # file changed between the two reads; don't cache
            return skill
        _SKILL_CACHE[key] = skill
    return skill


def run(root: Path = sl.REPO_ROOT, *, live: bool = False, today: dt.date | None = None) -> Report:
    """Validate the skill tree under `root` and return the report."""
    root = Path(root).resolve()
    report = Report()
    schema, schema_problems = load_schema()
    for problem in schema_problems:
        report.add("<schema>", "schema-stale", file="schema/skill-frontmatter.schema.json", detail=problem)

    skills: list[sl.Skill] = []
    unloadable: set[str] = set()
    for d in sl.iter_skill_dirs(root):
        try:
            skills.append(_load_skill_cached(d))
        except (sl.FrontmatterError, UnicodeDecodeError) as e:
            unloadable.add(d.name)
            report.add(d.name, "frontmatter-malformed", file=f"{d.name}/SKILL.md", error=str(e))

    slugs = {s.slug for s in skills} | unloadable
    for skill in skills:
        check_frontmatter(skill, report, today=today, schema=schema)
        check_sections(skill, report)
        check_code_fences(skill, report)
        check_links(skill, report, live=live)
        check_wiki_links(skill, report, slugs)

    check_relationships(skills, report, known_slugs=unloadable)

    for issue in gi.check_index_consistency(skills, root / "INDEX.md"):
        report.add("INDEX.md", "index-mismatch", file="INDEX.md", detail=issue)
    return report


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--warnings", action="store_true", help="also print warnings")
    ap.add_argument("--strict", action="store_true", help="warnings also cause a non-zero exit")
    ap.add_argument("--check-links", action="store_true", help="live-check https:// reference URLs (network required)")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--root", type=Path, default=sl.REPO_ROOT,
                    help="directory containing the skill folders and INDEX.md (default: this repo)")
    ap.add_argument("--today", type=dt.date.fromisoformat, default=None,
                    help="override today's date (YYYY-MM-DD) for the future-date rule; for reproducible runs")
    args = ap.parse_args(argv)

    if not args.root.is_dir():
        print(f"--root {args.root} is not a directory", file=sys.stderr)
        return 2
    report = run(args.root, live=args.check_links, today=args.today)

    if args.json:
        print(json.dumps({"errors": report.errors, "warnings": report.warnings}, indent=2, default=str))
    else:
        report.print_human(show_warnings=args.warnings or args.strict)

    if report.errors:
        return 1
    if args.strict and report.warnings:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
