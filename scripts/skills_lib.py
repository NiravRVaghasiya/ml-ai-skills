"""Shared utilities for the skill-library tooling (validate/router/graph/freshness/index).

Deliberately dependency-free (no PyYAML) so that `python scripts/whatever.py`
works on a bare Python 3.10+ install with no `pip install` step, per the
repo's "don't add unnecessary dependency managers" policy (CLAUDE.md /
docs/SKILL-SPEC.md).

The frontmatter parser below implements a documented *subset* of YAML (see
docs/SKILL-SPEC.md "Supported YAML subset"). Inside that subset its result is
meant to be identical to `yaml.safe_load` (dates aside: they stay ISO
strings) because Claude Code reads the same frontmatter with a real YAML
parser — any divergence means the validator checks something other than
what ships. Everything outside the subset is not guessed at: it produces a
`Diagnostic` (with a line number) that validate_skills.py reports as an
ERROR, instead of being silently dropped or mis-read.
"""
from __future__ import annotations

import functools
import re
import textwrap
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, NamedTuple

REPO_ROOT = Path(__file__).resolve().parent.parent

# Directories that are NOT skills even though they may contain a SKILL.md
# (the template) or sit at repo root.
NON_SKILL_DIRS = {"_TEMPLATE", ".git", ".claude", "scripts", "schema", "tests", "docs", "evals", ".github"}


class FrontmatterError(ValueError):
    pass


@dataclass(frozen=True)
class Diagnostic:
    """A frontmatter construct the parser could not represent faithfully."""

    line: int  # 1-based line number in the file the text came from
    code: str  # stable kebab-case id, e.g. "duplicate-key"
    message: str
    key: str | None = None  # top-level key the problem belongs to, if known

    def __str__(self) -> str:
        return f"line {self.line}: {self.message}"


class ParseResult(NamedTuple):
    data: dict[str, Any]
    diagnostics: list[Diagnostic]
    key_lines: dict[str, int]  # top-level key -> 1-based line it was declared on


@dataclass
class Skill:
    slug: str  # folder name
    path: Path  # path to SKILL.md
    frontmatter: dict[str, Any]
    body: str  # markdown after the closing `---`
    raw: str  # full file text
    diagnostics: list[Diagnostic] = field(default_factory=list)
    key_lines: dict[str, int] = field(default_factory=dict)

    def __getattr__(self, item):  # convenience: skill.type instead of skill.frontmatter["type"]
        if item.startswith("__"):  # keep copy/pickle protocol lookups from recursing
            raise AttributeError(item)
        try:
            return self.frontmatter[item]
        except KeyError as e:
            raise AttributeError(item) from e

    def markdown(self) -> MarkdownScan:
        """The body's line/block structure (fences, HTML comments, headings), cached per body."""
        cached = self.__dict__.get("_scan")
        if cached is None or cached[0] is not self.body:
            cached = (self.body, scan_markdown(self.body))
            self.__dict__["_scan"] = cached
        return cached[1]

    def headers(self) -> list[str]:
        """Return the text of every level-2 ATX heading (`## X`) in the body, in order.

        Headings inside fenced code blocks and HTML comments are not headings;
        a closing `#` sequence (`## Gotchas ##`) is not part of the text.
        """
        return [h.text for h in self.markdown().headings if h.level == 2]

    def bullets_under(self, header: str) -> list[str]:
        """Return top-level `- ` bullet lines under the first `## Header` section.

        The section runs to the next level-1 or level-2 heading. Lines inside
        fenced code blocks and HTML comments are not bullets.
        """
        scan = self.markdown()
        k = next((k for k, h in enumerate(scan.headings) if h.level == 2 and h.text == header), None)
        if k is None:
            return []
        h = scan.headings[k]
        end = next((x.line for x in scan.headings[k + 1:] if x.level <= 2), len(scan.lines))
        return [scan.lines[i] for i in range(h.line + 1, end)
                if scan.kinds[i] == "text" and _BULLET_RE.match(scan.lines[i])
                and not _THEMATIC_BREAK_RE.match(scan.lines[i])]

    def code_blocks(self, lang: str | None = "python") -> list[tuple[str, str]]:
        """Return (lang, code) for every fenced code block in the body.

        Both ```` ``` ```` and `~~~` fences count (any length >= 3; the
        closing fence uses the same character and is at least as long), and
        `lang` is the first word of the info string. An unclosed fence runs
        to the end of the body, as in CommonMark.

        Code fences nested under a numbered-list step are themselves indented
        in the raw Markdown (e.g. 3 spaces to align with "1. "). That
        indentation is part of the Markdown, not the Python — dedent it here
        so downstream consumers (e.g. validate_skills.py's ast.parse check)
        see the code as it's meant to run, not as it's laid out on the page.
        """
        blocks = [(b.lang, textwrap.dedent(b.code)) for b in self.markdown().fences]
        if lang is None:
            return blocks
        return [(lang_tag, c) for lang_tag, c in blocks if lang_tag.lower() == lang]

    def markdown_links(self) -> list[tuple[str, str]]:
        """Return (link_text, url) for every markdown [text](url) in the body."""
        return re.findall(r"\[([^\]]+)\]\((https?://[^\s)]+|[^\s)]+)\)", self.body)

    def prose(self) -> str:
        """The body with fenced code blocks and inline code spans blanked out.

        Line count is preserved (code lines become empty) so callers can still
        report line numbers. Used by link/wiki-link checks: `df[["a", "b"]]`
        or `x[i](y)` inside code is not a link.
        """
        cached = self.__dict__.get("_prose")
        if cached is None or cached[0] is not self.body:
            cached = (self.body, strip_code(self.body))
            self.__dict__["_prose"] = cached
        return cached[1]

    def body_line_offset(self) -> int:
        """Number of file lines that precede the first body line."""
        return self.raw.count("\n") - self.body.count("\n")


