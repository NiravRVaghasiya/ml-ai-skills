#!/usr/bin/env python3
"""Static security/hygiene audit of GitHub Actions workflow files (stdlib only).

Checks every ``.github/workflows/*.yml|*.yaml`` (or the files given on the
command line) and reports:

  ERROR  unpinned-action          ``uses:`` ref is not a 40-hex commit SHA (tags and
                                  branches are mutable; a moved tag runs new code)
  WARN   missing-version-comment  pinned ``uses:`` without a ``# vX.Y.Z`` comment
                                  (needed to know what to bump; see ci.yml header)
  ERROR  missing-permissions      no top-level ``permissions:``, or one with a null/empty
                                  value (the default token scope depends on repo
                                  settings, often write-all)
  ERROR  invalid-permissions      a scope name GitHub does not document (typo, or
                                  KNOWN_SCOPES below is stale), or a null job-level value
  ERROR  excessive-permissions    ``write-all`` anywhere; any ``write`` scope at the top
                                  level (grant writes per job); any job-level ``write``
                                  in a workflow that pull requests can trigger
  ERROR  pull-request-target      ``pull_request_target`` runs fork code with a
                                  privileged token and secrets
  WARN   privileged-trigger       ``workflow_run`` / ``issue_comment`` / ``issues`` /
                                  ``discussion*``: runs with secrets and a write-capable
                                  token on events anyone can cause; review by hand
  ERROR  untrusted-checkout       actions/checkout of an event-supplied ref under
                                  pull_request_target or a privileged trigger
  ERROR  script-injection         ``${{ github.event.* }}`` / ``${{ github.head_ref }}`` /
                                  ``${{ toJSON(github) }}`` expanded directly inside
                                  ``run:`` (or actions/github-script ``script:``), matched
                                  case-insensitively like GitHub does; also
                                  ``${{ env.X }}`` there when an ``env:`` entry in scope
                                  (workflow, job, step) carries such a value. Pass it via
                                  ``env:`` and reference ``"$VAR"`` instead
  WARN   ref-injection            ``${{ github.ref }}`` / ``github.ref_name`` / ``github.base_ref``
                                  (directly or via ``${{ env.X }}``) in the same places: git
                                  ref names may contain ``$ ( ) ` ;``, and whoever can push a
                                  branch or tag chooses them (WARN, not ERROR: pushers already
                                  have write access). Same fix: ``env:`` + ``"$VAR"``
  WARN   indirect-expression      ``${{ steps.*.outputs.* }}`` / ``${{ needs.*.outputs.* }}``
                                  inside ``run:``: the audit cannot tell whether the
                                  output carries attacker data; prefer ``env:`` + ``"$VAR"``
  ERROR  checkout-credentials     actions/checkout without ``persist-credentials: false``
                                  (the token otherwise stays in .git/config for
                                  every later step)
  ERROR  missing-timeout          job without ``timeout-minutes`` (default is 360)
  ERROR  secrets-in-pr-workflow   ``${{ secrets.* }}`` (other than GITHUB_TOKEN), the whole
                                  ``secrets`` context, or ``secrets: inherit`` in a
                                  workflow that ``pull_request*`` triggers
  ERROR  parse-error / unsupported-yaml  the file is outside the YAML subset this
                                  parser understands - the audit fails closed rather
                                  than vouching for a file it could not read

The parser handles the block-style YAML subset workflows use (mappings,
sequences, ``|``/``>`` block scalars, quoted scalars, single-line flow
collections, comments). Anchors, aliases, tags and multi-document files are
rejected as ``unsupported-yaml``. As a cross-check, the number of ``uses:``
keys found structurally must equal a line-regex count of the raw text.

Usage:
    python scripts/audit_workflows.py                 # all workflows, errors fail
    python scripts/audit_workflows.py --strict        # warnings also fail
    python scripts/audit_workflows.py --json FILE...  # machine-readable

Limits (static analysis, not a proof of safety): expressions are matched textually,
so data laundered through a file, ``$GITHUB_ENV``/``$GITHUB_OUTPUT`` or a composite
action's inputs is not traced; step/job outputs are only warned about.

Exit codes: 0 clean; 1 findings at the failing level; 2 usage error / no files.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import asdict, dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
WORKFLOW_DIR = REPO_ROOT / ".github" / "workflows"

SHA_RE = re.compile(r"^[0-9a-f]{40}$")
DOCKER_DIGEST_RE = re.compile(r"@sha256:[0-9a-f]{64}$")
# ${{ ... }} expressions; attacker-controllable contexts inside them.
EXPR_RE = re.compile(r"\$\{\{(.*?)\}\}", re.S)
# Expressions are normalised first (_normalize_expr): `x['name']` / `x["name"]` becomes
# `x.name`, then remaining string literals are blanked, so `'github-actions[bot]'` is
# not a context reference. GitHub resolves context and property names case-insensitively
# (`${{ GitHub.Event.issue.title }}` is still event data), hence re.I throughout.
# `(?<![\w.-])` keeps e.g. `steps.github.outputs` or `inputs.env` from matching.
_CTX = r"(?<![\w.-])"
# Tainted: github.event.* / github.head_ref, and the whole `github` context (or a
# computed index into it): toJSON(github) includes the event payload.
TAINTED_RE = re.compile(_CTX + r"github\b\s*(?:\.\s*(?:event|head_ref)\b|(?!\.))", re.I)
# Branch/tag names chosen by whoever pushes the ref (github.ref_type is not: branch|tag).
REF_NAME_RE = re.compile(_CTX + r"github\b\s*\.\s*(?:ref_name|base_ref|ref)\b", re.I)
EVENT_OR_REF_RE = re.compile(f"(?:{TAINTED_RE.pattern})|(?:{REF_NAME_RE.pattern})", re.I)
SECRET_RE = re.compile(_CTX + r"secrets\b(?:\s*\.\s*([A-Za-z_][A-Za-z0-9_-]*))?", re.I)
ENV_REF_RE = re.compile(_CTX + r"env\b(?:\s*\.\s*([A-Za-z_][A-Za-z0-9_-]*))?", re.I)
OUTPUTS_RE = re.compile(_CTX + r"(?:steps|needs)\s*\.\s*[A-Za-z0-9_-]+\s*\.\s*outputs\b", re.I)
_BRACKET_LITERAL_RE = re.compile(r"""\[\s*(?:'([^']*)'|"([^"]*)")\s*\]""")
_STRING_LITERAL_RE = re.compile(r"'(?:[^']|'')*'")
RAW_USES_RE = re.compile(r"^\s*(?:-\s+)?uses\s*:", re.M)
PR_EVENTS = {"pull_request", "pull_request_target", "pull_request_review", "pull_request_review_comment"}
# Run in the base repository with secrets and a write-capable token, on events that
# anyone who can open an issue/comment (or a fork PR whose workflow finished) can cause.
PRIVILEGED_EVENTS = {"workflow_run", "issue_comment", "issues", "discussion", "discussion_comment"}
# GITHUB_TOKEN scopes listed at https://docs.github.com/en/actions/reference/workflows-and-actions/
# workflow-syntax#permissions (fetched 2026-10-28), plus `models` and `repository-projects`,
# which earlier revisions of GitHub's docs listed (not re-verified against the live service);
# kept so an older workflow is not a false ERROR. Add new scopes here when GitHub documents them.
KNOWN_SCOPES = {
    "actions", "artifact-metadata", "attestations", "checks", "code-quality", "contents",
    "copilot-requests", "deployments", "discussions", "id-token", "issues", "packages", "pages",
    "pull-requests", "security-events", "statuses", "vulnerability-alerts",
    "models", "repository-projects",
}


