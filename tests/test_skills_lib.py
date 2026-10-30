import datetime
import json
import random
import subprocess
import sys
import unittest

from testutil import REPO_ROOT, sl

try:  # optional: only for the YAML-compatibility tests
    import yaml
except ImportError:  # pragma: no cover - exercised on bare installs
    yaml = None

try:  # optional YAML 1.2 reference; not in requirements-dev.txt, so CI skips it
    from ruamel.yaml import YAML as _RuamelYAML
except ImportError:  # pragma: no cover - exercised wherever ruamel.yaml is absent
    _RuamelYAML = None

NEEDS_YAML = unittest.skipIf(yaml is None, "PyYAML not installed; compatibility tests need a reference parser")
NEEDS_YAML12 = unittest.skipIf(_RuamelYAML is None,
                               "ruamel.yaml not installed; the YAML 1.2 cross-check needs a YAML 1.2 parser")

try:  # optional CommonMark reference for the setext checks; pinned in requirements-dev.txt (unit-with-dev-deps)
    from markdown_it import MarkdownIt as _MarkdownIt
except ImportError:  # pragma: no cover - exercised wherever markdown-it-py is absent
    _MarkdownIt = None

NEEDS_COMMONMARK = unittest.skipIf(_MarkdownIt is None,
                                   "markdown-it-py not installed; the CommonMark cross-check needs a reference parser")

# (Markdown, 0-based underline lines of its setext headings as CommonMark renders them).
# Shared by the stdlib test and the markdown-it-py cross-check.
SETEXT_CASES = [
    ("Gotchas\n===\n", [1]), ("Gotchas\n---\n", [1]), ("Foo\n-\n", [1]), ("   Para\n   ---\n", [1]),
    ("Para\n    more\n---\n", [2]),  # an indented line continues the paragraph
    # inside list items: the underline is indented to the item's content
    ("1. **Step**\n   text\n   ---\n", [2]), ("1. Step\n   ---\n", [1]), ("- foo\n  ---\n", [1]),
    ("- foo\n  ===\n", [1]), ("* x\n  ---\n", [1]), ("1) x\n   ---\n", [1]), ("10. step\n    ---\n", [1]),
    ("-   wide\n    ---\n", [1]), ("- a\n  - b\n    ---\n", [2]), ("1. step\n\n   para\n   ---\n", [3]),
    ("- foo\n  bar\n  -\n", [2]), ("1. step\n    text\n    ---\n", [2]), ("1. step\n      ---\n", [1]),
    ("- a\n\tb\n\t---\n", [2]), ("Para\n- item\n  ---\n", [2]), ("- z\nlazy\n  ---\n", [2]),
    # not headings: a rule after a blank line, under a list item at the wrong column, a quote,
    # a table, HTML, a heading or code, or after an item whose text is itself a block
    ("text\n\n---\n", []), ("- item\n---\n", []), ("1. foo\n  ---\n", []), ("1. step\n---\n", []),
    ("Para\n    ---\n", []), ("> q\n---\n", []), ("| a |\n---\n", []), ("<div>\n---\n", []),
    ("## H\n---\n", []), ("- ## H\n  ---\n", []), ("-     five\n  ---\n", []), ("- item\n-\n  ---\n", []),
    ("1. step\n\n       code\n   ---\n", []), ("- a\n\nb\n---\n", [3]), ("x\n- - -\n", []),
    ("1. a\n   ```\n   x\n   ```\n   ---\n", []), ("<div>\n    deep\n- t\nPara\n  ---\n", []),
]