# --------------------------------------------------------------------------
# Markdown structure (line based; see docs/SKILL-SPEC.md known limitation 6)
# --------------------------------------------------------------------------
# A deliberately small subset of CommonMark: enough to tell headings, bullets
# and code apart, not a renderer. Fences may be indented by any amount
# (skills nest them under numbered steps); a backtick fence's info string may
# not contain a backtick (so "```x```" on one line is inline code, not a fence).
_FENCE_OPEN_RE = re.compile(r"^[ \t]*(?P<fence>`{3,}(?=[^`]*$)|~{3,})(?P<info>.*)$")
_ATX_RE = re.compile(r"^ {0,3}(?P<hashes>#{1,6})(?:[ \t]+(?P<text>.*?))?[ \t]*$")
_ATX_CLOSE_RE = re.compile(r"(?:^|[ \t]+)#+[ \t]*$")
# Setext detection works on a line's text after its indentation (`_indent`), so
# it also sees headings inside list items; the column checks are in scan_markdown.
_SETEXT_UNDERLINE_RE = re.compile(r"^(?:=+|-+)[ \t]*$")
_LIST_MARKER_RE = re.compile(r"^(?P<marker>[-*+]|(?P<num>[0-9]{1,9})[.)])(?P<gap>[ \t]+|$)")
# Blocks a `---` under which is not a setext underline: quotes, tables, HTML, ATX headings.
_OTHER_BLOCK_START_RE = re.compile(r"^(?:>|\||<|#{1,6}(?:[ \t]|$))")
_BULLET_RE = re.compile(r"^-[ \t]+\S")
_THEMATIC_BREAK_RE = re.compile(r"^ {0,3}(?:(?:-[ \t]*){3,}|(?:\*[ \t]*){3,}|(?:_[ \t]*){3,})$")
_HTML_COMMENT_START_RE = re.compile(r"^ {0,3}<!--")
_INLINE_CODE_RE = re.compile(r"(`+)(?:(?!\1).)+?\1")


class Heading(NamedTuple):
    line: int  # 0-based line index in the body
    level: int
    text: str


class Fence(NamedTuple):
    line: int  # 0-based index of the opening fence line
    end: int | None  # 0-based index of the closing fence line; None if never closed
    marker: str  # the opening fence, e.g. "```" or "~~~~"
    lang: str  # first word of the info string ("" if none)
    code: str  # content lines, each ending in "\n" (not yet dedented)


class MarkdownScan(NamedTuple):
    lines: list[str]
    # Per line: "text", "fence" (an opening/closing fence line), "code" (inside
    # a fence) or "html" (inside an HTML comment block).
    kinds: list[str]
    headings: list[Heading]  # ATX headings outside code and comments
    fences: list[Fence]
    setext: list[Heading]  # setext-style headings (text + `===`/`---` underline); the validator rejects them


def _indent(line: str) -> tuple[int, str]:
    """(column of the first non-blank character, tabs at 4-column stops; the rest of the line)."""
    rest = line.lstrip(" \t")
    return len(line[:len(line) - len(rest)].expandtabs(4)), rest


def scan_markdown(markdown: str) -> MarkdownScan:
    """Classify each line of `markdown` as text, fenced code or HTML comment."""
    lines = markdown.split("\n")
    kinds: list[str] = []
    headings: list[Heading] = []
    fences: list[Fence] = []
    setext: list[Heading] = []
    open_fence: tuple[int, str, str] | None = None  # (line, marker, lang)
    code: list[str] = []
    in_comment = False
    # Setext state, a CommonMark approximation: the current run of text lines,
    # its kind ("para"; or "code", "other" (a quote) or "toblank" (an HTML block
    # or table, which runs to the next blank line), under which `---` is never
    # an underline), the column its paragraph text starts at, and the content columns of the
    # list items it may sit in (innermost last).
    para_start: int | None = None
    para_kind = ""
    para_base = 0
    items: list[int] = []
    empty_item = False  # the previous line was a list marker with no content
    for i, line in enumerate(lines):
        if open_fence is not None:
            start, marker, lang = open_fence
            close = line.strip(" \t")
            if close and set(close) == {marker[0]} and len(close) >= len(marker):
                fences.append(Fence(start, i, marker, lang, "".join(code)))
                open_fence, code = None, []
                kinds.append("fence")
            else:
                code.append(line + "\n")
                kinds.append("code")
            continue
        if in_comment:
            kinds.append("html")
            in_comment = "-->" not in line
            continue
        m = _FENCE_OPEN_RE.match(line)
        if m:
            info = m.group("info").strip()
            open_fence = (i, m.group("fence"), info.split()[0] if info else "")
            kinds.append("fence")
            para_start = None
            continue
        c = _HTML_COMMENT_START_RE.match(line)
        if c:
            kinds.append("html")
            in_comment = "-->" not in line[c.end():]
            para_start = None
            continue
        kinds.append("text")
        if not line.strip(" \t"):
            if empty_item:  # a list item that starts with a blank line ends at the next one
                items.pop()
            para_start, empty_item = None, False
            continue
        h = _ATX_RE.match(line)
        if h:
            text = _ATX_CLOSE_RE.sub("", h.group("text") or "")
            headings.append(Heading(i, len(h.group("hashes")), text.strip(" \t")))
        col, rest = _indent(line)
        # A `===`/`---` line directly under a paragraph turns it into a heading
        # (CommonMark setext heading), including inside a list item when it is
        # indented to the item's content: "1. Step\n   text\n   ---".
        if para_start is not None and para_kind == "para" and para_base <= col < para_base + 4 \
                and _SETEXT_UNDERLINE_RE.match(rest):
            level = 1 if rest[0] == "=" else 2
            text = [lines[para_start].expandtabs(4)[para_base:].strip(), *(x.strip() for x in lines[para_start + 1:i])]
            setext.append(Heading(i, level, " ".join(x for x in text if x)))
            para_start = None
            continue
        base = max((c for c in items if c <= col), default=0)  # innermost list item this line is inside
        block = col - base <= 3  # 4+ columns past it is indented code, or a paragraph's continuation
        li = _LIST_MARKER_RE.match(rest) if block else None
        if li and para_start is not None and li.group("num") not in (None, "1") \
                and (para_kind == "other" or para_kind == "para" and col >= para_base):
            li = None  # an ordered item not starting at 1 cannot interrupt a paragraph (or a quote's lazy text)
        elif li and para_start is not None and para_kind == "para" and col >= para_base and not rest[li.end():]:
            li = None  # nor can an empty item; outside the paragraph's own container it is a sibling item
        rule = block and _THEMATIC_BREAK_RE.match(rest)
        other = block and _OTHER_BLOCK_START_RE.match(rest)
        if para_start is not None and (para_kind == "toblank" or para_kind in ("para", "other") and not (h or li or rule or other)):
            if para_kind == "para" and col < para_base and _THEMATIC_BREAK_RE.match(rest):
                para_kind = "other"  # a rule-like lazy line: parsers disagree, so no underline may follow
            continue  # continuation: of a paragraph (lazy or indented), a quote, or an HTML block or table
        items = [c for c in items if c <= col]
        empty_item = False
        if h or rule:
            para_start = None
        elif li:
            # Push the item's content column (and those of items nested on the same
            # line, "- 1. x"); its text is a paragraph unless it opens another block.
            while li:
                marker = li.group("marker")
                gap = len((" " * col + marker + li.group("gap")).expandtabs(4)) - col - len(marker)
                content = rest[li.end():]
                col += len(marker) + (gap if 1 <= gap <= 4 and content else 1)
                rest = content if gap <= 4 else " " * (gap - 1) + content  # 5+ spaces: indented code
                items.append(col)
                li = _LIST_MARKER_RE.match(rest) if rest and not _THEMATIC_BREAK_RE.match(rest) else None
            if not rest:
                para_start, empty_item = None, True
            elif _ATX_RE.match(rest) or _THEMATIC_BREAK_RE.match(rest):
                para_start = None  # a one-line block
            elif rest[0] in "<|":
                para_start, para_kind = i, "toblank"
            else:
                para_start, para_kind, para_base = i, "para", col
                if rest[0] in " \t":
                    para_kind = "code"
                elif _OTHER_BLOCK_START_RE.match(rest):
                    para_kind = "other"
        elif other:
            # A raw HTML block or a table runs to the next blank line; a quote absorbs
            # lazy continuation lines. A `---` under any of them is not a heading.
            para_start, para_kind = i, "toblank" if rest[0] in "<|" else "other"
        elif block:
            para_start, para_kind, para_base = i, "para", base
        else:
            para_start, para_kind = i, "code"
    if open_fence is not None:
        start, marker, lang = open_fence
        fences.append(Fence(start, None, marker, lang, "".join(code)))
    return MarkdownScan(lines, kinds, headings, fences, setext)