class YamlError(Exception):
    def __init__(self, line: int, message: str, code: str = "parse-error"):
        super().__init__(f"line {line}: {message}")
        self.line = line
        self.code = code


class YMap(dict):
    """dict that also remembers the 1-based line of each key."""

    def __init__(self):
        super().__init__()
        self.lines: dict[str, int] = {}


class YSeq(list):
    """list that also remembers the 1-based line of each item."""

    def __init__(self):
        super().__init__()
        self.lines: list[int] = []


# --------------------------------------------------------------------------- parser

def _strip_comment(text: str) -> str:
    """Drop a trailing ``# comment`` that is outside quotes (YAML: '#' preceded by space)."""
    quote = None
    i = 0
    while i < len(text):
        c = text[i]
        if quote:
            if c == quote:
                if quote == "'" and i + 1 < len(text) and text[i + 1] == "'":
                    i += 2
                    continue
                quote = None
            elif c == "\\" and quote == '"':
                i += 2
                continue
        elif c in "'\"" and (i == 0 or text[i - 1] in " \t[{,:"):
            quote = c
        elif c == "#" and (i == 0 or text[i - 1] in " \t"):
            return text[:i].rstrip()
        i += 1
    return text.rstrip()


def _find_mapping_colon(text: str) -> int:
    """Index of the key/value ':' (followed by space or end), outside quotes; -1 if none."""
    quote = None
    depth = 0
    for i, c in enumerate(text):
        if quote:
            if c == quote:
                quote = None
            continue
        if c in "'\"" and (i == 0 or text[i - 1] in " \t[{,"):
            quote = c
        elif c in "[{":
            depth += 1
        elif c in "]}":
            depth -= 1
        elif c == ":" and depth == 0 and (i + 1 == len(text) or text[i + 1] in " \t"):
            return i
    return -1


