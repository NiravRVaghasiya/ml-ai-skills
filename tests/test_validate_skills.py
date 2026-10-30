import datetime as dt
import io
import json
import re
import unittest
from contextlib import redirect_stdout

from testutil import REPO_ROOT, make_skill, sl
import validate_skills as vs


def rules(report):
    return [e["rule"] for e in report.errors]


def codes(items):
    return [(e["code"], e.get("field")) for e in items]


class TestFrontmatterChecks(unittest.TestCase):
    def test_valid_skill_has_no_errors(self):
        s = make_skill("demo")
        report = vs.Report()
        vs.check_frontmatter(s, report)
        vs.check_sections(s, report)
        vs.check_code_fences(s, report)
        self.assertEqual(report.errors, [])

    def test_missing_field_flagged(self):
        s = make_skill("demo")
        del s.frontmatter["evidence_level"]
        report = vs.Report()
        vs.check_frontmatter(s, report)
        self.assertIn("missing required metadata", rules(report))
        self.assertIn(("missing-required-key", "evidence_level"), codes(report.errors))

    def test_invalid_enum_flagged(self):
        s = make_skill("demo", type="not-a-real-type")
        report = vs.Report()
        vs.check_frontmatter(s, report)
        self.assertIn("invalid metadata value", rules(report))

    def test_name_mismatch_flagged(self):
        s = make_skill("demo", name="something-else")
        report = vs.Report()
        vs.check_frontmatter(s, report)
        self.assertIn("name != folder name", rules(report))

    def test_empty_list_field_is_not_a_missing_field(self):
        # `requires:` with no items parses to None, representing "no hard
        # dependencies" — that's valid, not a missing-field error.
        s = make_skill("demo", requires=None, conflicts=None)
        report = vs.Report()
        vs.check_frontmatter(s, report)
        self.assertNotIn(("missing-required-key", "requires"), codes(report.errors))
        self.assertNotIn(("missing-required-key", "conflicts"), codes(report.errors))

    def test_truly_absent_list_field_is_flagged(self):
        s = make_skill("demo")
        del s.frontmatter["capabilities"]
        report = vs.Report()
        vs.check_frontmatter(s, report)
        self.assertIn(("missing-required-key", "capabilities"), codes(report.errors))

    def test_missing_not_for_clause_flagged(self):
        s = make_skill("demo", description="Use when the user wants to do X. Trigger phrases: \"a\", \"b\", \"c\".")
        report = vs.Report()
        vs.check_frontmatter(s, report)
        self.assertIn("description missing 'NOT for ...' clause", rules(report))

    def test_unknown_key_suggests_closest(self):
        s = make_skill("demo")
        s.frontmatter["evidence-level"] = s.frontmatter.pop("evidence_level")
        report = vs.Report()
        vs.check_frontmatter(s, report)
        [unknown] = [e for e in report.errors if e["code"] == "unknown-key"]
        self.assertEqual((unknown["field"], unknown["suggestion"]), ("evidence-level", "evidence_level"))
        self.assertIn("evidence_level", unknown["fix"])

    def test_calendar_and_future_dates(self):
        today = dt.date(2026, 10, 27)
        cases = {"2026-13-45": "invalid-date", "2026-02-30": "invalid-date", "2026-9-1": "invalid-date",
                 "2026-11-30": "future-date", "2026-10-28": None, "2026-10-27": None}
        for value, expected in cases.items():
            with self.subTest(value=value):
                report = vs.Report()
                vs.check_frontmatter(make_skill("demo", last_verified=value), report, today=today)
                found = [c for c, f in codes(report.errors) if f == "last_verified"]
                self.assertEqual(found, [expected] if expected else [])

    def test_list_item_rules(self):
        s = make_skill("demo", capabilities=["Good_Tag", "ok-tag", "ok-tag"], related=[None])
        report = vs.Report()
        vs.check_frontmatter(s, report)
        found = codes(report.errors)
        self.assertIn(("invalid-list-item", "capabilities"), found)
        self.assertIn(("duplicate-list-item", "capabilities"), found)
        self.assertIn(("invalid-type", "related"), found)

    def test_claude_name_and_description_limits(self):
        long_name = "a" * 65
        report = vs.Report()
        vs.check_frontmatter(make_skill(long_name), report)
        self.assertIn(("name-too-long", "name"), codes(report.errors))

        report = vs.Report()
        vs.check_frontmatter(make_skill("claude-helper"), report)
        self.assertIn(("name-reserved-word", "name"), codes(report.errors))

        desc = 'Use when X. Trigger phrases: "a", "b", "c". NOT for Y. ' + "z" * 1024
        report = vs.Report()
        vs.check_frontmatter(make_skill("demo", description=desc), report)
        self.assertIn(("description-too-long", "description"), codes(report.errors))

        desc = 'Use when <instructions> appear. Trigger phrases: "a", "b", "c". NOT for Y.'
        report = vs.Report()
        vs.check_frontmatter(make_skill("demo", description=desc), report)
        self.assertIn(("description-xml-tag", "description"), codes(report.errors))

        desc = 'Use when a < b and c > d. Trigger phrases: "a", "b", "c". NOT for Y.'
        report = vs.Report()
        vs.check_frontmatter(make_skill("demo", description=desc), report)
        self.assertNotIn(("description-xml-tag", "description"), codes(report.errors))

    def test_parser_diagnostics_become_errors(self):
        s = make_skill("demo")
        s.diagnostics.append(vs.sl.Diagnostic(5, "duplicate-key", "duplicate key 'level'", "level"))
        report = vs.Report()
        vs.check_frontmatter(s, report)
        [e] = [e for e in report.errors if e["code"] == "yaml-syntax"]
        self.assertEqual((e["field"], e["line"], e["problem"]), ("level", 5, "duplicate-key"))

    def test_version_constraint_format_is_a_warning(self):
        good = "scikit-learn>=1.4,<2: GridSearchCV(cv=...) (model-knowledge estimate, not live-verified)"
        bad = "scikit-learn: GridSearchCV is fine"
        report = vs.Report()
        vs.check_frontmatter(make_skill("demo", version_constraints=[good, bad]), report)
        self.assertEqual(report.errors, [])
        self.assertEqual([w["value"] for w in report.warnings if w["code"] == "version-constraint-format"], [bad])


