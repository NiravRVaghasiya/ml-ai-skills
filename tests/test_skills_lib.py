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

NEEDS_YAML = unittest.skipIf(yaml is None, "PyYAML not installed; compatibility tests need a reference parser")

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


if __name__ == "__main__":
    unittest.main()