def strip_code(markdown: str) -> str:
    """Blank fenced code blocks (fence lines included) and inline code spans.

    Line count is preserved. HTML comments are kept: they are not code, and
    the link checks deliberately still look inside them.
    """
    scan = scan_markdown(markdown)
    return "\n".join(_INLINE_CODE_RE.sub("", line) if kind in ("text", "html") else ""
                     for line, kind in zip(scan.lines, scan.kinds, strict=True))


# --------------------------------------------------------------------------
# Frontmatter parsing
# --------------------------------------------------------------------------

# A top-level mapping key. YAML accepts far more (quoted keys, `? ` complex
# keys); those are diagnosed rather than parsed. Hyphens/dots/spaces are
# accepted on purpose: `risk-level: low` is a valid YAML key that Claude Code
# would read, so we must read it too and let the validator call it unknown.
_KEY_RE = re.compile(r"^(?P<key>[A-Za-z0-9_][A-Za-z0-9_.\- ]*?)[ ]*:(?:[ \t]+(?P<rest>.*)|[ \t]*)$")
_LIST_ITEM_RE = re.compile(r"^(?P<indent> *)-(?:[ \t]+(?P<rest>.*)|[ \t]*)$")
# At most one chomping indicator, before or after the digit (`|-+` is a YAML ScannerError).
_BLOCK_HEADER_RE = re.compile(r"^(?P<style>[|>])(?P<ind>[+-][1-9]?|[1-9][+-]?)?(?:[ \t]+#.*)?[ \t]*$")

# YAML 1.1 implicit-type patterns, as resolved by PyYAML's SafeLoader. A plain
# (unquoted) scalar matching one of these is NOT a string to a YAML parser.
_NULL_RE = re.compile(r"^(?:~|null|Null|NULL)$")
_BOOL_RE = re.compile(r"^(?:yes|Yes|YES|no|No|NO|true|True|TRUE|false|False|FALSE|on|On|ON|off|Off|OFF)$")
_INT_RE = re.compile(r"^(?:[-+]?0b[0-1_]+|[-+]?0[0-7_]+|[-+]?(?:0|[1-9][0-9_]*)|[-+]?0x[0-9a-fA-F_]+"
                     r"|[-+]?[1-9][0-9_]*(?::[0-5]?[0-9])+)$")
_FLOAT_RE = re.compile(r"^(?:[-+]?(?:[0-9][0-9_]*)\.[0-9_]*(?:[eE][-+][0-9]+)?|\.[0-9][0-9_]*(?:[eE][-+][0-9]+)?"
                       r"|[-+]?[0-9][0-9_]*(?::[0-5]?[0-9])+\.[0-9_]*|[-+]?\.(?:inf|Inf|INF)|\.(?:nan|NaN|NAN))$")