def _unquote(s: str, line: int) -> str:
    s = s.strip()
    if len(s) >= 2 and s[0] == s[-1] == "'":
        return s[1:-1].replace("''", "'")
    if len(s) >= 2 and s[0] == s[-1] == '"':
        try:
            return json.loads(s)
        except json.JSONDecodeError:
            return s[1:-1]
    if s[:1] in ("&", "*", "!"):
        raise YamlError(line, f"anchors, aliases and tags are not supported: {s!r}", "unsupported-yaml")
    if s[:1] in ("'", '"'):
        raise YamlError(line, f"unterminated quoted scalar: {s!r}")
    return s


def _parse_flow(text: str, line: int):
    """Parse a single-line flow collection ``[a, 'b']`` / ``{k: v}``."""
    pos = 0

    def skip_ws():
        nonlocal pos
        while pos < len(text) and text[pos] in " \t":
            pos += 1

    def scalar(stop: str) -> str:
        nonlocal pos
        skip_ws()
        start = pos
        if pos < len(text) and text[pos] in "'\"":
            q = text[pos]
            pos += 1
            while pos < len(text):
                if text[pos] == q:
                    if q == "'" and pos + 1 < len(text) and text[pos + 1] == "'":
                        pos += 2
                        continue
                    break
                pos += 2 if (text[pos] == "\\" and q == '"') else 1
            pos += 1
            return _unquote(text[start:pos], line)
        while pos < len(text) and text[pos] not in stop:
            pos += 1
        return _unquote(text[start:pos], line)

    def value():
        nonlocal pos
        skip_ws()
        if pos >= len(text):
            raise YamlError(line, "unterminated flow collection")
        if text[pos] == "[":
            pos += 1
            out = YSeq()
            skip_ws()
            if pos < len(text) and text[pos] == "]":
                pos += 1
                return out
            while True:
                out.append(value())
                out.lines.append(line)
                skip_ws()
                if pos < len(text) and text[pos] == ",":
                    pos += 1
                    continue
                if pos < len(text) and text[pos] == "]":
                    pos += 1
                    return out
                raise YamlError(line, f"bad flow sequence: {text!r}")
        if text[pos] == "{":
            pos += 1
            out = YMap()
            skip_ws()
            if pos < len(text) and text[pos] == "}":
                pos += 1
                return out
            while True:
                key = scalar(":,}")
                skip_ws()
                if pos < len(text) and text[pos] == ":":
                    pos += 1
                    out[key] = value()
                else:
                    out[key] = None
                out.lines[key] = line
                skip_ws()
                if pos < len(text) and text[pos] == ",":
                    pos += 1
                    continue
                if pos < len(text) and text[pos] == "}":
                    pos += 1
                    return out
                raise YamlError(line, f"bad flow mapping: {text!r}")
        return scalar(",]}")

    result = value()
    skip_ws()
    if pos != len(text):
        raise YamlError(line, f"trailing characters after flow collection: {text!r}")
    return result