def _desc_findings(desc):
    report = vs.Report()
    vs.check_frontmatter(make_skill("demo", description=f'{desc} Trigger phrases: "a", "b", "c". NOT for Y.'), report)
    return ([e for e in report.errors if e["code"] == "description-xml-tag"],
            [w for w in report.warnings if w["code"] == "description-angle-bracket"])


class TestDescriptionTagRule(unittest.TestCase):
    """Claude's docs say only "Cannot contain XML tags"; the validator's reading of that is
    table-driven here so a change to it is a deliberate, visible decision."""

    # (description text, the tag-like text the error must report)
    TAGS = [
        ("Use when <task> happens.", "<task>"),
        ("Close with </x> please.", "</x>"),
        ("Use when the user wants to <do X>.", "<do X>"),
        ("Line<br/>break.", "<br/>"),
        ("Line <br /> break.", "<br />"),
        ('Use <a href="x">links</a>.', '<a href="x">'),
        ("Placeholder <phrase 1> here.", "<phrase 1>"),
        ("Generic List<int> type.", "<int>"),  # conservative: an HTML/XML parser reads this as a tag
        ("Generic Map<K,V> type.", "<K,V>"),
        ("A <!-- comment --> here.", "<!-- comment -->"),
        ("A <?xml version='1.0'?> header.", "<?xml version='1.0'?>"),
        ("Unicode <é> tag.", "<é>"),
        ("Underscore <_x> tag.", "<_x>"),
        ("Attribute-only <a=\"b\"> tag.", '<a="b">'),
        ("Spans a<b and c>d.", "<b and c>"),  # conservative: no space after `<`, so it looks like a tag
        ("Nested <a <b> c.", "<b>"),  # the match starts at the last `<` before the `>`
    ]
    # Not tags, but still '<' or '>': a warning, because Anthropic's upload validator rejects them.
    ANGLE_ONLY = ["Use when a < b and c > d.", "Use when x <= y.", "Map inputs -> outputs.", "We <3 tests.",
                  "Significant at p<0.05.", "Use a </3 sad face.", "R assignment x <- 1.", "Shift a << 2.",
                  "Unclosed <tag at the end.", "Greater: x > y.",
                  "Spaced <a < b> pair."]  # documented trade-off of the linear-time pattern (validate_skills.py)

    def test_tag_like_text_is_an_error(self):
        for desc, expected in self.TAGS:
            with self.subTest(desc=desc):
                errors, warnings = _desc_findings(desc)
                self.assertEqual([e["value"] for e in errors], [expected])
                self.assertEqual(warnings, [])  # one finding per description, the more serious one

    def test_comparisons_and_arrows_are_only_a_warning(self):
        for desc in self.ANGLE_ONLY:
            with self.subTest(desc=desc):
                errors, warnings = _desc_findings(desc)
                self.assertEqual(errors, [])
                self.assertEqual(len(warnings), 1)

    def test_plain_description_has_neither(self):
        self.assertEqual(_desc_findings("Use when the user wants to do X (see y)."), ([], []))

    def test_search_is_linear_on_unclosed_tag_starts(self):
        # With `[^>]*` this input took about 8 s at 80,000 characters (quadratic); the
        # description is searched before the length check, and export_skills.py reuses
        # the pattern, so one pasted SKILL.md could stall CI. Linear, it takes milliseconds.
        import time
        for text in ("<a" * 100_000, "</a" * 70_000, "<!" * 100_000):
            with self.subTest(text=text[:6]):
                started = time.perf_counter()
                self.assertIsNone(vs.XML_TAG_RE.search(text))
                self.assertLess(time.perf_counter() - started, 5.0)

    def test_pattern_is_shared_with_the_exporter(self):
        # scripts/export_skills.py imports XML_TAG_RE; keep the name and the .search() API.
        self.assertEqual(vs.XML_TAG_RE.search("x <y> z").group(0), "<y>")


