"""Self-tests for the example harness (stdlib only — always runs)."""
from __future__ import annotations

import os
import unittest
from unittest import mock

from examples import harness


class TestHarness(unittest.TestCase):
    def test_extracts_blocks_from_canonical_skill(self):
        blocks = harness.python_blocks("data-preprocessing")
        self.assertGreater(len(blocks), 0)

    def test_illustrative_marker(self):
        self.assertTrue(harness.is_illustrative("\n# illustrative: needs an API key\nx = 1\n"))
        self.assertFalse(harness.is_illustrative("x = 1\n# illustrative\n"))

    def test_replacement_must_match_exactly_once(self):
        with self.assertRaises(AssertionError):
            harness._apply_replacements("a = 1\na = 1\n", {"a = 1": "a = 2"}, "x")
        with self.assertRaises(AssertionError):
            harness._apply_replacements("a = 1\n", {"b = 1": "b = 2"}, "x")
        self.assertEqual(harness._apply_replacements("a = 1\n", {"a = 1": "a = 2"}, "x"), "a = 2\n")

    def test_out_of_range_block_index_errors(self):
        with self.assertRaises(AssertionError):
            harness.run_blocks("data-preprocessing", [999])

    def test_requires_skips_or_fails_on_missing_module(self):
        # requires() reads the env var when the decorator is applied, so each
        # variant is decorated inside its own patched environment.
        with mock.patch.dict(os.environ, {harness.REQUIRE_ENV: "0"}):
            @harness.requires("definitely_not_a_real_module_xyz")
            class T(unittest.TestCase):
                def test_x(self):
                    pass

        result = unittest.TestResult()
        unittest.defaultTestLoader.loadTestsFromTestCase(T).run(result)
        self.assertEqual(len(result.skipped), 1)

        with mock.patch.dict(os.environ, {harness.REQUIRE_ENV: "1"}):
            @harness.requires("definitely_not_a_real_module_xyz")
            class Strict(unittest.TestCase):
                def test_x(self):
                    pass
        strict_result = unittest.TestResult()
        unittest.defaultTestLoader.loadTestsFromTestCase(Strict).run(strict_result)
        self.assertEqual(len(strict_result.skipped), 0)
        self.assertGreaterEqual(len(strict_result.errors) + len(strict_result.failures), 1)


if __name__ == "__main__":
    unittest.main()
