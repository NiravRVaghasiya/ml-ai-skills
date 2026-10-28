"""Routing regressions: requests the pre-improvement keyword router
(scripts/router.py at git 8328c60) got wrong, asserted against the real skill
library so a future router or SKILL.md metadata change cannot silently bring
them back.

Two groups:
  * hand-written requests reported in Phase 1 (README / docs/ROUTING.md
    examples, off-topic requests, known paraphrase and vocabulary-collision
    misses), with the expected behaviour spelled out here;
  * benchmark dev-split cases the old router misrouted and the current router
    routes correctly. Their labels live in evals/routing/benchmark.json and
    are judged with the benchmark's own task-level rule
    (eval_router.case_outcome), so this file never duplicates a label.

Only dev-split ids are listed: the router was tuned on dev, and the held-out
test split must stay a measurement, not a target.
"""
import unittest

import testutil  # noqa: F401  (inserts scripts/ and schema/ onto sys.path as a side effect)
import eval_router as er
import router


def slugs(task, **kw):
    return [h.slug for h in router.route(task, **kw)[1]]


class TestPhase1Regressions(unittest.TestCase):
    def test_off_topic_requests_select_nothing(self):
        # Old router: 5-7 skills for every off-topic request (false-activation rate 1.0).
        for task in ("bake a chocolate cake", "what's the weather today",
                     "Book me a flight from Seattle to Boston next Tuesday."):
            with self.subTest(task=task):
                res = router.route_details(task)
                self.assertEqual(res.hits, [])
                self.assertIsNotNone(res.fallback_reason)

    def test_reference_trigger_phrase_beats_default_workflow_intent(self):
        # Old router: the default 'workflow' intent bonus pushed the reference
        # skill whose trigger phrase this is out of the top hits.
        self.assertEqual(slugs("is this difference statistically significant")[:1], ["statistics-for-ml"])

    def test_structured_output_prompting(self):
        self.assertEqual(slugs("get JSON output from the model")[:1], ["prompt-engineering"])

    def test_class_imbalance_evaluation_not_object_detection(self):
        # docs/ROUTING.md's documented collision: "fraud detection" vs computer-vision's
        # "object detection". Old router ranked computer-vision first.
        for task in ("class imbalance fraud detection model evaluation",
                     "Build a fraud detection model and evaluate it under severe class imbalance"):
            with self.subTest(task=task):
                got = slugs(task)
                self.assertIn(got[0], {"model-evaluation", "supervised-learning", "ml-problem-framing"})
                self.assertIn("model-evaluation", got)
                self.assertIn("supervised-learning", got)
                self.assertNotIn("computer-vision", got)

    def test_readme_example(self):
        # README's example: old router also returned training-deep-models and
        # fine-tuning-llms (any-word capability matches on "training").
        got = slugs("handle missing values and encode categorical features before training")
        self.assertEqual(got, ["data-preprocessing"])

    def test_negated_sibling_is_dropped(self):
        got = slugs("I don't want to fine-tune or retrain anything. Just improve my prompt so the model "
                    "answers in our house format.")
        self.assertEqual(got[:1], ["prompt-engineering"])
        self.assertNotIn("fine-tuning-llms", got)

    def test_prerequisite_not_pulled_in_by_unrelated_hit(self):
        # Old router prepended rag-pipeline / agents-and-tools at score 0 whenever
        # their dependents scraped into the top 5 on generic words.
        for task in ("Add mixed precision, a warmup-plus-cosine learning rate scheduler and gradient clipping "
                     "to my training loop.",
                     "Forecast next quarter's sales with SARIMA and check whether the series is stationary."):
            with self.subTest(task=task):
                got = slugs(task)
                self.assertNotIn("rag-pipeline", got)
                self.assertNotIn("agents-and-tools", got)


class TestNegationScopeRegressions(unittest.TestCase):
    """Review findings after the first rewrite: symptom descriptions ("is not
    training", "never predicts", "isn't learning") and avoided problems
    ("without leakage") were treated as exclusions, so their key terms were
    negated and the requests routed to nothing or to the wrong skills."""

    def check(self, task, allowed_top, required=()):
        res = router.route_details(task)
        got = [h.slug for h in res.hits]
        self.assertTrue(got, f"{task!r}: {res.fallback_reason}")
        self.assertIn(got[0], allowed_top, got)
        for slug in required:
            self.assertIn(slug, got)

    def test_symptom_descriptions_route(self):
        self.check("my network is not training", {"neural-net-fundamentals", "training-deep-models"})
        self.check("Why isn't my LSTM learning?", {"rnn-sequence"})
        self.check("My RAG chatbot is not retrieving the right documents", {"rag-pipeline", "rag-evaluation"})
        self.check("my gradient boosting model is not calibrated", {"model-evaluation", "supervised-learning",
                                                                    "model-optimization"}, ["model-evaluation"])
        self.check("My classifier never predicts the minority class",
                   {"supervised-learning", "model-evaluation", "ml-problem-framing"})

    def test_no_idea_is_not_an_exclusion(self):
        self.check("I have no idea how to tune xgboost hyperparameters", {"hyperparameter-tuning"})

    def test_avoided_problem_routes_to_its_owner(self):
        self.check("How do I split my data without leakage?", {"data-preprocessing", "model-evaluation"})
        self.check("Evaluate my classifier without data leakage between train and test",
                   {"data-preprocessing", "model-evaluation", "supervised-learning"})


# Dev-split cases misrouted by the pre-improvement router (55 of 118) that the
# current router routes correctly (45 of them). Measured 2026-10-27 with
# eval_router.evaluate on --split dev; the other 10, plus 5 dev cases the old
# router got right and this one gets wrong, are listed in docs/ROUTING.md.
# mul-010, par-020, par-028 and par-035 pass only because of synonym-map
# entries that occur in exactly one dev query (dev-fitted; see
# docs/ROUTING.md "Synonym map"): pruning those entries is a legitimate
# change that should drop these ids here and re-baseline, not be "fixed" by
# adding more per-query vocabulary.
FIXED_DEV_CASES = (
    "adv-001", "adv-003", "adv-011", "adv-013", "adv-015", "adv-018", "adv-023", "amb-005", "amb-006",
    "amb-016", "con-001", "con-003", "con-005", "con-006", "con-011", "con-013", "con-016", "con-018",
    "con-020", "con-021", "mul-010", "mul-015", "non-001", "non-003", "non-005", "non-006", "non-008",
    "non-010", "non-011", "non-013", "non-015", "non-016", "non-020", "non-021", "par-001", "par-003",
    "par-005", "par-018", "par-020", "par-021", "par-023", "par-028", "par-031", "par-033", "par-035",
)


class TestFixedDevCases(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cases = {c["id"]: c for c in er.load_benchmark()["cases"]}
        cls.cases = [cases[cid] for cid in FIXED_DEV_CASES]
        cls.results = {r["id"]: r for r in er.run_cases(cls.cases, router.route, top_k=5)}

    def test_ids_are_dev_split(self):
        self.assertEqual({c["split"] for c in self.cases}, {"dev"})

    def test_previously_misrouted_cases_route_correctly(self):
        for cid in FIXED_DEV_CASES:
            ok, reasons = er.case_outcome(self.results[cid])
            with self.subTest(case=cid):
                self.assertTrue(ok, f"{cid}: {reasons}; selected={self.results[cid]['selected']}")


if __name__ == "__main__":
    unittest.main()