# Differential corpus: each document is either diagnosed by our parser or read exactly
# as the reference parser reads it (PyYAML for YAML 1.1, ruamel.yaml for YAML 1.2).
# Grouped by the construct it exercises; see docs/SKILL-SPEC.md "Supported YAML subset".
YAML_CORPUS = [
    # YAML 1.1 vs 1.2 plain scalars: booleans, octal-looking and other numbers, sexagesimal, nulls
    *(f"k: {v}\n" for v in (
        "yes", "no", "on", "off", "y", "n", "Y", "N", "true", "True", "TRUE", "tRue", "ON", "Off", "oN",
        "null", "Null", "NULL", "~", "nUll", "017", "0o17", "0x1F", "0b101", "089", "0001", "1_000", "+1", "-0",
        "0", "00", "1e3", "1E3", "1.0e3", "1.0e+3", "1e+3", "12e3", "+12e03", "0e0", ".5", "5.", "+.5", "1.",
        "1.e3", "1.5e-3", "-.5e1", "._5", "1_000.5", "-.inf", ".inf", "+.inf", ".Inf", "-.Inf", "+.INF", ".NaN",
        ".nan", ".NAN", "nan", "inf", "Infinity", "190:20:30", "190:20:30.15", "1:20", "0:30", "12:30", "1:2:3",
        "2026-09-21", "2026-9-21", "2026-09-21T10:00:00", "2026-09-21 10:00:00", "0.", "0x", "0o", "+", "-1_0",
        "0b", "0B1", "0X1F", "0O17", "1e", "3.14.15", "1,000", "=", "<<", "c++", "50%", "v1.2.3", "1.2.3")),
    # quoted scalars and escapes
    'k: "a\\tb"\n', 'k: "\\x41\\u00e9\\U0001F600"\n', 'k: "\\/"\n', 'k: "\\N\\_\\L\\P"\n', "k: 'a''b'\n",
    'k: "\\e\\a\\b\\v\\f\\0\\r"\n', 'k: "\\ "\n', 'k: "a" # c\n', "k: 'a'#c\n", 'k: "a"x\n', 'k: ""\n', "k: ''\n",
    'k: "multi\n  line"\n', "k: 'multi\n  line'\n", 'k: "\\x4"\n', 'k: "a\\\\"\n', 'k: "\\""\n', "k: '\\n'\n",
    'k: "a\tb"\n', "k: 'a\tb'\n", 'k: "a\\\nb"\n',
    # block scalars: styles, chomping, indentation indicators, blank and comment lines
    "k: |\n  a\n  b\n", "k: |-\n  a\n\n", "k: |+\n  a\n\n\n", "k: >\n  a\n  b\n\n  c\n", "k: >-\n  a\n   b\n  c\n",
    "k: >+\n  a\n", "k: |2\n    a\n  b\n", "k: >2-\n   a\n  b\n", "k: |\n\n  a\n", "k: |\n  \n  a\n",
    "k: |\n   a\n  b\n", "k: | # c\n  a\n", "k: |#c\n  a\n", "k: >\n  a\n#c\n", "k: |\n  a\n  # not comment\n",
    "k: >\n\ta\n", "k: |\nx: 1\n", "k: >\n", "k: |\n  a\n x\n", "k: |0\n  a\n", "k: |10\n  a\n",
    "k: >\n  a\n\n\n  b\n", "k: >\n  a\n    b\n  c\n", "k: |\r\n  a\r\n", "k: |\n  a", "k: |-\n  a", "k: |+\n  a",
    "k: >\n  a\n  \n  b\n", "k: >\n  a\n   \n  b\n", "k: |\n    \n  a\n", "k: >-\n\n  a\n", "k: |+\n\n", "k: |-\n",
    "k: |-+\n", "k: >+-\n  x\n", "k: |-1\n  x\n", "k: |1-\n  x\n",
    # flow sequences
    "k: [a, b]\n", "k: [a,b]\n", "k: [ a , b , ]\n", "k: [a, [b]]\n", "k: [a, {b: c}]\n", "k: ['a, b', \"c\"]\n",
    "k: [a #c]\n", "k: [a b, c]\n", "k: [a: b]\n", "k: [-a]\n", "k: [- a]\n", "k: [a, , b]\n", "k: [,]\n",
    "k: [ ]\n", "k: [yes, 1, 0o7, 1e3]\n", "k: [a]x\n", "k: [a] # c\n", "k: [\"a\" b]\n", "k: [a\n  , b]\n",
    "k: [*a]\n", "k: [&a b]\n", "k: [!t a]\n", "k: [a\tb]\n", "k: [@a]\n", "k: [#a]\n", "k: [a#b]\n",
    "k: [|a]\n", "k: [>a]\n", "k: [a, 'b''c']\n", "k: [\"a\\\"b\"]\n", "k: [~, null]\n", "k: [a]#c\n",
    "k: [a ,b]\n", "k: [?x]\n", "k: [:x]\n", "k: [a:b]\n",
    # comments
    "# c\nk: v\n", "k: v # c\n", "k: v# c\n", "k: v #c\n", "k: v\t# c\n", "k: # c\n  - a\n",
    "k:\n  # c\n  - a\n  # d\n  - b\n", "k: a\n  # c\n  b\n", "k: a\n  b # c\n  d\n", "  # indented\nk: v\n",
    # duplicate keys
    "k: a\nk: b\n", "k: a\nK: b\n", "k: a\nk : b\n",
    # tabs
    "k:\ta\n", "k: a\t\n", "\tk: a\n", "k: a\tb\n", "k:\n\t- a\n", "k: |\n\ta\n", "k:\t[a]\n", "k: |\n  a\tb\n",
    # unicode, BOM, zero-width and no-break spaces, line-break characters
    "k: \u00e9\n", "\u043a: \u0437\n", "k: \u65e5\u672c\n", "k: \ufeffa\n", "k: a\u200b\n", "k: \U0001F600\n",
    'k: "\\u00e9"\n', "k: a\u00a0b\n", "k: x\xa0\n", "k: x\x85\n", "k: x\u2028\n", "k: x\ry\n",
    # null-ish and implicit-type keys
    "k:\n", "~: a\n", "null: a\n", "yes: a\n", "1: a\n", "0o7: a\n", "1e3: a\n", "089: a\n", "on: a\n",
    "k k: a\n", "k.k: a\n", "k-k: a\n",
    # plain multi-line scalars, block lists, indicators
    "k: a\n  b\n\n  c\n", "k:\n  a\n  b\n", "k: a\n- b\n", "k:\n- a\n- b\n", "k:\n  - a\n  -  b\n",
    "k:\n  - a\n    b\n", "k:\n  - \"a\"\n  - 'b'\n", "k:\n  -\n  - a\n", "k:\n  - |\n    a\n",
    "k:\n  - >-\n    a\n    b\n", "k: a:b\n", "k: a :b\n", "k: http://x\n", "k: a -b\n", "k: -a\n", "k: ?a\n",
    "k: :a\n", "k: !a\n", "k: |a\n", "k: >a\n", "k: \"a\n", "k: 'a\n", "k: a ? b\n", "k: a, b\n", "k: a [b]\n",
    "k: a {b}\n", "k: a & b\n", "k: a * b\n", "k: %50\n", "k: a'b\n", "k: a\"b\n", "k: 'a'\n  b\n",
    "k:\n  - a\n  - b\nj: c\n", "k:\n - a\n -  b\n", "k:\n    - a\n    - b\n", "k:\n  - a\n - b\n",
    "k:\n  - a\n   - b\n", "k: a\n  - b\n", "k:\n  - a: b\n", "k:\n  - [a]\n", "k:\n  - - a\n", "k: ---\n",
    "k: ...\n", "---\nk: a\n", "k: a\n...\n", "k: ,x\n", "k: ]x\n", "k: }x\n", "k: x,y ]}\n",
]
# Documents the subset must accept: keeps the corpus from passing by diagnosing everything.
YAML_CORPUS_CLEAN = [
    "k: y\n", "k: 0o\n", "k: 1e\n", "k: v1.2.3\n", "k: 2026-09-21\n", "k: c++\n", "k: \"\\x41\\u00e9\\U0001F600\"\n",
    "k: 'a''b'\n", "k: |+\n  a\n\n\n", "k: >2-\n   a\n  b\n", "k: |-1\n  x\n", "k: [ a , b , ]\n", "k: [~, null]\n",
    "k:\n  # c\n  - a\n  # d\n  - b\n", "k: \u65e5\u672c\n", "k: a\u00a0b\n", "k: a :b\n",
    "k: |\n  a\tb\n", "k-k: a\n",
]
YAML12_NUMBERS = ["0o17", "089","1e3", "1E3", "1.0e3", "1e+3", "12e3", "+12e03", "0e0", "+.5", "1.e3"]