class _Parser:
    def __init__(self, text: str):
        if "\t" in "".join(ln[: len(ln) - len(ln.lstrip(" \t"))] for ln in text.splitlines()):
            raise YamlError(1, "tab characters in indentation are not valid YAML")
        self.raw = text.splitlines()
        # [indent, content, lineno]; content keeps comments (block scalars need them raw).
        self.lines = [[len(r) - len(r.lstrip(" ")), r.lstrip(" "), i + 1] for i, r in enumerate(self.raw)]
        self.i = 0
        for entry in self.lines:  # one optional leading "---" document marker is fine
            content = _strip_comment(entry[1])
            if content:
                if content == "---" and entry[0] == 0:
                    entry[1] = ""
                break

    def _significant(self, i: int) -> int:
        """Index of the next line with content (not blank, not comment-only)."""
        while i < len(self.lines):
            content = _strip_comment(self.lines[i][1])
            if content:
                if self.lines[i][0] == 0 and (content in ("---", "...") or content.startswith(("--- ", "... "))):
                    raise YamlError(self.lines[i][2], "multi-document YAML is not supported", "unsupported-yaml")
                return i
            i += 1
        return i

    def parse(self):
        self.i = self._significant(0)
        if self.i >= len(self.lines):
            return None
        indent = self.lines[self.i][0]
        if indent != 0:
            raise YamlError(self.lines[self.i][2], "document must start at column 0")
        value = self._block(indent)
        self.i = self._significant(self.i)
        if self.i < len(self.lines):
            raise YamlError(self.lines[self.i][2], "unexpected content (bad indentation?)")
        return value

    def _block(self, indent: int):
        content = _strip_comment(self.lines[self.i][1])
        if content == "-" or content.startswith("- "):
            return self._sequence(indent)
        return self._mapping(indent)

    def _sequence(self, indent: int) -> YSeq:
        out = YSeq()
        while True:
            self.i = self._significant(self.i)
            if self.i >= len(self.lines):
                return out
            ind, content_raw, lineno = self.lines[self.i]
            content = _strip_comment(content_raw)
            if ind < indent:
                return out
            if ind > indent:
                raise YamlError(lineno, "bad indentation inside sequence")
            if not (content == "-" or content.startswith("- ")):
                return out
            rest_raw = content_raw[1:]
            rest = rest_raw.lstrip(" ")
            out.lines.append(lineno)
            if not _strip_comment(rest):
                self.i += 1
                nxt = self._significant(self.i)
                if nxt < len(self.lines) and self.lines[nxt][0] > indent:
                    self.i = nxt
                    out.append(self._block(self.lines[nxt][0]))
                else:
                    out.append(None)
                continue
            # "- key: value" or "- - x" or "- scalar": re-read the rest as a nested node
            # whose indentation is the column right after "- ".
            child_indent = ind + 1 + (len(rest_raw) - len(rest))
            stripped = _strip_comment(rest)
            if stripped == "-" or stripped.startswith("- ") or _find_mapping_colon(stripped) != -1:
                self.lines[self.i] = [child_indent, rest, lineno]
                out.append(self._block(child_indent))
            else:
                out.append(self._scalar_value(stripped, indent, lineno))
                self.i += 1

    def _mapping(self, indent: int) -> YMap:
        out = YMap()
        while True:
            self.i = self._significant(self.i)
            if self.i >= len(self.lines):
                return out
            ind, content_raw, lineno = self.lines[self.i]
            content = _strip_comment(content_raw)
            if ind < indent:
                return out
            if ind > indent:
                raise YamlError(lineno, "bad indentation inside mapping")
            if content == "-" or content.startswith("- "):
                return out  # a sequence at the parent's indent ends this mapping
            colon = _find_mapping_colon(content)
            if colon == -1:
                raise YamlError(lineno, f"expected 'key: value', got {content!r}")
            key = _unquote(content[:colon], lineno)
            if key in out:
                raise YamlError(lineno, f"duplicate key {key!r}")
            value_text = content[colon + 1:].strip()
            out.lines[key] = lineno
            if value_text[:1] in ("|", ">"):
                if not re.fullmatch(r"[|>][+-]?[1-9]?|[|>][1-9]?[+-]?", value_text):
                    raise YamlError(lineno, f"unsupported block scalar header {value_text!r}", "unsupported-yaml")
                out[key] = self._block_scalar(indent, value_text[0] == ">")
                continue
            self.i += 1
            if value_text == "":
                nxt = self._significant(self.i)
                if nxt < len(self.lines):
                    n_ind, n_content, _ = self.lines[nxt]
                    n_content = _strip_comment(n_content)
                    if n_ind > indent or (n_ind == indent and (n_content == "-" or n_content.startswith("- "))):
                        self.i = nxt
                        out[key] = self._block(n_ind)
                        continue
                out[key] = None
            else:
                out[key] = self._scalar_value(value_text, indent, lineno)

    def _scalar_value(self, text: str, indent: int, lineno: int):
        if text[:1] in ("[", "{"):
            return _parse_flow(text, lineno)
        if text[:1] in ("'", '"') and not (len(text) >= 2 and text[-1] == text[0]):
            raise YamlError(lineno, "multi-line quoted scalars are not supported", "unsupported-yaml")
        return _unquote(text, lineno)

    def _block_scalar(self, indent: int, folded: bool) -> str:
        self.i += 1
        body: list[str] = []
        block_indent = None
        while self.i < len(self.lines):
            raw = self.raw[self.i]
            if raw.strip() == "":
                body.append("")
                self.i += 1
                continue
            ind = len(raw) - len(raw.lstrip(" "))
            if ind <= indent:
                break
            if block_indent is None:
                block_indent = ind
            if ind < block_indent:
                raise YamlError(self.i + 1, "block scalar line is less indented than its first line")
            body.append(raw[block_indent:])
            self.i += 1
        while body and body[-1] == "":
            body.pop()
        return (" " if folded else "\n").join(body) + "\n"