class TestPlaceholderText(unittest.TestCase):
    def test_todo_in_any_frontmatter_text_is_an_error(self):
        for key, value in (("description", 'TODO Use when X. Trigger phrases: "a", "b", "c". NOT for Y.'),
                           ("inputs", "TODO one line"), ("display_name", "TODO Name"),
                           ("capabilities", ["ok-tag", "todo-tag-one"]), ("related", ["x-todo"])):
            with self.subTest(key=key):
                report = vs.Report()
                vs.check_frontmatter(make_skill("demo", **{key: value}), report)
                self.assertIn(("placeholder-text", key), codes(report.errors))

    def test_ordinary_words_are_not_placeholders(self):
        report = vs.Report()
        vs.check_frontmatter(make_skill("demo", inputs="A to-do list of todos (Todo app)",
                                        capabilities=["todolist-parsing"]), report)
        self.assertNotIn("placeholder-text", [c for c, _ in codes(report.errors)])

    def test_unchanged_template_copy_fails_with_a_specific_message(self):
        """A skill copied from _TEMPLATE/SKILL.md without filling it in must fail on its
        placeholders -- not on description-xml-tag, as the old `<do X>` placeholders did."""
        template = sl.load_skill(REPO_ROOT / "_TEMPLATE")
        self.assertEqual(template.diagnostics, [])
        skill = sl.Skill(slug="new-skill", path=REPO_ROOT / "new-skill" / "SKILL.md",
                         frontmatter=template.frontmatter, body=template.body, raw=template.raw,
                         key_lines=template.key_lines)
        report = vs.Report()
        vs.check_frontmatter(skill, report, today=dt.date(2026, 10, 29))
        found = codes(report.errors)
        self.assertNotIn(("description-xml-tag", "description"), found)
        for key in ("description", "display_name", "inputs", "outputs", "capabilities"):
            self.assertIn(("placeholder-text", key), found)
        [desc] = [e for e in report.errors if e["code"] == "placeholder-text" and e["field"] == "description"]
        self.assertIn("TODO", desc["rule"])
        self.assertIn("_TEMPLATE/SKILL.md", desc["fix"])
        self.assertEqual(desc["line"], template.key_lines["description"])
        # The template still states every rule's shape, so filling the TODOs is all that is left.
        self.assertNotIn(("description-not-for", "description"), found)
        self.assertNotIn(("description-trigger-phrases", "description"), found)