# Repo files allowed to use YAML outside the supported subset (skipped with a
# reason instead of failing). Empty on purpose: the five eval cases whose list
# items had an unquoted ': ' (PyYAML read each as a {text: text} mapping) have
# been quoted. Only add an entry with a linked fix in flight; a file listed
# here is still checked by test_known_divergences_are_still_diagnosed.
KNOWN_YAML_DIVERGENCE: set[str] = set()


def _norm(v):
    """PyYAML yields datetime.date for YYYY-MM-DD; our parser keeps the ISO string."""
    if isinstance(v, datetime.date):
        return v.isoformat()
    if isinstance(v, dict):
        return {(_norm(k) if isinstance(k, datetime.date) else k): _norm(x) for k, x in v.items()}
    if isinstance(v, list):
        return [_norm(x) for x in v]
    return v


def codes(text):
    return [d.code for d in sl.parse_frontmatter_with_diagnostics(text).diagnostics]


PARSE_TIMEOUT_S = 60  # generous for slow CI; a non-terminating parse never finishes at all


def _run_bounded(code: str, *args: str) -> str:
    """Run `code` (with `sl` imported) in a child interpreter under a timeout; return stdout.

    In-process there is no way to interrupt a stuck pure-Python loop, so a
    termination test must run the parser where it can be killed."""
    prelude = f"import sys\nsys.path.insert(0, {str(REPO_ROOT / 'scripts')!r})\nimport skills_lib as sl\n"
    try:
        proc = subprocess.run([sys.executable, "-c", prelude + code, *args], capture_output=True, text=True,
                              encoding="utf-8", timeout=PARSE_TIMEOUT_S)
    except subprocess.TimeoutExpired:
        raise AssertionError(f"parser did not terminate within {PARSE_TIMEOUT_S}s") from None
    if proc.returncode != 0:
        raise AssertionError(f"child failed (rc={proc.returncode}): {proc.stderr[-2000:]}")
    return proc.stdout