def parse_yaml(text: str):
    return _Parser(text).parse()


# --------------------------------------------------------------------------- audit

@dataclass
class Finding:
    file: str
    line: int
    level: str  # ERROR | WARN
    code: str
    message: str


def _triggers(on) -> set[str]:
    if on is None:
        return set()
    if isinstance(on, str):
        return {on}
    if isinstance(on, list):
        return {str(x) for x in on}
    if isinstance(on, dict):
        return set(on)
    return set()


def _without_key(m: YMap, exclude: str) -> YMap:
    """Shallow copy of ``m`` without key ``exclude`` (line info kept)."""
    out = YMap()
    for k, v in m.items():
        if k != exclude:
            out[k] = v
            out.lines[k] = m.lines.get(k, 1)
    return out


def _walk_scalars(node, line: int):
    """Yield (string, line) for every scalar (and mapping key) under node."""
    if isinstance(node, dict):
        lines = getattr(node, "lines", {})
        for k, v in node.items():
            kl = lines.get(k, line)
            yield str(k), kl
            yield from _walk_scalars(v, kl)
    elif isinstance(node, list):
        lines = getattr(node, "lines", [])
        for idx, v in enumerate(node):
            yield from _walk_scalars(v, lines[idx] if idx < len(lines) else line)
    elif node is not None:
        yield str(node), line


def _check_permissions(perms, where: str, line: int, top_level: bool, pr_triggered: bool, add) -> None:
    if perms is None or (isinstance(perms, str) and not perms.strip()):
        # `permissions:` with no value is null, not "no permissions" ({} is that); a null
        # top-level value leaves the repository default token scope in force.
        if top_level:
            add(line, "ERROR", "missing-permissions",
                "top-level 'permissions:' is null/empty, so the repository default applies "
                "(set 'permissions: contents: read', or '{}' for none)")
        else:
            add(line, "ERROR", "invalid-permissions", f"{where}: 'permissions:' is null/empty")
        return
    if isinstance(perms, str):
        if perms.strip() == "write-all":
            add(line, "ERROR", "excessive-permissions", f"{where}: 'permissions: write-all' grants every scope")
        elif perms.strip() != "read-all":
            add(line, "ERROR", "excessive-permissions", f"{where}: unrecognised permissions value {perms!r}")
        return
    if not isinstance(perms, dict):
        add(line, "ERROR", "excessive-permissions", f"{where}: permissions must be a mapping or read-all")
        return
    for scope, level in perms.items():
        sline = perms.lines.get(scope, line) if isinstance(perms, YMap) else line
        level = str(level).strip()
        if scope not in KNOWN_SCOPES:
            add(sline, "ERROR", "invalid-permissions",
                f"{where}: unknown permission scope {scope!r} (typo? GitHub rejects unknown scopes; "
                "if GitHub has added it, add it to KNOWN_SCOPES in scripts/audit_workflows.py)")
        if level not in ("read", "write", "none"):
            add(sline, "ERROR", "excessive-permissions", f"{where}: {scope}: unknown level {level!r}")
        elif level == "write" and top_level:
            add(sline, "ERROR", "excessive-permissions",
                f"top-level '{scope}: write' applies to every job; grant writes only in the job that needs them")
        elif level == "write" and pr_triggered:
            add(sline, "ERROR", "excessive-permissions",
                f"{where}: '{scope}: write' in a workflow that pull requests can trigger")


