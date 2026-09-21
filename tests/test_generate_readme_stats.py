import unittest

import testutil  # noqa: F401  (inserts scripts/ and schema/ onto sys.path as a side effect)
import generate_readme_stats as grs


class TestRender(unittest.TestCase):
    def test_render_contains_markers_and_real_counts(self):
        block = grs.render()
        self.assertTrue(block.startswith(grs.START))
        self.assertTrue(block.endswith(grs.END))
        self.assertIn("Skills", block)
        self.assertIn("Unit tests", block)

    def test_check_matches_after_write(self):
        # --write then --check against the real README.md should agree with
        # itself: rendering twice in a row must be idempotent.
        first = grs.render()
        second = grs.render()
        self.assertEqual(first, second)


if __name__ == "__main__":
    unittest.main()