class TestSectionChecks(unittest.TestCase):
    def test_workflow_skill_requires_workflow_section(self):
        body = "\n## Overview\nx\n\n## Gotchas\n- a\n- b\n- c\n\n## References\n- [a](https://x.com) - r\n- [b](https://y.com) - r\n"
        s = make_skill("demo", body=body, type="workflow")
        report = vs.Report()
        vs.check_sections(s, report)
        self.assertIn(("missing-section", "Workflow"),
                      [(e["code"], e.get("section")) for e in report.errors])

    def test_reference_skill_rejects_workflow_section(self):
        s = make_skill("demo", type="reference")  # default body has ## Workflow
        report = vs.Report()
        vs.check_sections(s, report)
        self.assertIn(("wrong-section-for-type", "Workflow"),
                      [(e["code"], e.get("section")) for e in report.errors])

    def test_too_few_gotchas_flagged(self):
        body = "\n## Overview\nx\n\n## Workflow\n1. **s** - d\n```python\npass\n```\n\n## Gotchas\n- only one\n\n## References\n- [a](https://x.com) - r\n- [b](https://y.com) - r\n"
        s = make_skill("demo", body=body)
        report = vs.Report()
        vs.check_sections(s, report)
        self.assertIn("fewer than 3 gotchas", rules(report))


GOOD_TAIL = ("\n## Gotchas\n- a\n- b\n- c\n\n## References\n- [a](https://x.com) - r\n- [b](https://y.com) - r\n")


def _body_report(body, type_="reference"):
    s = make_skill("demo", body=body, type=type_)
    report = vs.Report()
    vs.check_sections(s, report)
    vs.check_code_fences(s, report)
    return report


def _sections(report):
    return [(e["code"], e.get("section")) for e in report.errors]