class TestParseFrontmatter(unittest.TestCase):
    def test_scalar_and_list_and_folded(self):
        text = """
name: my-skill
display_name: My Skill
description: >
  Use when the user wants to do X. Trigger phrases: "a", "b", "c".
  NOT for Y (see other-skill).
type: workflow
domain: specialized
capabilities:
  - thing-one
  - thing-two
requires:
"""
        fm = sl.parse_frontmatter(text)
        self.assertEqual(fm["name"], "my-skill")
        self.assertEqual(fm["type"], "workflow")
        self.assertIn("Use when the user wants to do X.", fm["description"])
        self.assertEqual(fm["capabilities"], ["thing-one", "thing-two"])
        self.assertIsNone(fm["requires"])

    def test_literal_block_preserves_newlines(self):
        # YAML "clip" chomping keeps exactly one trailing newline.
        fm = sl.parse_frontmatter("notes: |\n  line one\n  line two\n")
        self.assertEqual(fm["notes"], "line one\nline two\n")

    def test_block_chomping_indicators(self):
        fm = sl.parse_frontmatter("a: >-\n  x\n  y\nb: |+\n  z\n\nc: >\n  p\n\n  q\n")
        self.assertEqual(fm, {"a": "x y", "b": "z\n\n", "c": "p\nq\n"})

    def test_flow_lists(self):
        fm = sl.parse_frontmatter("a: [x, 'y, z', \"w\"]\nb: []\nc: [x, y,]  # trailing comma ok\n")
        self.assertEqual(fm, {"a": ["x", "y, z", "w"], "b": [], "c": ["x", "y"]})

    def test_flow_indicator_at_value_start_is_diagnosed(self):
        # Stdlib-only twin of TestYamlCompatibility.test_flow_indicator_starts_are_diagnosed,
        # so the stdlib CI job also guards it.
        for doc in ("a: ,foo\n", "a: ]x\n", "a: }x\n", "a:\n  - ,x\n", "a: [?x]\n", "a: [:x]\n", "a: =\n"):
            with self.subTest(doc=doc):
                codes = [d.code for d in sl.parse_frontmatter_with_diagnostics(doc).diagnostics]
                self.assertIn(codes, (["unsupported-syntax"], ["implicit-type"]))
        self.assertEqual(sl.parse_frontmatter_with_diagnostics("a: x,y ]}\n").diagnostics, [])

    def test_quoted_scalar_semantics(self):
        fm = sl.parse_frontmatter('a: "say \\"hi\\" \\\\ \\u00e9"\nb: \'it\'\'s # not a comment\'\n')
        self.assertEqual(fm["a"], 'say "hi" \\ é')
        self.assertEqual(fm["b"], "it's # not a comment")

    def test_list_item_escapes(self):
        fm = sl.parse_frontmatter('v:\n  - "torchvision: `weights=\\"DEFAULT\\"`"\n')
        self.assertEqual(fm["v"], ['torchvision: `weights="DEFAULT"`'])

    def test_inline_comments(self):
        fm = sl.parse_frontmatter("a: low  # reviewed\nb: x#y\nc: # nothing\nd:\n  - p  # c\n  # whole-line\n  - q\n")
        self.assertEqual(fm, {"a": "low", "b": "x#y", "c": None, "d": ["p", "q"]})

    def test_multiline_plain_scalar_is_folded(self):
        fm = sl.parse_frontmatter("inputs: a long\n  value\nnext: x\n")
        self.assertEqual(fm, {"inputs": "a long value", "next": "x"})

    def test_list_at_key_indentation(self):
        self.assertEqual(sl.parse_frontmatter("a:\n- x\n- y\n"), {"a": ["x", "y"]})

    def test_hyphenated_key_is_read_not_dropped(self):
        fm = sl.parse_frontmatter("risk-level: low\n")
        self.assertEqual(fm, {"risk-level": "low"})

    def test_duplicate_key_diagnosed_with_line(self):
        r = sl.parse_frontmatter_with_diagnostics("a: 1x\nb: y\na: z\n", line_offset=1)
        self.assertEqual(r.data["a"], "z")  # YAML keeps the last value
        [d] = r.diagnostics
        self.assertEqual((d.code, d.line, d.key), ("duplicate-key", 4, "a"))
        self.assertEqual(r.key_lines["a"], 4)

    def test_unparseable_and_indentation_diagnostics(self):
        r = sl.parse_frontmatter_with_diagnostics("a: x\nnot yaml at all\n   stray: indented\n")
        self.assertEqual([(d.code, d.line) for d in r.diagnostics],
                         [("unparseable-line", 2), ("unexpected-indentation", 3)])

    def test_tab_diagnostics(self):
        self.assertIn("tab-indentation", codes("a:\n\t- x\n"))
        self.assertIn("tab-character", codes("a: x\ty\n"))
        self.assertEqual(codes("a: |\n  col1\tcol2\n"), [])  # tabs are content inside block scalars

    def test_yaml_hazards_diagnosed(self):
        self.assertIn("mapping-in-scalar", codes("inputs: A table: rows\n"))
        self.assertIn("mapping-in-scalar", codes("a:\n  - text: more text\n"))
        self.assertIn("implicit-type", codes("a: yes\n"))
        self.assertIn("implicit-type", codes("a: 1.5\n"))
        self.assertIn("unsupported-syntax", codes("a: {b: c}\n"))
        self.assertIn("unsupported-syntax", codes("a: &anchor x\n"))
        self.assertIn("unsupported-syntax", codes('a: "never closed\n'))
        self.assertIn("unsupported-syntax", codes("a: [x, y\n"))
        self.assertIn("invalid-escape", codes('a: "\\q"\n'))
        self.assertEqual(codes("a: 2026-09-21\nb: http://x.y/z\nc: c#\nd: x:y\n"), [])

    def test_parse_frontmatter_never_raises(self):
        for text in ["", "\n\n", ":", "- a", "a: [", 'a: "', "a: |\n", "\t\t", "a:\n  - [x\n"]:
            with self.subTest(text=text):
                self.assertIsInstance(sl.parse_frontmatter(text), dict)

    def test_brace_inside_flow_list_terminates_and_is_diagnosed(self):
        # A `}` inside `[...]` once left the flow-list cursor in place forever,
        # hanging validate_skills/router/validate_evals. Run in a child process so
        # a regression fails on the timeout instead of hanging the test run.
        docs = ["related: [a}]\n", "k: [}]\n", "k: [a, b}\n", "k: [=}\"%1\n", "k: [x, }, y]\n"]
        out = _run_bounded("import json, sys\n"
                           "for doc in json.loads(sys.argv[1]):\n"
                           "    r = sl.parse_frontmatter_with_diagnostics(doc)\n"
                           "    print(json.dumps([r.data, [d.code for d in r.diagnostics]]))\n",
                           json.dumps(docs))
        results = [json.loads(line) for line in out.splitlines()]
        self.assertEqual(len(results), len(docs))
        for doc, (_data, diag_codes) in zip(docs, results, strict=True):
            with self.subTest(doc=doc):
                self.assertIn("unsupported-syntax", diag_codes)  # PyYAML rejects all of these
        self.assertEqual(results[0][0], {"related": ["a"]})

    def test_random_documents_always_terminate(self):
        """Seeded fuzz biased toward flow lists and indicator characters: the
        parser must return (never hang, never raise) on every input."""
        out = _run_bounded(
            "import random\n"
            "rng = random.Random(41)\n"
            "chars = list(\",[]{}'\\\"#:?|>-!&*%@`=\\t \\xa0\\\\\") + ['a', 'b1', '<<', '2026-02-30', '|-+', '\\n', '\\n  ']\n"
            "n = 0\n"
            "for _ in range(4000):\n"
            "    body = ''.join(rng.choice(chars) for _ in range(rng.randint(0, 12)))\n"
            "    doc = rng.choice(['k: [', 'k: ', 'k:\\n  - ', 'k: |', 'k: \"', \"k: '\"]) + body + rng.choice(['', ']', '\\n'])\n"
            "    r = sl.parse_frontmatter_with_diagnostics(doc)\n"
            "    assert isinstance(r.data, dict), doc\n"
            "    n += 1\n"
            "print(n)\n")
        self.assertEqual(out.strip(), "4000")

    def test_unicode_whitespace_is_part_of_a_plain_scalar(self):
        # YAML strips only spaces/tabs; str.strip() would also eat NBSP / U+3000 and make
        # the validator see a different `name` than a YAML parser does.
        self.assertEqual(sl.parse_frontmatter("name: x\xa0\n"), {"name": "x\xa0"})
        self.assertEqual(sl.parse_frontmatter("name: x\u3000  # c\n"), {"name": "x\u3000"})
        self.assertEqual(sl.parse_frontmatter("k: [x\xa0, \xa0y]\n"), {"k": ["x\xa0", "\xa0y"]})
        self.assertEqual(sl.parse_frontmatter("name: x \t\n"), {"name": "x"})

    def test_yaml_line_breaks_and_controls_are_diagnosed(self):
        # YAML treats CR, NEL, LS and PS as line breaks (PyYAML reads `name: x<NEL>`
        # as 'x') and rejects VT/FF/other controls; we split on LF and strip only
        # spaces/tabs, so each is diagnosed. Stdlib twin of the PyYAML check below.
        for doc in ("name: x\x85\n", "k: 1\u2028\n", "k: [a\x85]\n", "k:\n  - a\u2028\n", "name: x\u2029\n",
                    'k: "a\x85b"\n', "k: |\n  a\x85b\n", "k: x\x0b\n", "k: x\x0c\n", "k: x\ry\n", "k: x\x00\n"):
            with self.subTest(doc=doc):
                self.assertIn("unsupported-syntax", codes(doc))
        # CRLF is a single line break: read exactly as LF, with no diagnostics.
        r = sl.parse_frontmatter_with_diagnostics("name: x\r\nk: [a, b]\r\nd: |\r\n  p\r\n", line_offset=1)
        self.assertEqual((r.data, r.diagnostics), ({"name": "x", "k": ["a", "b"], "d": "p\n"}, []))
        self.assertEqual(r.key_lines, {"name": 2, "k": 3, "d": 4})

    def test_yaml_rejected_scalars_are_diagnosed(self):
        # Each is a whole-document error for yaml.safe_load (ScannerError /
        # ConstructorError / ValueError); stdlib twin of the PyYAML check below.
        for doc in ("k: |-+\n", "k: >+-\n  x\n", "k: |1+-\n  x\n", "a: <<\n", "k: [<<]\n",
                    "a: 2026-02-30\n", "k: [2026-13-01]\n", "k:\n  - 2026-00-10\n"):
            with self.subTest(doc=doc):
                self.assertTrue(sl.parse_frontmatter_with_diagnostics(doc).diagnostics)
        for doc, expected in (("k: |-1\n  x\n", {"k": " x"}), ("k: |1-\n  x\n", {"k": " x"}),
                              ("k: >+\n  x\n", {"k": "x\n"}), ("a: 2024-02-29\n", {"a": "2024-02-29"}),
                              ("a: <<x\n", {"a": "<<x"})):
            with self.subTest(doc=doc):
                r = sl.parse_frontmatter_with_diagnostics(doc)
                self.assertEqual((r.data, r.diagnostics), (expected, []))


    def test_yaml12_numbers_are_diagnosed(self):
        # Strings to PyYAML (YAML 1.1) but numbers to a YAML 1.2 parser; stdlib twin of
        # TestYaml12Compatibility, so the stdlib CI job guards it too.
        for v in YAML12_NUMBERS:
            for doc in (f"k: {v}\n", f"k: [{v}]\n", f"k:\n  - {v}\n"):
                with self.subTest(doc=doc):
                    self.assertEqual(codes(doc), ["implicit-type"])
            with self.subTest(key=v):  # a key starting with `+` or `.` is already unparseable-line
                self.assertTrue(codes(f"{v}: a\n"))
        r = sl.parse_frontmatter_with_diagnostics("k: 1e3\n")
        self.assertIn("YAML 1.2", r.diagnostics[0].message)

    def test_corpus_clean_documents_stay_clean(self):
        for doc in YAML_CORPUS_CLEAN:
            with self.subTest(doc=doc):
                self.assertEqual(codes(doc), [])
        self.assertTrue(set(YAML_CORPUS_CLEAN) <= set(YAML_CORPUS))

    def test_corpus_never_raises(self):
        for doc in YAML_CORPUS:
            with self.subTest(doc=doc):
                self.assertIsInstance(sl.parse_frontmatter_with_diagnostics(doc).data, dict)


