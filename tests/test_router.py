import unittest

import testutil  # noqa: F401  (inserts scripts/ and schema/ onto sys.path as a side effect)
import router


class TestClassifyIntent(unittest.TestCase):
    def test_reference_question(self):
        self.assertEqual(router.classify_intent("What is calibration?"), "reference")
        self.assertEqual(router.classify_intent("Explain how self-attention works"), "reference")

    def test_workflow_request(self):
        self.assertEqual(router.classify_intent("Build a fraud detection model and evaluate it"), "workflow")
        self.assertEqual(router.classify_intent("Fine-tune a model on my support tickets"), "workflow")

    def test_default_is_workflow(self):
        self.assertEqual(router.classify_intent("model calibration"), "workflow")


class TestScoring(unittest.TestCase):
    def test_capability_match_outscores_no_match(self):
        from testutil import make_skill
        strong = make_skill("strong-fit", capabilities=["fraud detection", "class imbalance"])
        weak = make_skill("weak-fit", capabilities=["time series forecasting"])
        tokens = router.tokenize("build a fraud detection model under class imbalance")
        s_strong = router.score_skill(strong, tokens, "workflow")
        s_weak = router.score_skill(weak, tokens, "workflow")
        self.assertGreater(s_strong.score, s_weak.score)

    def test_type_mismatch_penalized(self):
        from testutil import make_skill
        wf = make_skill("wf-skill", type="workflow", capabilities=["thing"])
        ref = make_skill("ref-skill", type="reference", capabilities=["thing"])
        tokens = router.tokenize("thing")
        s_wf = router.score_skill(wf, tokens, "workflow")
        s_ref = router.score_skill(ref, tokens, "workflow")
        self.assertGreater(s_wf.score, s_ref.score)


class TestExpandWithRequires(unittest.TestCase):
    def test_prerequisite_prepended(self):
        from testutil import make_skill
        base = make_skill("base", requires=[])
        dependent = make_skill("dependent", requires=["base"])
        by_slug = {"base": base, "dependent": dependent}
        expanded = router.expand_with_requires(by_slug, ["dependent"])
        self.assertEqual(expanded, ["base", "dependent"])


if __name__ == "__main__":
    unittest.main()