class TestMarkdownStructure(unittest.TestCase):
    """Section checks read Markdown structure, not raw lines (docs/SKILL-SPEC.md limitation 6)."""

    def test_heading_inside_any_fence_is_not_a_section(self):
        for fence in ("```", "```markdown", "~~~", "~~~ md title=x", "````", "  ```"):
            close = fence.strip().split()[0]
            with self.subTest(fence=fence):
                body = f"\n## Overview\nx\n\n## Key Concepts\n- k\n\n{fence}\n## Gotchas\n- a\n- b\n- c\n{close}\n" \
                       "\n## References\n- [a](https://x.com) - r\n- [b](https://y.com) - r\n"
                self.assertIn(("missing-section", "Gotchas"), _sections(_body_report(body)))

    def test_longer_fence_contains_shorter_fence(self):
        # A ```` fence is closed only by ```` (or longer); the inner ``` lines are content.
        body = ("\n## Overview\nx\n\n## Key Concepts\n````markdown\n```python\n## Gotchas\n```\n````\n" + GOOD_TAIL)
        report = _body_report(body)
        self.assertEqual(report.errors, [])
        self.assertEqual(make_skill("demo", body=body).code_blocks(None),
                         [("markdown", "```python\n## Gotchas\n```\n")])

    def test_bullets_inside_code_or_comments_do_not_count(self):
        body = ("\n## Overview\nx\n\n## Key Concepts\n- k\n\n## Gotchas\n- a\n```yaml\n- b\n- c\n```\n"
                "<!--\n- d\n-->\n\n## References\n- [a](https://x.com) - r\n- [b](https://y.com) - r\n")
        [e] = [e for e in _body_report(body).errors if e["code"] == "too-few-gotchas"]
        self.assertEqual(e["found"], 1)

    def test_heading_in_fence_does_not_end_a_section(self):
        body = ("\n## Overview\nx\n\n## Key Concepts\n- k\n\n## Gotchas\n- a\n```bash\n## not a heading\n```\n- b\n- c\n"
                "\n## References\n- [a](https://x.com) - r\n- [b](https://y.com) - r\n")
        self.assertEqual(_body_report(body).errors, [])

    def test_atx_heading_variants(self):
        body = ("\n## Overview ##\nx\n\n   ## Key Concepts\n- k\n\n##\tGotchas   #####\n- a\n- b\n- c\n"
                "\n## References\n- [a](https://x.com) - r\n- [b](https://y.com) - r\n")
        self.assertEqual(_body_report(body).errors, [])
        s = make_skill("demo", body="## C#\n##No space\n    ## indented code\n## #\n## Real\n")
        self.assertEqual(s.headers(), ["C#", "", "Real"])

    def test_multiline_html_comment_hides_headings(self):
        body = ("\n## Overview\nx\n\n## Key Concepts\n- k\n<!--\n## Workflow\n-->\n" + GOOD_TAIL)
        self.assertEqual(_body_report(body).errors, [])
        self.assertNotIn("Workflow", make_skill("demo", body=body).headers())

    def test_setext_headings_are_rejected(self):
        for underline, level in (("---", 2), ("===", 1), ("-", 2), ("  ===  ", 1)):
            with self.subTest(underline=underline):
                body = f"\n## Overview\nx\n\n## Key Concepts\n- k\n\nGotchas\n{underline}\n- a\n" + GOOD_TAIL
                report = _body_report(body)
                [e] = [e for e in report.errors if e["code"] == "setext-heading"]
                self.assertEqual((e["heading"], e["level"]), ("Gotchas", level))
                s = make_skill("demo", body=body)
                self.assertEqual(e["line"], s.body_line_offset() + body.split("\n").index("Gotchas") + 2)

    def test_rules_that_are_not_setext_headings(self):
        # A `---` after a blank line, a list item, a table or a heading is a thematic break.
        for before in ("text\n\n", "- item\n", "- item\n  continued\n", "| a | b |\n", "## Key Concepts\n",
                       "> quote\n", "1. step\n", "<div>\n", "```\ncode\n```\n"):
            with self.subTest(before=before):
                body = f"\n## Overview\nx\n\n## Key Concepts\n- k\n\n{before}---\n" + GOOD_TAIL
                self.assertNotIn("setext-heading", [e["code"] for e in _body_report(body).errors])

    def test_unclosed_fence_detection(self):
        for body, unclosed in (("```python\nx = 1\n```\n", False), ("~~~\nx\n~~~\n", False),
                               ("```\nx\n", True), ("~~~\nx\n```\n", True), ("````\nx\n```\n", True),
                               ("````\nx\n`````\n", False), ("Use ``` fences in prose, or ```x```.\n", False),
                               ("```python\nx\n```python\n", True)):
            with self.subTest(body=body):
                report = _body_report("\n## Overview\n" + body + "\n## Key Concepts\n- k\n" + GOOD_TAIL)
                found = [e for e in report.errors if e["code"] == "unbalanced-fence"]
                self.assertEqual(bool(found), unclosed)
                if found:
                    self.assertEqual(found[0]["line"], make_skill("demo").body_line_offset() + 3)

    def test_numbered_steps_in_code_do_not_count(self):
        body = ("\n## Overview\nx\n\n## Workflow\n1. **Step** - do it.\n```text\n2. **not a step**\n3. **nor this**\n```\n"
                + GOOD_TAIL)
        self.assertEqual([w for w in _body_report(body, "workflow").warnings
                          if w["code"] == "fewer-code-blocks-than-steps"], [])


