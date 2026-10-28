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