_DATE_RE = re.compile(r"^[0-9]{4}-[0-9]{2}-[0-9]{2}$")
_TIMESTAMP_RE = re.compile(r"^[0-9]{4}-[0-9]{1,2}-[0-9]{1,2}(?:[Tt]|[ \t]+)[0-9]{1,2}:[0-9]{2}:[0-9]{2}")
# YAML 1.2 core-schema numbers (YAML 1.2.2 spec, section 10.3.2) that YAML 1.1
# reads as strings: `0o17`, `089`, `1e3`, `1.0e3`, `+.5`. A YAML 1.2 parser
# resolves them to numbers, so they are diagnosed too: a value inside the
# subset must read the same under both versions. Deliberately a little wider
# than the spec (signs on 0o/0x, `_` digit separators, as ruamel.yaml accepts),
# because over-diagnosing only costs a pair of quotes.
_INT12_RE = re.compile(r"^(?:[-+]?[0-9][0-9_]*|[-+]?0o[0-7_]+|[-+]?0x[0-9a-fA-F_]+)$")
_FLOAT12_RE = re.compile(r"^[-+]?(?:\.[0-9_]+|[0-9][0-9_]*(?:\.[0-9_]*)?)(?:[eE][-+]?[0-9_]+)?$")

_DQ_ESCAPES = {
    "0": "\0", "a": "\x07", "b": "\x08", "t": "\t", "\t": "\t", "n": "\n", "v": "\x0b",
    "f": "\x0c", "r": "\r", "e": "\x1b", " ": " ", '"': '"', "/": "/", "\\": "\\",
    "N": "\x85", "_": "\xa0", "L": "\u2028", "P": "\u2029",
}
_DQ_HEX = {"x": 2, "u": 4, "U": 8}

# Characters that cannot start a plain scalar (YAML "indicators") and that
# this subset does not implement: anchors, aliases, tags, directives, reserved.
# `,` `]` `}` are flow indicators: YAML's ns-plain-first excludes them, so a
# YAML parser rejects the whole document (and Claude Code then loads the skill
# with no frontmatter at all) -- they must never be accepted silently.
# Characters YAML reads as line breaks (lone CR, NEL, LS, PS) or that PyYAML's
# reader rejects outright (C0/C1 controls other than tab/LF/CR/NEL, DEL,
# surrogates, U+FFFE/U+FFFF). The subset splits lines on LF only and strips only
# spaces/tabs, so with any of these present its reading can silently differ
# from YAML's (`name: x<NEL>` is 'x' to PyYAML) -- diagnose instead of guessing.
_YAML_BREAK_OR_NONPRINTABLE_RE = re.compile(
    r"[\r\x85\u2028\u2029\x00-\x08\x0b\x0c\x0e-\x1f\x7f\x80-\x84\x86-\x9f\ud800-\udfff\ufffe\uffff]")

_UNSUPPORTED_START = {"&": "anchor", "*": "alias", "!": "tag", "%": "directive", "@": "reserved indicator",
                      "`": "reserved indicator", "{": "flow mapping", "}": "flow indicator",
                      "]": "flow indicator", ",": "flow indicator"}


def _indent_of(line: str) -> int:
    return len(line) - len(line.lstrip(" "))


def _strip_comment(text: str) -> tuple[str, bool]:
    """Split a plain-scalar line at the first ` #` (YAML comment start).

    Only YAML whitespace (space, tab) is stripped. A bare `str.rstrip()` would
    also drop U+00A0 and other Unicode spaces that a YAML parser keeps in the
    value, so `name: x<NBSP>` would validate as 'x' while shipping as 'x\\xa0'.
    """
    if text.startswith("#"):
        return "", True
    m = re.search(r"[ \t]#", text)
    if m:
        return text[: m.start()].rstrip(" \t"), True
    return text.rstrip(" \t"), False