class TestCapabilityNearDuplicates(unittest.TestCase):
    NEAR = [("fine-tune", "finetune", "separator"), ("text-embedding", "text-embeddings", "plural"),
            ("retrieval-query", "retrieval-queries", "plural"), ("model-evaluation", "evaluation-model", "token-order"),
            ("ml-drift-detection", "drift-detection", "generic-prefix"),
            ("llm-prompt-caching", "prompt-caching", "generic-prefix"),
            ("hyperparamter-tuning", "hyperparameter-tuning", "typo"),
            ("tokenizer-training", "tokeniser-training", "typo")]
    DISTINCT = [("class-imbalance-check", "class-imbalance-handling"), ("data-quality-gating", "model-quality-gating"),
                ("gradient-descent-math", "gradient-descent-variants"), ("gan-training", "gnn-training"),
                ("lora-fine-tuning", "qlora-fine-tuning"), ("ml-security", "security"),
                ("data-drift-detection", "drift-detection"), ("text-embedding", "text-embedding"),
                ("bias-audit", "bias-audits-report")]

    def test_reason_table(self):
        for a, b, reason in self.NEAR:
            with self.subTest(a=a, b=b):
                self.assertEqual(vs.near_duplicate_reason(a, b), reason)
                self.assertEqual(vs.near_duplicate_reason(b, a), reason)  # symmetric
        for a, b in self.DISTINCT:
            with self.subTest(a=a, b=b):
                self.assertIsNone(vs.near_duplicate_reason(a, b))
                self.assertIsNone(vs.near_duplicate_reason(b, a))

    def test_warning_names_both_skills_deterministically(self):
        def run():
            skills = [make_skill("skill-b", capabilities=["text-embeddings"]),
                      make_skill("skill-a", capabilities=["text-embedding", "rag-chunking"]),
                      make_skill("skill-c", capabilities=["text-embedding"])]
            report = vs.Report()
            vs.check_relationships(skills, report)
            return [w for w in report.warnings if w["code"] == "capability-near-duplicate"]
        first = run()
        self.assertEqual(first, run())
        got = sorted((w["skill"], w["value"], w["near"], tuple(w["shared_with"])) for w in first)
        self.assertEqual(got, [("skill-a", "text-embedding", "text-embeddings", ("skill-b",)),
                               ("skill-b", "text-embeddings", "text-embedding", ("skill-a", "skill-c")),
                               ("skill-c", "text-embedding", "text-embeddings", ("skill-b",))])
        self.assertTrue(all(w["reason"] == "plural" and w["field"] == "capabilities" for w in first))

    def test_both_spellings_in_one_skill_is_not_a_collision(self):
        report = vs.Report()
        vs.check_relationships([make_skill("skill-a", capabilities=["text-embedding", "text-embeddings"])], report)
        self.assertEqual(report.warnings, [])

    def test_real_skills_have_no_near_duplicates(self):
        # Checked by hand on 2026-10-29: the closest real pairs share one word and are
        # distinct capabilities (see DISTINCT above). A new hit needs a decision, not a waiver.
        warnings = [w for w in vs.run(REPO_ROOT).warnings if w["code"] == "capability-near-duplicate"]
        self.assertEqual(warnings, [])


class TestRelationships(unittest.TestCase):
    def test_unknown_related_slug_flagged(self):
        s = make_skill("demo", related=["does-not-exist"])
        report = vs.Report()
        vs.check_relationships([s], report)
        self.assertTrue(any("unknown skill" in r for r in rules(report)))

    def test_duplicate_name_flagged(self):
        a = make_skill("skill-a", name="same-name")
        b = make_skill("skill-b", name="same-name")
        report = vs.Report()
        vs.check_relationships([a, b], report)
        self.assertIn("duplicate skill name", rules(report))

    def test_self_reference_and_overlap(self):
        a = make_skill("skill-a", requires=["skill-a", "skill-b"], conflicts=["skill-b"])
        b = make_skill("skill-b", conflicts=["skill-a"])
        report = vs.Report()
        vs.check_relationships([a, b], report)
        found = codes(report.errors)
        self.assertIn(("self-reference", "requires"), found)
        self.assertIn(("requires-conflicts-overlap", "conflicts"), found)
        self.assertEqual([w for w in report.warnings if w["code"] == "conflicts-asymmetric"], [])

    def test_non_list_relationship_values_do_not_crash(self):
        s = make_skill("demo", related="abc")
        report = vs.Report()
        vs.check_relationships([s], report)  # the type error is check_frontmatter's job
        self.assertEqual(report.errors, [])