def _check_uses(value: str, line: int, raw_line: str, add, where: str) -> None:
    value = value.strip()
    if value.startswith("./"):
        return  # local action/workflow, versioned with the repo
    if value.startswith("docker://"):
        if not DOCKER_DIGEST_RE.search(value):
            add(line, "ERROR", "unpinned-action", f"{where}: docker action {value!r} is not pinned to a sha256 digest")
        return
    if "@" not in value:
        add(line, "ERROR", "unpinned-action", f"{where}: {value!r} has no @ref")
        return
    ref = value.rsplit("@", 1)[1]
    if not SHA_RE.match(ref):
        add(line, "ERROR", "unpinned-action",
            f"{where}: {value!r} is pinned to a mutable ref; pin the full 40-hex commit SHA (keep '# vX.Y.Z' after it)")
        return
    if not re.search(r"#\s*\S", raw_line.split(ref, 1)[-1]):
        add(line, "WARN", "missing-version-comment", f"{where}: pinned {value!r} has no '# vX.Y.Z' comment")


def _normalize_expr(expr: str) -> str:
    """``a['b']`` -> ``a.b``; other string literals blanked (see the regex comment above)."""
    expr = _BRACKET_LITERAL_RE.sub(lambda m: "." + (m.group(1) if m.group(1) is not None else m.group(2)), expr)
    return _STRING_LITERAL_RE.sub("''", expr)


def _expressions(text: str):
    """Yield (raw, normalised) for every ``${{ ... }}`` in text."""
    for m in EXPR_RE.finditer(text):
        yield m.group(1).strip(), _normalize_expr(m.group(1))


def _is_tainted(text: str, tainted_env: set[str], pattern: re.Pattern = TAINTED_RE) -> bool:
    """Does text contain an expression that yields attacker-controllable data?"""
    for _raw, expr in _expressions(text):
        if pattern.search(expr):
            return True
        for m in ENV_REF_RE.finditer(expr):
            if tainted_env and (m.group(1) is None or m.group(1).upper() in tainted_env):
                return True
    return False


def _tainted_env(env, inherited: set[str], pattern: re.Pattern = TAINTED_RE) -> set[str]:
    """Upper-cased names of env: entries whose value carries tainted data (env lookups in
    expressions are case-insensitive), including entries that copy an inherited tainted one."""
    out = set(inherited)
    if isinstance(env, dict):
        for k, v in env.items():
            if _is_tainted(str(v) if v is not None else "", inherited, pattern):
                out.add(str(k).upper())
    return out


def _env_hits(expr: str, names: set[str]) -> list[str]:
    return [m.group(1) or "<whole env context>" for m in ENV_REF_RE.finditer(expr)
            if names and (m.group(1) is None or m.group(1).upper() in names)]


def _check_script(text: str, line: int, add, where: str, what: str, tainted_env: set[str],
                  ref_env: set[str] = frozenset()) -> None:
    """`tainted_env`: env names carrying event data; `ref_env`: names carrying event data or ref names."""
    for raw, expr in _expressions(text):
        if TAINTED_RE.search(expr):
            add(line, "ERROR", "script-injection",
                f"{where}: {what} interpolates ${{{{ {raw} }}}} directly; pass it through env: "
                "and use \"$VAR\" (the value is attacker-controlled and is spliced into the script text)")
            continue
        hits = _env_hits(expr, tainted_env)
        if hits:
            add(line, "ERROR", "script-injection",
                f"{where}: {what} interpolates ${{{{ {raw} }}}}, and env {', '.join(hits)} carries event data; "
                "the expression is spliced into the script text before the shell runs, so env: does not "
                "help here - reference \"$VAR\" in the shell instead")
            continue
        hits = ["(direct)"] if REF_NAME_RE.search(expr) else _env_hits(expr, ref_env)
        if hits:
            via = "" if hits == ["(direct)"] else f" (env {', '.join(hits)} carries a ref name)"
            add(line, "WARN", "ref-injection",
                f"{where}: {what} interpolates ${{{{ {raw} }}}}{via}; branch and tag names may contain shell "
                "metacharacters and are chosen by whoever pushes the ref - pass it through env: and use \"$VAR\"")
            continue
        if OUTPUTS_RE.search(expr):
            add(line, "WARN", "indirect-expression",
                f"{where}: {what} interpolates ${{{{ {raw} }}}}; the audit cannot tell whether that output "
                "carries attacker data - pass it through env: and use \"$VAR\"")


