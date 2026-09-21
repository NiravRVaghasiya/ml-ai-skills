import unittest

from testutil import make_skill
import validate_skills as vs


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
        rules = [e["rule"] for e in report.errors]
        self.assertIn("missing required metadata", rules)

    def test_invalid_enum_flagged(self):
        s = make_skill("demo", type="not-a-real-type")
        report = vs.Report()
        vs.check_frontmatter(s, report)
        rules = [e["rule"] for e in report.errors]
        self.assertIn("invalid metadata value", rules)

    def test_name_mismatch_flagged(self):
        s = make_skill("demo", name="something-else")
        report = vs.Report()
        vs.check_frontmatter(s, report)
        rules = [e["rule"] for e in report.errors]
        self.assertIn("name != folder name", rules)

    def test_empty_list_field_is_not_a_missing_field(self):
        # `requires:` with no items parses to None, representing "no hard
        # dependencies" — that's valid, not a missing-field error.
        s = make_skill("demo", requires=None, conflicts=None)
        report = vs.Report()
        vs.check_frontmatter(s, report)
        rules_with_fields = [(e["rule"], e.get("field")) for e in report.errors]
        self.assertNotIn(("missing required metadata", "requires"), rules_with_fields)
        self.assertNotIn(("missing required metadata", "conflicts"), rules_with_fields)

    def test_truly_absent_list_field_is_flagged(self):
        s = make_skill("demo")
        del s.frontmatter["capabilities"]
        report = vs.Report()
        vs.check_frontmatter(s, report)
        rules_with_fields = [(e["rule"], e.get("field")) for e in report.errors]
        self.assertIn(("missing required metadata", "capabilities"), rules_with_fields)

    def test_missing_not_for_clause_flagged(self):
        s = make_skill("demo", description="Use when the user wants to do X. Trigger phrases: \"a\", \"b\", \"c\".")
        report = vs.Report()
        vs.check_frontmatter(s, report)
        rules = [e["rule"] for e in report.errors]
        self.assertIn("description missing 'NOT for ...' clause", rules)


class TestSectionChecks(unittest.TestCase):
    def test_workflow_skill_requires_workflow_section(self):
        body = "\n## Overview\nx\n\n## Gotchas\n- a\n- b\n- c\n\n## References\n- [a](https://x.com) - r\n- [b](https://y.com) - r\n"
        s = make_skill("demo", body=body, type="workflow")
        report = vs.Report()
        vs.check_sections(s, report)
        rules = [e["rule"] for e in report.errors]
        self.assertIn("workflow skill missing ## Workflow section", rules)

    def test_reference_skill_rejects_workflow_section(self):
        s = make_skill("demo", type="reference")  # default body has ## Workflow
        report = vs.Report()
        vs.check_sections(s, report)
        rules = [e["rule"] for e in report.errors]
        self.assertTrue(any("must not have ## Workflow" in r for r in rules))

    def test_too_few_gotchas_flagged(self):
        body = "\n## Overview\nx\n\n## Workflow\n1. **s** - d\n```python\npass\n```\n\n## Gotchas\n- only one\n\n## References\n- [a](https://x.com) - r\n- [b](https://y.com) - r\n"
        s = make_skill("demo", body=body)
        report = vs.Report()
        vs.check_sections(s, report)
        rules = [e["rule"] for e in report.errors]
        self.assertIn("fewer than 3 gotchas", rules)


class TestRelationships(unittest.TestCase):
    def test_unknown_related_slug_flagged(self):
        s = make_skill("demo", related=["does-not-exist"])
        report = vs.Report()
        vs.check_relationships([s], report)
        rules = [e["rule"] for e in report.errors]
        self.assertTrue(any("unknown skill" in r for r in rules))

    def test_duplicate_name_flagged(self):
        a = make_skill("skill-a", name="same-name")
        b = make_skill("skill-b", name="same-name")
        report = vs.Report()
        vs.check_relationships([a, b], report)
        rules = [e["rule"] for e in report.errors]
        self.assertIn("duplicate skill name", rules)


if __name__ == "__main__":
    unittest.main()