class _Parser:
    def __init__(self, text: str, line_offset: int):
        # CRLF is one YAML line break; any other CR is diagnosed in parse().
        text = text.replace("\r\n", "\n")
        self.lines = text.split("\n")
        # "a\nb\n".split("\n") has a trailing "" that is not a real line.
        self.final_newline = text.endswith("\n")
        if self.final_newline:
            self.lines.pop()
        self.n = len(self.lines)
        self.offset = line_offset
        self.diags: list[Diagnostic] = []
        self.key: str | None = None  # key currently being parsed (for diagnostics)
        self.block_lines: set[int] = set()  # lines consumed as block-scalar content

    # -- helpers -----------------------------------------------------------
    def diag(self, idx: int, code: str, message: str) -> None:
        self.diags.append(Diagnostic(idx + 1 + self.offset, code, message, self.key))

    def has_newline(self, idx: int) -> bool:
        return idx < self.n - 1 or self.final_newline

    def is_blank(self, idx: int) -> bool:
        return self.lines[idx].strip(" \t") == ""

    def is_comment(self, idx: int) -> bool:
        return self.lines[idx].lstrip(" ").startswith("#")

    def next_significant(self, idx: int) -> int:
        while idx < self.n and (self.is_blank(idx) or self.is_comment(idx)):
            idx += 1
        return idx

    # -- top level ---------------------------------------------------------
    def parse(self) -> ParseResult:
        data: dict[str, Any] = {}
        key_lines: dict[str, int] = {}
        i = 0
        while i < self.n:
            line = self.lines[i]
            self.key = None
            if self.is_blank(i) or self.is_comment(i):
                i += 1
                continue
            if line[0] == "\t" or "\t" in line[: _indent_of(line) + 1]:
                self.diag(i, "tab-indentation", "tab used for indentation (YAML forbids tabs here); use spaces")
                i += 1
                continue
            if line[0] == " ":
                self.diag(i, "unexpected-indentation",
                          "indented line does not belong to any key (wrong indentation or unsupported nesting)")
                i += 1
                continue
            m = _KEY_RE.match(line)
            if not m:
                self.diag(i, "unparseable-line", f"cannot parse line as `key: value`: {line.strip()[:60]!r}")
                i += 1
                continue
            key = m.group("key")
            self.key = key
            if any(r.match(key) for r in (_NULL_RE, _BOOL_RE, _INT_RE, _FLOAT_RE, _DATE_RE, _INT12_RE, _FLOAT12_RE)):
                self.diag(i, "implicit-type", f"key {key!r} is not read as a string by YAML; quote-free keys "
                                              "must be identifiers like `risk_level`")
            if key in data:
                self.diag(i, "duplicate-key", f"duplicate key {key!r} (first declared on line {key_lines[key]}); "
                                               "YAML keeps only the last value")
            key_lines[key] = i + 1 + self.offset
            value, i = self.value(m.group("rest") or "", i + 1, parent_indent=-1, start_idx=i)
            data[key] = value
        # Tabs: legal as block-scalar content, but PyYAML rejects them in many
        # other positions (after `:`, before a value, trailing). Rather than
        # mirror its exact rules, flag every tab outside block-scalar content.
        tab_flagged = {d.line for d in self.diags if d.code == "tab-indentation"}
        for idx, line in enumerate(self.lines):
            if "\t" in line and idx not in self.block_lines and idx + 1 + self.offset not in tab_flagged:
                self.diags.append(Diagnostic(idx + 1 + self.offset, "tab-character",
                                             "tab character outside a block scalar; use spaces (or `\\t` inside "
                                             "a double-quoted value)"))
        for idx, line in enumerate(self.lines):
            m = _YAML_BREAK_OR_NONPRINTABLE_RE.search(line)
            if m:
                self.diags.append(Diagnostic(idx + 1 + self.offset, "unsupported-syntax",
                                             f"character U+{ord(m.group()):04X} is a YAML line break or is not "
                                             "allowed in YAML; remove it (or use an escape inside a "
                                             "double-quoted value)"))
        self.diags.sort(key=lambda d: d.line)
        return ParseResult(data, self.diags, key_lines)

    # -- values --------------------------------------------------------------
    def value(self, rest: str, i: int, parent_indent: int, start_idx: int) -> tuple[Any, int]:
        """Parse the value whose first text is `rest` (on line start_idx); `i` is the next line."""
        rest = rest.rstrip(" \t")
        if rest == "" or rest.startswith("#"):
            j = self.next_significant(i)
            if j < self.n:
                m = _LIST_ITEM_RE.match(self.lines[j])
                # parent_indent is -1 for a top-level key, so a block sequence
                # may sit at column 0 (YAML allows `key:\n- a`); under a list
                # item it must be deeper than that item's dash.
                if m and len(m.group("indent")) > parent_indent:
                    return self.block_sequence(j)
            # Possibly a plain scalar that starts on the next line.
            text, i2 = self.plain_continuation("", i, parent_indent + 1)
            if text == "":
                return None, i
            return self.resolve_plain(text, start_idx), i2
        if rest[0] in "|>":
            return self.block_scalar(rest, i, parent_indent, start_idx)
        if rest[0] == '"':
            return self.quoted(rest, start_idx, double=True), i
        if rest[0] == "'":
            return self.quoted(rest, start_idx, double=False), i
        if rest[0] == "[":
            return self.flow_sequence(rest, start_idx), i
        if self.bad_plain_start(rest, start_idx):
            return rest, i
        first, had_comment = _strip_comment(rest)
        self.check_plain_piece(first, start_idx)
        if had_comment:
            return self.resolve_plain(first, start_idx), i
        text, i = self.plain_continuation(first, i, parent_indent + 1)
        return self.resolve_plain(text, start_idx), i

    def bad_plain_start(self, text: str, idx: int, extra: str = "") -> bool:
        """Diagnose text that cannot begin a YAML plain scalar; True if it was diagnosed."""
        c = text[:1]
        if c and c in _UNSUPPORTED_START:
            self.diag(idx, "unsupported-syntax",
                      f"YAML {_UNSUPPORTED_START[c]} ({c!r}) is outside the supported subset; quote the value")
            return True
        if c and c in "-?:" and (len(text) == 1 or text[1] in " \t"):
            self.diag(idx, "unsupported-syntax",
                      f"value starts with YAML indicator {c!r} (nested collection); quote the value")
            return True
        if c and c in extra:
            self.diag(idx, "unsupported-syntax",
                      f"a value starting with {c!r} must begin on the same line as its key")
            return True
        return False

    def check_plain_piece(self, piece: str, idx: int) -> None:
        if ": " in piece or ":\t" in piece or piece.endswith(":"):
            self.diag(idx, "mapping-in-scalar",
                      "unquoted value contains ': ' (or ends with ':'), which YAML reads as a nested mapping "
                      "or rejects; wrap the whole value in quotes")

    def plain_continuation(self, text: str, i: int, min_indent: int) -> tuple[str, int]:
        """Fold continuation lines of a multi-line plain scalar (YAML flow folding)."""
        min_indent = max(min_indent, 1)  # top-level keys sit at column 0; continuations must be deeper
        pieces = [text] if text != "" else []
        pending_blank = 0
        j = i
        while j < self.n:
            if self.is_blank(j):
                pending_blank += 1
                j += 1
                continue
            line = self.lines[j]
            if _indent_of(line) < min_indent or line.lstrip(" ").startswith("#") or "\t" in line[: _indent_of(line) + 1]:
                break
            if not pieces and _LIST_ITEM_RE.match(line):
                break
            if not pieces and self.bad_plain_start(line.strip(" "), j, extra="[]{},\"'|>"):
                i = j + 1
                break
            piece, had_comment = _strip_comment(line.strip(" "))
            self.check_plain_piece(piece, j)
            if pieces:
                pieces.append("\n" * pending_blank if pending_blank else " ")
            pieces.append(piece)
            pending_blank = 0
            j += 1
            i = j
            if had_comment:
                break
        return "".join(pieces), i

    def resolve_plain(self, text: str, idx: int) -> Any:
        if text == "" or _NULL_RE.match(text):
            return None
        kind = None
        if text == "=":  # PyYAML resolves a bare `=` to tag:yaml.org,2002:value, which SafeLoader rejects
            kind = "the YAML 1.1 'value' type"
        elif text == "<<":  # resolved to tag:yaml.org,2002:merge, which SafeLoader rejects as a value
            kind = "the YAML 1.1 merge key"
        elif _BOOL_RE.match(text):
            kind = "a boolean"
        elif _INT_RE.match(text):
            kind = "an integer"
        elif _FLOAT_RE.match(text):
            kind = "a float"
        elif _TIMESTAMP_RE.match(text):
            kind = "a timestamp"
        elif _INT12_RE.match(text):
            kind = "an integer under YAML 1.2 (YAML 1.1 reads it as a string)"
        elif _FLOAT12_RE.match(text):
            kind = "a float under YAML 1.2 (YAML 1.1 reads it as a string)"
        if kind:
            self.diag(idx, "implicit-type",
                      f"unquoted value {text!r} is read by YAML as {kind}, not a string; quote it")
        elif _DATE_RE.match(text) and not is_calendar_date(text):
            # PyYAML resolves it as a date, then raises ValueError and rejects the whole document.
            self.diag(idx, "implicit-type",
                      f"unquoted value {text!r} is read by YAML as a date but is not a real calendar date, "
                      "so a YAML parser rejects the whole document; fix the date or quote it")
        # Date-only scalars (2026-09-21) are deliberately kept as strings:
        # every consumer here wants the ISO text, and it is the documented format.
        return text

    def quoted(self, rest: str, idx: int, double: bool) -> str:
        q = '"' if double else "'"
        out: list[str] = []
        k = 1
        closed = False
        while k < len(rest):
            c = rest[k]
            if double and c == "\\":
                if k + 1 >= len(rest):
                    break
                e = rest[k + 1]
                if e in _DQ_ESCAPES:
                    out.append(_DQ_ESCAPES[e])
                    k += 2
                    continue
                if e in _DQ_HEX:
                    width = _DQ_HEX[e]
                    hexs = rest[k + 2:k + 2 + width]
                    if len(hexs) == width and all(h in "0123456789abcdefABCDEF" for h in hexs):
                        out.append(chr(int(hexs, 16)))
                        k += 2 + width
                        continue
                self.diag(idx, "invalid-escape", f"invalid escape sequence '\\{e}' in double-quoted value")
                out.append(e)
                k += 2
                continue
            if c == q:
                if not double and rest[k + 1:k + 2] == "'":
                    out.append("'")
                    k += 2
                    continue
                closed = True
                k += 1
                break
            out.append(c)
            k += 1
        if not closed:
            self.diag(idx, "unsupported-syntax",
                      "quoted value is not closed on the same line (multi-line quoted scalars are unsupported)")
            return "".join(out)
        tail = rest[k:]
        if tail.strip(" \t") and not re.match(r"^[ \t]+#", tail):
            self.diag(idx, "unparseable-line", f"unexpected text after closing quote: {tail.strip()[:40]!r}")
        return "".join(out)

    def flow_sequence(self, rest: str, idx: int) -> list[Any]:
        items: list[Any] = []
        k = 1
        expect_item = True
        while True:
            loop_start = k
            while k < len(rest) and rest[k] in " \t":
                k += 1
            if k >= len(rest):
                self.diag(idx, "unsupported-syntax",
                          "flow list `[...]` is not closed on the same line (multi-line flow lists are unsupported)")
                return items
            c = rest[k]
            if c == "]":
                k += 1
                break
            if c == ",":
                if expect_item:
                    self.diag(idx, "unparseable-line", "empty entry in flow list (`,,` or leading comma)")
                expect_item = True
                k += 1
                continue
            if c == "}":
                # No branch below consumes it (the plain-token regex stops at `}` and
                # matches nothing), so without this `k` never advanced: an infinite loop.
                self.diag(idx, "unsupported-syntax", "stray '}' inside a flow list; quote the value")
                k += 1
                continue
            if not expect_item:
                self.diag(idx, "unparseable-line", "missing comma between flow list entries")
            if c in "[{":
                self.diag(idx, "unsupported-syntax", "nested collections inside a flow list are unsupported")
                depth, start = 0, k
                while k < len(rest):
                    depth += rest[k] in "[{"
                    depth -= rest[k] in "]}"
                    k += 1
                    if depth == 0:
                        break
                items.append(rest[start:k])
            elif c in "\"'":
                sub = rest[k:]
                end = self.quoted_end(sub, double=(c == '"'))
                items.append(self.quoted(sub[:end], idx, double=(c == '"')))
                k += end
            else:
                m = re.match(r"[^,\[\]{}]*", rest[k:])
                token = m.group(0)
                k += len(token)
                token, had_comment = _strip_comment(token)
                if had_comment:
                    self.diag(idx, "unsupported-syntax", "comment inside a flow list")
                token = token.strip(" \t")  # YAML whitespace only (see _strip_comment)
                # Inside `[...]`, PyYAML reads a leading `?` or `:` as a mapping
                # indicator even without a following space (`[?x]` -> [{'x': None}]).
                self.bad_plain_start(token, idx, extra="|>?:")
                self.check_plain_piece(token, idx)
                items.append(self.resolve_plain(token, idx))
            expect_item = False
            if k == loop_start:  # defensive: a branch that consumes nothing would loop forever
                self.diag(idx, "unsupported-syntax", f"cannot parse flow list near {rest[k:k + 20]!r}")
                return items
        tail = rest[k:]
        if tail.strip(" \t") and not re.match(r"^[ \t]+#", tail):
            self.diag(idx, "unparseable-line", f"unexpected text after flow list: {tail.strip()[:40]!r}")
        return items

    @staticmethod
    def quoted_end(s: str, double: bool) -> int:
        """Index just past the closing quote of the quoted scalar starting at s[0]."""
        k = 1
        while k < len(s):
            if double and s[k] == "\\":
                k += 2
                continue
            if s[k] == s[0]:
                if not double and s[k + 1:k + 2] == "'":
                    k += 2
                    continue
                return k + 1
            k += 1
        return len(s)

    def block_sequence(self, j: int) -> tuple[list[Any], int]:
        dash_indent = len(_LIST_ITEM_RE.match(self.lines[j]).group("indent"))
        items: list[Any] = []
        i = j
        while True:
            j = self.next_significant(i)
            if j >= self.n:
                break
            m = _LIST_ITEM_RE.match(self.lines[j])
            if not m or len(m.group("indent")) != dash_indent:
                break
            value, i = self.value(m.group("rest") or "", j + 1, parent_indent=dash_indent, start_idx=j)
            if isinstance(value, list):
                self.diag(j, "unsupported-syntax", "nested list inside a list is unsupported")
            items.append(value)
        return items, i

    def block_scalar(self, rest: str, i: int, parent_indent: int, start_idx: int) -> tuple[str, int]:
        m = _BLOCK_HEADER_RE.match(rest)
        if not m:
            self.diag(start_idx, "unparseable-line",
                      f"invalid block scalar header {rest[:20]!r} (expected e.g. `>`, `|`, `>-`, `|+`)")
            return "", i
        folded = m.group("style") == ">"
        ind = m.group("ind") or ""
        chomp = "keep" if "+" in ind else "strip" if "-" in ind else "clip"
        digits = "".join(ch for ch in ind if ch.isdigit())
        min_indent = parent_indent + 1 if parent_indent >= 0 else 1
        if parent_indent < 0:
            parent_indent = 0
        if digits:
            indent = parent_indent + int(digits)
        else:
            # Auto-detect from the first non-blank line (PyYAML semantics).
            k, max_indent = i, 0
            while k < self.n and self.lines[k].strip(" ") == "":
                max_indent = max(max_indent, len(self.lines[k]))
                k += 1
            first = _indent_of(self.lines[k]) if k < self.n else 0
            indent = max(min_indent, first, max_indent)

        def is_empty(idx: int) -> bool:
            line = self.lines[idx]
            return line.strip(" ") == "" and len(line) <= indent

        def is_content(idx: int) -> bool:
            return self.lines[idx].startswith(" " * indent) and not is_empty(idx)

        chunks: list[str] = []
        breaks: list[str] = []
        line_break = ""
        while i < self.n and is_empty(i):
            breaks.append("\n")
            i += 1
        while i < self.n and is_content(i):
            chunks.extend(breaks)
            self.block_lines.add(i)
            text = self.lines[i][indent:]
            leading_non_space = text[:1] not in (" ", "\t")
            chunks.append(text)
            line_break = "\n" if self.has_newline(i) else ""
            i += 1
            breaks = []
            while i < self.n and is_empty(i):
                breaks.append("\n")
                i += 1
            if i < self.n and is_content(i):
                nxt = self.lines[i][indent:]
                if folded and line_break == "\n" and leading_non_space and nxt[:1] not in (" ", "\t"):
                    if not breaks:
                        chunks.append(" ")
                else:
                    chunks.append(line_break)
            else:
                break
        if chomp != "strip":
            chunks.append(line_break)
        if chomp == "keep":
            chunks.extend(breaks)
        return "".join(chunks), i