class TestReportContract(unittest.TestCase):
    def test_every_finding_has_code_file_and_fix(self):
        s = make_skill("Bad_Slug", name="claude", type="nope", related=["missing"], last_verified="2026-13-01")
        report = vs.Report()
        vs.check_frontmatter(s, report)
        vs.check_relationships([s], report)
        self.assertTrue(report.errors)
        for e in report.errors + report.warnings:
            self.assertIn(e["code"], vs.RULES)
            self.assertEqual(e["file"], "Bad_Slug/SKILL.md")
            self.assertTrue(e["fix"])
            self.assertEqual(e["rule"], vs.RULES[e["code"]][1])

    def test_every_rule_is_documented_in_skill_spec(self):
        spec = (REPO_ROOT / "docs" / "SKILL-SPEC.md").read_text(encoding="utf-8")
        missing = [code for code in vs.RULES if f"`{code}`" not in spec]
        self.assertEqual(missing, [], "add these rule codes to docs/SKILL-SPEC.md 'Validation rules'")
        for code, (severity, _rule, _fix) in vs.RULES.items():
            row = re.search(rf"^\|\s*`{re.escape(code)}`\s*\|\s*(\w+)", spec, flags=re.MULTILINE)
            self.assertIsNotNone(row, f"{code} needs a table row in docs/SKILL-SPEC.md")
            self.assertEqual(row.group(1).lower(), severity, f"severity of {code} differs between code and docs")

    def test_spec_frontmatter_example_passes_frontmatter_checks(self):
        # People copy this example; it must not trip the rules it documents
        # (its `<X>`-style placeholders once failed description-xml-tag).
        spec = (REPO_ROOT / "docs" / "SKILL-SPEC.md").read_text(encoding="utf-8")
        block = re.search(r"^## Frontmatter contract\s*```yaml\n(.*?)```", spec, re.S | re.M).group(1)
        parsed = sl.parse_frontmatter_with_diagnostics(block.replace("\n---\n", "\n"))
        self.assertEqual(parsed.diagnostics, [])
        s = sl.Skill(slug="kebab-case-name", path=REPO_ROOT / "kebab-case-name" / "SKILL.md",
                     frontmatter=parsed.data, body="", raw="", key_lines=parsed.key_lines)
        report = vs.Report()
        vs.check_frontmatter(s, report, today=dt.date(2026, 10, 27))
        self.assertEqual(codes(report.errors + report.warnings), [])

    def test_every_yaml_diagnostic_has_a_fix(self):
        source = (REPO_ROOT / "scripts" / "skills_lib.py").read_text(encoding="utf-8")
        emitted = set(re.findall(r'self\.diag\([^,]+,\s*"([a-z-]+)"', source)) | {"tab-character"}
        self.assertEqual(emitted - set(vs.YAML_FIXES), set())


class TestRepository(unittest.TestCase):
    def test_repository_has_zero_errors(self):
        report = vs.run(REPO_ROOT)
        self.assertEqual(report.errors, [], "\n".join(f"{e['code']} {e['file']} {e.get('field', '')}"
                                                      for e in report.errors))

    def test_json_output_shape(self):
        buf = io.StringIO()
        with redirect_stdout(buf):
            exit_code = vs.main(["--json", "--root", str(REPO_ROOT)])
        payload = json.loads(buf.getvalue())
        self.assertEqual(set(payload), {"errors", "warnings"})
        self.assertEqual(exit_code, 0 if not payload["errors"] else 1)


if __name__ == "__main__":
    unittest.main()
