import unittest

from testutil import sl


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
        text = """
notes: |
  line one
  line two
"""
        fm = sl.parse_frontmatter(text)
        self.assertEqual(fm["notes"], "line one\nline two")


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


if __name__ == "__main__":
    unittest.main()