def parse_frontmatter_with_diagnostics(fm_text: str, line_offset: int = 0) -> ParseResult:
    """Parse the supported YAML subset, returning data plus diagnostics.

    `line_offset` is added to every reported line number (1 for SKILL.md,
    whose frontmatter starts after the opening `---` line).
    """
    return _Parser(fm_text, line_offset).parse()


def parse_frontmatter(fm_text: str) -> dict[str, Any]:
    """Parse the restricted YAML subset used by SKILL.md frontmatter / eval cases.

    Supported (see docs/SKILL-SPEC.md "Supported YAML subset"): top-level
    `key: value` mappings; plain, single- and double-quoted scalars (with
    YAML escape semantics); `>`/`|` block scalars with `-`/`+` chomping and
    indentation indicators; block lists (`- item`) and single-line flow lists
    (`[a, b]`, `[]`) of scalars; `#` comments. Anything else is reported via
    `parse_frontmatter_with_diagnostics` instead of being guessed at; this
    function returns the data only and never raises for malformed input.
    """
    return parse_frontmatter_with_diagnostics(fm_text).data


def _split_frontmatter(raw: str) -> tuple[str, str]:
    """Split on whole `---` lines (a `---` inside a value is not a fence)."""
    lines = raw.split("\n")
    if lines[0].rstrip() != "---":
        raise FrontmatterError("file does not start with a `---` frontmatter fence")
    for idx in range(1, len(lines)):
        if lines[idx].rstrip() == "---":
            fm = "".join(line + "\n" for line in lines[1:idx])
            return fm, "\n".join(lines[idx + 1:]).lstrip("\n")
    raise FrontmatterError("could not find closing `---` for frontmatter")