class TestSplitAndLoad(unittest.TestCase):
    def test_dashes_inside_value_are_not_a_fence(self):
        fm, body = sl._split_frontmatter("---\na: x---y\nb: ---\n---\n\n## Body\n")
        self.assertEqual(fm, "a: x---y\nb: ---\n")
        self.assertEqual(body, "## Body\n")

    def test_missing_fences_raise(self):
        with self.assertRaises(sl.FrontmatterError):
            sl._split_frontmatter("no fence\n")
        with self.assertRaises(sl.FrontmatterError):
            sl._split_frontmatter("---\na: b\n")

    def test_load_skill_attaches_file_line_numbers(self):
        skill = sl.load_skill(REPO_ROOT / "data-preprocessing")
        self.assertEqual(skill.key_lines["name"], 2)  # line 1 is the opening ---
        self.assertEqual(skill.diagnostics, [])
        self.assertEqual(skill.raw.split("\n")[skill.body_line_offset()].strip(), skill.body.split("\n")[0].strip())


class TestSkillHelpers(unittest.TestCase):
    def setUp(self):
        from testutil import make_skill
        self.skill = make_skill("demo")

    def test_headers(self):
        self.assertIn("Overview", self.skill.headers())
        self.assertIn("Workflow", self.skill.headers())
        self.assertIn("Gotchas", self.skill.headers())
        self.assertIn("References", self.skill.headers())

    def test_bullets_under(self):
        self.assertEqual(len(self.skill.bullets_under("Gotchas")), 3)
        self.assertEqual(len(self.skill.bullets_under("References")), 2)
        self.assertEqual(self.skill.bullets_under("Nonexistent Header"), [])

    def test_code_blocks(self):
        blocks = self.skill.code_blocks(lang="python")
        self.assertEqual(len(blocks), 1)
        self.assertIn("print", blocks[0][1])

    def test_markdown_links(self):
        links = self.skill.markdown_links()
        self.assertEqual(len(links), 2)
        self.assertEqual(links[0][1], "https://example.com/a")

    def test_prose_blanks_code_but_keeps_lines(self):
        body = "text [[a]]\n```python\nx[[0]]\n```\ninline `y[[1]]` [[b]]\n"
        prose = sl.strip_code(body)
        self.assertEqual(prose.count("\n"), body.count("\n"))
        self.assertIn("[[a]]", prose)
        self.assertIn("[[b]]", prose)
        self.assertNotIn("[[0]]", prose)
        self.assertNotIn("[[1]]", prose)

    def test_strip_code_honours_every_fence_form(self):
        body = ("a [[x]]\n~~~ md\n[[t1]]\n```\n[[t2]]\n~~~\nb\n````\n```\n[[t3]]\n```\n````\n"
                "<!--\n[[c]]\n-->\n```python\n[[t4]]\n")  # the last fence never closes
        prose = sl.strip_code(body)
        self.assertEqual(prose.count("\n"), body.count("\n"))
        for hidden in ("[[t1]]", "[[t2]]", "[[t3]]", "[[t4]]"):
            self.assertNotIn(hidden, prose)
        self.assertIn("[[x]]", prose)
        self.assertIn("[[c]]", prose)  # comments are kept for the link checks

    def test_scan_markdown_kinds_fences_and_headings(self):
        scan = sl.scan_markdown("# T\n<!-- one-line -->\n```py  extra\n## no\n```\ntext\nSetext\n===\n## H ##\n")
        self.assertEqual(scan.kinds, ["text", "html", "fence", "code", "fence", "text", "text", "text", "text", "text"])
        self.assertEqual([(h.level, h.text) for h in scan.headings], [(1, "T"), (2, "H")])
        self.assertEqual(scan.fences, [sl.Fence(2, 4, "```", "py", "## no\n")])
        self.assertEqual([(h.line, h.level, h.text) for h in scan.setext], [(7, 1, "text Setext")])  # takes the whole paragraph

    def test_setext_detection_table(self):
        # Expected values are what CommonMark renders (checked against markdown-it-py in
        # TestSetextAgainstCommonMark wherever it is installed): the underline line of
        # each setext heading. Misses are allowed only in the direction documented in
        # docs/SKILL-SPEC.md limitation 6; a false heading never is.
        for doc, expected in SETEXT_CASES:
            with self.subTest(doc=doc):
                self.assertEqual([h.line for h in sl.scan_markdown(doc).setext], expected)
        [h] = sl.scan_markdown("1. **Step**\n   text\n   ---\n").setext
        self.assertEqual((h.line, h.level, h.text), (2, 2, "**Step** text"))  # the list marker is not heading text

    def test_code_blocks_include_tilde_fences_and_full_info_strings(self):
        from testutil import make_skill
        skill = make_skill("demo", body="~~~python title=x.py\nprint(1)\n~~~\n   ```bash\n   ls\n   ```\n")
        self.assertEqual(skill.code_blocks(None), [("python", "print(1)\n"), ("bash", "ls\n")])
        self.assertEqual(skill.code_blocks("python"), [("python", "print(1)\n")])