def audit_text(text: str, name: str) -> list[Finding]:
    findings: list[Finding] = []

    def add(line: int, level: str, code: str, message: str) -> None:
        findings.append(Finding(name, line, level, code, message))

    try:
        doc = parse_yaml(text)
    except YamlError as e:
        add(e.line, "ERROR", e.code, str(e))
        return findings
    if not isinstance(doc, dict):
        add(1, "ERROR", "parse-error", "workflow is not a mapping")
        return findings
    raw_lines = text.splitlines()

    # PyYAML (YAML 1.1) reads a bare `on:` key as boolean True; we keep the string.
    triggers = _triggers(doc.get("on", doc.get("true")))
    on_line = doc.lines.get("on", 1)
    if not triggers:
        add(on_line, "ERROR", "parse-error", "no 'on:' triggers found")
    pr_triggered = bool(triggers & PR_EVENTS)
    if "pull_request_target" in triggers:
        add(on_line, "ERROR", "pull-request-target",
            "'pull_request_target' runs with a privileged token and secrets in the context of fork PRs; "
            "use 'pull_request'")
    privileged = sorted(triggers & PRIVILEGED_EVENTS)
    if privileged:
        add(on_line, "WARN", "privileged-trigger",
            f"{', '.join(privileged)} run(s) in the base repository with secrets and a write-capable token "
            "on events outsiders can cause; never check out or execute their code, keep permissions minimal")
    untrusted_checkout = "pull_request_target" in triggers or bool(privileged)

    if "permissions" not in doc:
        add(1, "ERROR", "missing-permissions", "no top-level 'permissions:' (set 'permissions: contents: read')")
    else:
        _check_permissions(doc["permissions"], "top-level", doc.lines["permissions"], True, pr_triggered, add)

    def check_secrets(node, line: int, where: str) -> None:
        for s, sl_ in _walk_scalars(node, line):
            for m in EXPR_RE.finditer(s):
                for sm in SECRET_RE.finditer(_normalize_expr(m.group(1))):
                    name = sm.group(1)
                    if name is None:
                        add(sl_, "ERROR", "secrets-in-pr-workflow",
                            f"{where} references the whole secrets context in a workflow pull requests trigger")
                    elif name.upper() != "GITHUB_TOKEN":
                        add(sl_, "ERROR", "secrets-in-pr-workflow",
                            f"{where} references secrets.{name} in a workflow pull requests trigger")

    if pr_triggered:  # top-level env:/defaults: etc. reach every job
        check_secrets(_without_key(doc, "jobs"), 1, "top level")

    jobs = doc.get("jobs")
    if not isinstance(jobs, dict) or not jobs:
        add(doc.lines.get("jobs", 1), "ERROR", "parse-error", "no 'jobs:' mapping")
        return findings

    workflow_tainted_env = _tainted_env(doc.get("env"), set())
    workflow_ref_env = _tainted_env(doc.get("env"), set(), EVENT_OR_REF_RE)
    uses_found = 0
    for job_id, job in jobs.items():
        jline = jobs.lines.get(job_id, 1)
        where = f"job '{job_id}'"
        if not isinstance(job, dict):
            add(jline, "ERROR", "parse-error", f"{where} is not a mapping")
            continue
        if "permissions" in job:
            _check_permissions(job["permissions"], where, job.lines["permissions"], False, pr_triggered, add)
        if "uses" in job:  # reusable workflow call: timeout-minutes is not allowed there
            uses_found += 1
            ul = job.lines["uses"]
            _check_uses(str(job["uses"]), ul, raw_lines[ul - 1], add, where)
        elif "timeout-minutes" not in job:
            add(jline, "ERROR", "missing-timeout", f"{where} has no timeout-minutes (GitHub's default is 360)")

        if pr_triggered:
            check_secrets(job, jline, where)
            if str(job.get("secrets", "")).strip() == "inherit":
                add(job.lines["secrets"], "ERROR", "secrets-in-pr-workflow",
                    f"{where} passes 'secrets: inherit' in a workflow pull requests trigger")

        job_tainted_env = _tainted_env(job.get("env"), workflow_tainted_env)
        job_ref_env = _tainted_env(job.get("env"), workflow_ref_env, EVENT_OR_REF_RE)
        steps = job.get("steps") or []
        if not isinstance(steps, list):
            add(job.lines.get("steps", jline), "ERROR", "parse-error", f"{where}: steps is not a list")
            continue
        for idx, step in enumerate(steps):
            sline = steps.lines[idx] if isinstance(steps, YSeq) and idx < len(steps.lines) else jline
            if not isinstance(step, dict):
                add(sline, "ERROR", "parse-error", f"{where} step {idx + 1} is not a mapping")
                continue
            swhere = f"{where} step {idx + 1}" + (f" ({step['name']})" if step.get("name") else "")
            step_tainted_env = _tainted_env(step.get("env"), job_tainted_env)
            step_ref_env = _tainted_env(step.get("env"), job_ref_env, EVENT_OR_REF_RE)
            if "run" in step:
                _check_script(str(step["run"]), step.lines["run"], add, swhere, "run:", step_tainted_env,
                              step_ref_env)
            uses = step.get("uses")
            if uses is not None:
                uses_found += 1
                ul = step.lines["uses"]
                _check_uses(str(uses), ul, raw_lines[ul - 1], add, swhere)
                with_ = step.get("with") if isinstance(step.get("with"), dict) else {}
                action = str(uses).split("@", 1)[0].strip().lower()
                if action == "actions/checkout":
                    pc = str(with_.get("persist-credentials", "")).strip().lower()
                    if pc != "false":
                        add(ul, "ERROR", "checkout-credentials",
                            f"{swhere}: actions/checkout without 'persist-credentials: false'")
                    for key in ("ref", "repository"):
                        if untrusted_checkout and _is_tainted(str(with_.get(key) or ""), step_tainted_env):
                            add(with_.lines.get(key, ul), "ERROR", "untrusted-checkout",
                                f"{swhere}: checks out an event-supplied {key} in a privileged workflow "
                                "(fork code would run with secrets and a write-capable token)")
                if action == "actions/github-script" and "script" in with_:
                    _check_script(str(with_["script"]), with_.lines["script"], add, swhere, "github-script script:",
                                  step_tainted_env, step_ref_env)

    raw_uses = len(RAW_USES_RE.findall(text))
    if raw_uses != uses_found:
        add(1, "ERROR", "parse-error",
            f"structural parse found {uses_found} 'uses:' keys but the raw text has {raw_uses}; "
            "refusing to vouch for a file the parser may have misread")
    findings.sort(key=lambda f: (f.line, f.code))
    return findings