def load_skill(skill_dir: Path) -> Skill:
    skill_md = skill_dir / "SKILL.md"
    raw = skill_md.read_text(encoding="utf-8")
    fm_text, body = _split_frontmatter(raw)
    parsed = parse_frontmatter_with_diagnostics(fm_text, line_offset=1)
    return Skill(slug=skill_dir.name, path=skill_md, frontmatter=parsed.data, body=body, raw=raw,
                 diagnostics=parsed.diagnostics, key_lines=parsed.key_lines)


def iter_skill_dirs(root: Path = REPO_ROOT) -> list[Path]:
    dirs = []
    for p in sorted(root.iterdir()):
        if not p.is_dir():
            continue
        if p.name in NON_SKILL_DIRS or p.name.startswith("."):
            continue
        if (p / "SKILL.md").exists():
            dirs.append(p)
    return dirs


def load_all_skills(root: Path = REPO_ROOT) -> list[Skill]:
    skills = []
    for d in iter_skill_dirs(root):
        try:
            skills.append(load_skill(d))
        except FrontmatterError as e:
            # let callers (validate_skills.py) report this as a proper error
            # rather than crashing the whole run; re-raise with slug context.
            raise FrontmatterError(f"{d.name}: malformed frontmatter ({e})") from e
    return skills


def by_slug(skills: list[Skill]) -> dict[str, Skill]:
    return {s.slug: s for s in skills}