@NEEDS_YAML
class TestYamlCompatibility(unittest.TestCase):
    """Claude Code reads frontmatter with a real YAML parser; we must read the same thing."""

    def _files(self):
        files = [d / "SKILL.md" for d in sl.iter_skill_dirs()] + sorted((REPO_ROOT / "evals").glob("*/*.yaml"))
        self.assertGreater(len(files), 30)
        return files

    def test_every_repo_file_matches_yaml_safe_load(self):
        for path in self._files():
            rel = path.relative_to(REPO_ROOT).as_posix()
            with self.subTest(file=rel):
                raw = path.read_text(encoding="utf-8")
                text, offset = (sl._split_frontmatter(raw)[0], 1) if path.name == "SKILL.md" else (raw, 0)
                ours = sl.parse_frontmatter_with_diagnostics(text, line_offset=offset)
                if ours.diagnostics:
                    if rel in KNOWN_YAML_DIVERGENCE:
                        self.skipTest(f"known divergence, reported for fixing: {ours.diagnostics[0]}")
                    self.fail(f"{rel} uses YAML outside the supported subset: "
                              + "; ".join(map(str, ours.diagnostics)))
                self.assertEqual(ours.data, _norm(yaml.safe_load(text)))

    def test_known_divergences_are_still_diagnosed(self):
        # Each listed file must either be fixed (then it is clean and equal, and
        # can be dropped from the list) or still be flagged — never silently differ.
        for rel in sorted(KNOWN_YAML_DIVERGENCE):
            path = REPO_ROOT / rel
            if not path.exists():
                continue
            with self.subTest(file=rel):
                text = path.read_text(encoding="utf-8")
                ours = sl.parse_frontmatter_with_diagnostics(text)
                if not ours.diagnostics:
                    self.assertEqual(ours.data, _norm(yaml.safe_load(text)))

    def test_flow_indicator_starts_are_diagnosed(self):
        """YAML rejects these whole documents; accepting them silently would let a
        skill pass validation while Claude Code loads it with no frontmatter."""
        for doc in ("a: ,foo\n", "a: ]x\n", "a: }x\n", "a:\n  - ,x\n", "a:\n  - ]x\n",
                    "a: [:x]\n", "a: =\n", "a:\n  - =\n"):
            with self.subTest(doc=doc):
                self.assertTrue(sl.parse_frontmatter_with_diagnostics(doc).diagnostics)
                with self.assertRaises(yaml.YAMLError):
                    yaml.safe_load(doc)
        # PyYAML reads a leading `?` in a flow list as a mapping key, not text.
        for doc in ("a: [?x]\n", "a: [x, ?y]\n"):
            with self.subTest(doc=doc):
                self.assertTrue(sl.parse_frontmatter_with_diagnostics(doc).diagnostics)
                self.assertNotEqual(yaml.safe_load(doc), sl.parse_frontmatter(doc))
        # The same characters later in a plain scalar are ordinary text.
        for doc in ("a: x,y\n", "a: x]\n", "a: x}\n", "a: ==\n", "a: [a:b]\n"):
            with self.subTest(doc=doc):
                r = sl.parse_frontmatter_with_diagnostics(doc)
                self.assertEqual(r.diagnostics, [])
                self.assertEqual(r.data, yaml.safe_load(doc))

    def test_audit_divergences_match_pyyaml(self):
        """Inputs where the parser once disagreed with yaml.safe_load: either we
        diagnose (and PyYAML rejects the document) or we read the same data."""
        for doc in ("k: |-+\n", "k: >+-\n  x\n", "a: <<\n", "k: [<<]\n", "a: 2026-02-30\n",
                    "k: [2026-13-01]\n"):
            with self.subTest(doc=doc):
                self.assertTrue(sl.parse_frontmatter_with_diagnostics(doc).diagnostics)
                with self.assertRaises((yaml.YAMLError, ValueError)):
                    yaml.safe_load(doc)
        # `[a}]` / `[}]` once hung our parser, so only PyYAML runs in-process here;
        # test_brace_inside_flow_list_terminates_and_is_diagnosed checks our side
        # in a child process, where a regression fails on a timeout instead of
        # hanging this whole module.
        for doc in ("related: [a}]\n", "k: [}]\n"):
            with self.subTest(doc=doc), self.assertRaises(yaml.YAMLError):
                yaml.safe_load(doc)
        # YAML line breaks (CR, NEL, LS, PS) and characters PyYAML's reader rejects:
        # we split on LF only, so we diagnose rather than silently read something else.
        for doc in ("name: x\x85\n", "k: 1\x85\n", "k: true\u2028\n", "k: [a\x85]\n", "k:\n  - a\u2028\n",
                    "name: x\u2029\n", 'k: "a\x85b"\n', "k: x\x0b\n", "k: x\x0c\n", "k: x\ry\n",
                    "k: |\n  a\x85b\n", "k: x\x7f\n"):
            with self.subTest(doc=doc):
                r = sl.parse_frontmatter_with_diagnostics(doc)
                self.assertIn("unsupported-syntax", [d.code for d in r.diagnostics])
                try:
                    ref = yaml.safe_load(doc)
                except yaml.YAMLError:
                    continue
                self.assertNotEqual(r.data, ref)  # a real divergence, hence the diagnostic
        for doc in ("name: x\xa0\n", "name: x\u3000  # c\n", "k: [x\xa0, \xa0y]\n", "k: |-1\n  x\n",
                    "k: |1-\n  x\n", "a: <<x\n", "name: x\r\n", "a: x\r\nb: [p, q]\r\n",
                    "k: |\r\n  a\r\n  b\r\n", 'k: "\\N"\n'):
            with self.subTest(doc=doc):
                r = sl.parse_frontmatter_with_diagnostics(doc)
                self.assertEqual(r.diagnostics, [])
                self.assertEqual(r.data, yaml.safe_load(doc))

    def test_differential_corpus_matches_pyyaml(self):
        """Every corpus document is diagnosed, or read exactly as yaml.safe_load reads it;
        a document PyYAML rejects is always diagnosed."""
        clean = 0
        for doc in YAML_CORPUS:
            with self.subTest(doc=doc):
                ours = sl.parse_frontmatter_with_diagnostics(doc)
                try:
                    ref = yaml.safe_load(doc)
                except (yaml.YAMLError, ValueError) as e:
                    self.assertTrue(ours.diagnostics, f"accepted a document PyYAML rejects ({type(e).__name__})")
                    continue
                if not ours.diagnostics:
                    clean += 1
                    self.assertEqual(ours.data, _norm(ref) if ref is not None else {})
        self.assertGreaterEqual(clean, len(YAML_CORPUS_CLEAN))

    def test_seeded_differential_fuzz(self):
        """Random documents built from YAML-ish fragments: whenever our parser
        reports no diagnostics, its result must equal yaml.safe_load."""
        frags = ["alpha", "b-c", "it's", '"q"', "'s'", "a: b", "#c", " #c", "x#y", "1", "no", "2026-01-02",
                 "[a, b]", "[]", "{a: 1}", ":", "- x", "-", "|", "> x", "&a", "*a", "!t", "\\", "\t", "é",
                 "key:", "a:b", "http://u", '"open', "'u",
                 # flow indicators / PyYAML-only types that once slipped through undiagnosed
                 ",x", "]", "}", "[?x]", "[:x]", "=",
                 # once silently stripped, or accepted though PyYAML rejects them. (`[a}]`, which
                 # once hung the parser, is left to the child-process tests above: in-process a
                 # regression would hang this test instead of failing it.)
                 "x\xa0", "\u3000", "<<", "2026-02-30", "|-+", ">+-",
                 # YAML line breaks / reader-rejected characters (CR, NEL, LS, PS, VT, FF)
                 "x\x85", "\x85", "1\u2028", "\u2029", "\x0b", "\x0c", "\r", "x\r\n"]
        rng = random.Random(1234)

        def line():
            ind = rng.choice(["", "", "  ", "    ", " ", "\t"])
            k = rng.random()
            words = " ".join(rng.choice(frags) for _ in range(rng.randint(0, 3)))
            if k < 0.35:
                return f"{ind}k{rng.randint(0, 3)}:{rng.choice(['', ' '])}{words}"
            if k < 0.6:
                return f"{ind}- {words}"
            if k < 0.7:
                return f"{ind}# comment"
            if k < 0.8:
                return ""
            if k < 0.9:
                return f"{ind}{rng.choice(['>', '|', '>-', '|+', '>2'])}"
            return f"{ind}{words or 'w'}"

        clean = 0
        for _ in range(3000):
            doc = "\n".join(line() for _ in range(rng.randint(1, 7))) + rng.choice(["\n", ""])
            ours = sl.parse_frontmatter_with_diagnostics(doc)
            if ours.diagnostics:
                continue
            clean += 1
            try:
                ref = _norm(yaml.safe_load(doc))
            except yaml.YAMLError as e:
                self.fail(f"we accepted a document PyYAML rejects ({type(e).__name__}): {doc!r}")
            self.assertEqual(ours.data, ref if ref is not None else {}, repr(doc))
        self.assertGreater(clean, 50)  # the generator must keep exercising the clean path


