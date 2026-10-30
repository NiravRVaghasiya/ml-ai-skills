"""Self-tests for the integration opt-in gate (stdlib only; always runs)."""
from __future__ import annotations

import os
import unittest
from unittest import mock

from integration import gate


def _run(cls) -> unittest.TestResult:
    result = unittest.TestResult()
    unittest.defaultTestLoader.loadTestsFromTestCase(cls).run(result)
    return result


class TestOptInGate(unittest.TestCase):
    # opt_in() reads the environment when it decorates, so each class is built inside its patch.

    def test_unset_flag_skips_with_a_reason_naming_the_variable(self):
        with mock.patch.dict(os.environ, {gate.flag("SELFTEST"): ""}):
            @gate.opt_in("SELFTEST", what="gate self-test")
            class T(unittest.TestCase):
                def test_x(self):
                    pass
        result = _run(T)
        self.assertEqual(len(result.skipped), 1)
        self.assertIn("ML_AI_SKILLS_INTEGRATION_SELFTEST=1", result.skipped[0][1])

    def test_opted_in_missing_module_or_binary_fails_instead_of_skipping(self):
        for kwargs in ({"modules": ["definitely_not_a_real_module_xyz"]},
                       {"modules": ["definitely_not_a_real_pkg_xyz.sub"]},
                       {"binaries": ["definitely-not-a-real-binary-xyz"]},
                       {"precondition": lambda: "no CUDA device"}):
            with self.subTest(kwargs=kwargs), mock.patch.dict(os.environ, {gate.flag("SELFTEST"): "1"}):
                @gate.opt_in("SELFTEST", what="gate self-test", **kwargs)
                class T(unittest.TestCase):
                    def test_x(self):
                        pass
                result = _run(T)
                self.assertEqual(result.skipped, [])
                self.assertEqual(len(result.errors) + len(result.failures), 1)
                self.assertIn("ML_AI_SKILLS_INTEGRATION_SELFTEST=1 but", str(result.errors + result.failures))

    def test_opted_in_runs_and_keeps_the_class_setup(self):
        calls = []
        with mock.patch.dict(os.environ, {gate.flag("SELFTEST"): "1"}):
            @gate.opt_in("SELFTEST", what="gate self-test", modules=["os"], precondition=lambda: None)
            class T(unittest.TestCase):
                @classmethod
                def setUpClass(cls):
                    calls.append(cls.__name__)

                def test_x(self):
                    calls.append("test")
        result = _run(T)
        self.assertTrue(result.wasSuccessful(), result.errors)
        self.assertEqual(calls, ["T", "test"])

    def test_block_index_requires_exactly_one_match(self):
        self.assertEqual(gate.block_index("nlp-tasks", 'model="dslim/bert-base-NER"'),
                         [i for i, b in enumerate(gate.harness.python_blocks("nlp-tasks"))
                          if "dslim/bert-base-NER" in b][0])
        with self.assertRaises(AssertionError):
            gate.block_index("nlp-tasks", "import")          # in several blocks
        with self.assertRaises(AssertionError):
            gate.block_index("nlp-tasks", "no such text in any block xyz")


if __name__ == "__main__":
    unittest.main()