def audit_file(path: Path) -> list[Finding]:
    try:
        rel = path.resolve().relative_to(REPO_ROOT).as_posix()
    except ValueError:
        rel = str(path)
    try:
        text = path.read_bytes().decode("utf-8")
    except (OSError, UnicodeDecodeError) as e:
        return [Finding(rel, 1, "ERROR", "parse-error", f"cannot read: {e}")]
    return audit_text(text, rel)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("files", nargs="*", type=Path, help="workflow files (default: .github/workflows/*.y*ml)")
    ap.add_argument("--strict", action="store_true", help="warnings also fail")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)

    files = args.files or sorted([*WORKFLOW_DIR.glob("*.yml"), *WORKFLOW_DIR.glob("*.yaml")])
    if not files:
        print("audit_workflows: no workflow files found", file=sys.stderr)
        return 2
    findings: list[Finding] = []
    for f in files:
        findings.extend(audit_file(f))
    failing = [f for f in findings if f.level == "ERROR" or (args.strict and f.level == "WARN")]
    if args.json:
        print(json.dumps({"files": [str(f) for f in files], "findings": [asdict(f) for f in findings],
                          "failed": bool(failing)}, indent=2))
    else:
        for f in findings:
            print(f"{f.file}:{f.line}: {f.level} {f.code}: {f.message}")
        n_err = sum(f.level == "ERROR" for f in findings)
        n_warn = sum(f.level == "WARN" for f in findings)
        print(f"audit_workflows: {len(files)} file(s), {n_err} error(s), {n_warn} warning(s)")
    return 1 if failing else 0


if __name__ == "__main__":
    sys.exit(main())