@NEEDS_YAML12
class TestYaml12Compatibility(unittest.TestCase):
    """A document inside the subset must also read the same under YAML 1.2 (ruamel.yaml),
    since Claude Code's YAML version is not documented."""

    def setUp(self):
        self.loader = _RuamelYAML(typ="safe", pure=True)

    def load(self, doc):
        return _norm(self.loader.load(doc))

    def test_differential_corpus_matches_yaml12(self):
        for doc in YAML_CORPUS:
            with self.subTest(doc=doc):
                ours = sl.parse_frontmatter_with_diagnostics(doc)
                if ours.diagnostics:
                    continue
                ref = self.load(doc)  # raises (failing the test) if YAML 1.2 rejects a document we accept
                self.assertEqual(ours.data, ref if ref is not None else {})

    def test_yaml12_numbers_really_are_numbers(self):
        for v in YAML12_NUMBERS:
            with self.subTest(v=v):
                self.assertIsInstance(self.load(f"k: {v}\n")["k"], (int, float))
                if yaml is not None:
                    self.assertIsInstance(yaml.safe_load(f"k: {v}\n")["k"], str)

    def test_every_repo_file_matches_yaml12(self):
        files = [d / "SKILL.md" for d in sl.iter_skill_dirs()] + sorted((REPO_ROOT / "evals").glob("*/*.yaml"))
        for path in files:
            with self.subTest(file=path.relative_to(REPO_ROOT).as_posix()):
                raw = path.read_text(encoding="utf-8")
                text, offset = (sl._split_frontmatter(raw)[0], 1) if path.name == "SKILL.md" else (raw, 0)
                ours = sl.parse_frontmatter_with_diagnostics(text, line_offset=offset)
                self.assertEqual(ours.diagnostics, [])
                self.assertEqual(ours.data, self.load(text))

    def test_seeded_fuzz_matches_yaml12(self):
        frags = ["alpha", "it's", '"q"', "'s'", "a: b", "#c", " #c", "1", "no", "on", "0o17", "089", "1e3", "+.5",
                 "[a, b]", "[]", ":", "- x", "-", "|", "> x", "\\", "\t", "é", "key:", "a:b", "~", "null"]
        rng = random.Random(77)
        clean = 0
        for _ in range(1500):
            lines = []
            for _ in range(rng.randint(1, 5)):
                words = " ".join(rng.choice(frags) for _ in range(rng.randint(0, 3)))
                lines.append(rng.choice([f"k{rng.randint(0, 3)}: {words}", f"  - {words}", "", f"  {words}",
                                         rng.choice(["k: >", "k: |-", "k:"])]))
            doc = "\n".join(lines) + "\n"
            ours = sl.parse_frontmatter_with_diagnostics(doc)
            if ours.diagnostics:
                continue
            clean += 1
            ref = self.load(doc)
            self.assertEqual(ours.data, ref if ref is not None else {}, repr(doc))
        self.assertGreater(clean, 50)