# --------------------------------------------------------------------------
# JSON-Schema subset validator (stdlib-only)
# --------------------------------------------------------------------------
# Enough of draft 2020-12 to enforce schema/skill-frontmatter.schema.json
# without a `pip install`. Any keyword outside this set raises instead of
# being ignored, so the generated schema can never quietly rely on a keyword
# this validator doesn't enforce. tests/test_schema.py cross-checks results
# against the real `jsonschema` package when it is installed.

SCHEMA_ANNOTATIONS = frozenset({"$schema", "$id", "$comment", "title", "description", "examples", "default"})
SCHEMA_KEYWORDS = frozenset({"type", "enum", "const", "pattern", "required", "properties", "additionalProperties",
                             "items", "maxLength", "minLength", "uniqueItems", "minItems", "maxItems", "format"})


@dataclass(frozen=True)
class SchemaError:
    path: tuple  # location in the instance, e.g. ("capabilities", 2)
    keyword: str  # failing keyword, e.g. "enum"
    message: str
    property: str | None = None  # for required/additionalProperties: the offending key


def _json_type_ok(value: Any, t: str) -> bool:
    if t == "string":
        return isinstance(value, str)
    if t == "array":
        return isinstance(value, list)
    if t == "object":
        return isinstance(value, dict)
    if t == "null":
        return value is None
    if t == "boolean":
        return isinstance(value, bool)
    if t == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if t == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    raise ValueError(f"unsupported JSON Schema type {t!r}")


@functools.lru_cache(maxsize=None)
def _ecma_pattern(pattern: str) -> re.Pattern:
    r"""Compile a JSON-Schema (ECMA-262) pattern with ECMA `$` semantics.

    In Python, `$` also matches just before a trailing newline, so
    re.search("^[a-z]+$", "tag\n") succeeds; in ECMA-262 (what JSON Schema
    specifies) it fails. Rewrite each unescaped `$` outside a character class
    to `\Z` so "tag\n" is rejected here as other JSON-Schema tools would.
    """
    out, k, in_class = [], 0, False
    while k < len(pattern):
        c = pattern[k]
        if c == "\\":
            out.append(pattern[k:k + 2])
            k += 2
            continue
        if in_class:
            in_class = c != "]"
        elif c == "[":
            in_class = True
        elif c == "$":
            c = r"\Z"
        out.append(c)
        k += 1
    return re.compile("".join(out))


def is_calendar_date(value: str) -> bool:
    """RFC 3339 full-date: YYYY-MM-DD that exists on the calendar."""
    import datetime as _dt

    if not _DATE_RE.match(value):
        return False
    try:
        _dt.date.fromisoformat(value)
    except ValueError:
        return False
    return True


def schema_errors(schema: dict, instance: Any, path: tuple = ()) -> list[SchemaError]:
    """Validate `instance` against `schema`; return every failure (empty == valid)."""
    unknown = set(schema) - SCHEMA_KEYWORDS - SCHEMA_ANNOTATIONS
    if unknown:
        raise ValueError(f"schema uses keyword(s) this validator does not implement: {sorted(unknown)}")
    errs: list[SchemaError] = []
    where = "/".join(str(p) for p in path) or "<root>"

    if "type" in schema:
        types = schema["type"] if isinstance(schema["type"], list) else [schema["type"]]
        if not any(_json_type_ok(instance, t) for t in types):
            errs.append(SchemaError(path, "type", f"{where}: expected {' or '.join(types)}, got {type(instance).__name__}"))
    if "enum" in schema and instance not in schema["enum"]:
        errs.append(SchemaError(path, "enum", f"{where}: {instance!r} is not one of {schema['enum']}"))
    if "const" in schema and instance != schema["const"]:
        errs.append(SchemaError(path, "const", f"{where}: expected {schema['const']!r}"))

    if isinstance(instance, str):
        if "minLength" in schema and len(instance) < schema["minLength"]:
            errs.append(SchemaError(path, "minLength", f"{where}: shorter than {schema['minLength']} character(s)"))
        if "maxLength" in schema and len(instance) > schema["maxLength"]:
            errs.append(SchemaError(path, "maxLength",
                                    f"{where}: {len(instance)} characters exceeds maxLength {schema['maxLength']}"))
        if "pattern" in schema and not _ecma_pattern(schema["pattern"]).search(instance):
            errs.append(SchemaError(path, "pattern", f"{where}: {instance!r} does not match {schema['pattern']}"))
        if schema.get("format") == "date" and not is_calendar_date(instance):
            errs.append(SchemaError(path, "format", f"{where}: {instance!r} is not a valid calendar date (YYYY-MM-DD)"))
        elif "format" in schema and schema["format"] != "date":
            raise ValueError(f"unsupported format {schema['format']!r}")

    if isinstance(instance, list):
        if "minItems" in schema and len(instance) < schema["minItems"]:
            errs.append(SchemaError(path, "minItems", f"{where}: fewer than {schema['minItems']} item(s)"))
        if "maxItems" in schema and len(instance) > schema["maxItems"]:
            errs.append(SchemaError(path, "maxItems", f"{where}: more than {schema['maxItems']} item(s)"))
        if schema.get("uniqueItems"):
            seen: list[Any] = []
            for item in instance:
                if item in seen:
                    errs.append(SchemaError(path, "uniqueItems", f"{where}: duplicate item {item!r}"))
                    break
                seen.append(item)
        if "items" in schema:
            for idx, item in enumerate(instance):
                errs.extend(schema_errors(schema["items"], item, path + (idx,)))

    if isinstance(instance, dict):
        for prop in schema.get("required", []):
            if prop not in instance:
                errs.append(SchemaError(path, "required", f"{where}: missing required property {prop!r}", prop))
        props = schema.get("properties", {})
        for key, val in instance.items():
            if key in props:
                errs.extend(schema_errors(props[key], val, path + (key,)))
            elif schema.get("additionalProperties") is False:
                errs.append(SchemaError(path, "additionalProperties", f"{where}: unexpected property {key!r}", key))
            elif isinstance(schema.get("additionalProperties"), dict):
                errs.extend(schema_errors(schema["additionalProperties"], val, path + (key,)))
    return errs