@NEEDS_COMMONMARK
class TestSetextAgainstCommonMark(unittest.TestCase):
    """scan_markdown's setext detection vs markdown-it-py (CommonMark, with GFM tables on).
    Exact agreement on SETEXT_CASES and on every repo SKILL.md; on random documents it may
    miss a heading (limitation 6) but must never report one CommonMark does not render."""

    @staticmethod
    def reference(text):
        return sorted(t.map[1] - 1 for t in _MarkdownIt("commonmark").enable("table").parse(text)
                      if t.type == "heading_open" and t.markup in ("=", "-"))

    def test_cases_match_commonmark(self):
        for doc, expected in SETEXT_CASES:
            with self.subTest(doc=doc):
                self.assertEqual(self.reference(doc), expected)

    def test_repo_skills_match_commonmark(self):
        for path in sorted(REPO_ROOT.glob("*/SKILL.md")):
            with self.subTest(file=path.parent.name):
                body = path.read_text(encoding="utf-8")
                self.assertEqual(sorted(h.line for h in sl.scan_markdown(body).setext), self.reference(body))

    def test_seeded_fuzz_never_reports_a_false_heading(self):
        atoms = ["Para", "text", "- item", "  - sub", "1. step", "   cont", "    deep", "  cont2", "---", "   ---",
                 "  ---", "    ---", "===", "   ===", "", "> q", "## H", "* x", "10. y", "-", "      ---", "\tt",
                 "- - x", "1. - x", "-     five", "2. z", "- ## H", "-\tx", "| a |", "<div>", "> - q", "***", "=",
                 "   -", "x ---", "        ---", "\t---", "000. z"]
        rng = random.Random(20261030)
        found = 0
        for _ in range(3000):
            doc = "\n".join(rng.choice(atoms) for _ in range(rng.randint(2, 7))) + "\n"
            ours = {h.line for h in sl.scan_markdown(doc).setext}
            ref = set(self.reference(doc))
            self.assertLessEqual(ours, ref, repr(doc))
            found += len(ours)
        self.assertGreater(found, 300)  # the fuzz really exercises detection, not only the safe direction


if __name__ == "__main__":
    unittest.main()
