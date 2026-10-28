"""Unit tests for scripts/router.py: normalisation, task analysis, scoring,
selection/gating, the CLI, and determinism. Routing of real requests against
the real skills is in tests/test_router_regressions.py; aggregate quality is
gated by tests/test_eval_router.py::TestRegressionFloor."""
import json
import os
import subprocess
import sys
import unittest

import testutil  # noqa: F401  (inserts scripts/ and schema/ onto sys.path as a side effect)
from testutil import make_skill
import router

ROUTER = str(testutil.REPO_ROOT / "scripts" / "router.py")


class TestClassifyIntent(unittest.TestCase):
    def test_reference_question(self):
        self.assertEqual(router.classify_intent("What is calibration?"), "reference")
        self.assertEqual(router.classify_intent("Explain how self-attention works"), "reference")

    def test_workflow_request(self):
        self.assertEqual(router.classify_intent("Build a fraud detection model and evaluate it"), "workflow")
        self.assertEqual(router.classify_intent("Fine-tune a model on my support tickets"), "workflow")

    def test_default_is_workflow(self):
        self.assertEqual(router.classify_intent("model calibration"), "workflow")

    def test_cues_match_whole_words(self):
        # "train" inside "constraint" and "ship" inside "relationship" are not cues.
        self.assertEqual(router._intent_counts("explain the constraint relationship"), (1, 0))

    def test_no_cue_is_not_explicit(self):
        self.assertFalse(router.analyze_task("model calibration").explicit_intent)
        self.assertTrue(router.analyze_task("What is calibration?").explicit_intent)


class TestPorterStemmer(unittest.TestCase):
    # Examples from M.F. Porter (1980), "An algorithm for suffix stripping".
    PAPER = {
        "caresses": "caress", "ponies": "poni", "cats": "cat", "feed": "feed", "agreed": "agre",
        "plastered": "plaster", "motoring": "motor", "sing": "sing", "conflated": "conflat",
        "troubled": "troubl", "sized": "size", "hopping": "hop", "tanned": "tan", "falling": "fall",
        "hissing": "hiss", "fizzed": "fizz", "failing": "fail", "filing": "file", "happy": "happi",
        "relational": "relat", "conditional": "condit", "rational": "ration", "digitizer": "digit",
        "vietnamization": "vietnam", "predication": "predic", "operator": "oper", "feudalism": "feudal",
        "decisiveness": "decis", "hopefulness": "hope", "callousness": "callous", "triplicate": "triplic",
        "formative": "form", "formalize": "formal", "electrical": "electr", "hopeful": "hope",
        "goodness": "good", "revival": "reviv", "allowance": "allow", "inference": "infer",
        "airliner": "airlin", "gyroscopic": "gyroscop", "adjustable": "adjust", "defensible": "defens",
        "irritant": "irrit", "replacement": "replac", "adjustment": "adjust", "dependent": "depend",
        "adoption": "adopt", "communism": "commun", "activate": "activ", "homologous": "homolog",
        "effective": "effect", "bowdlerize": "bowdler", "probate": "probat", "rate": "rate",
        "cease": "ceas", "controll": "control", "roll": "roll", "generalizations": "gener",
        "oscillators": "oscil",
    }

    def test_paper_examples(self):
        for word, want in self.PAPER.items():
            with self.subTest(word=word):
                self.assertEqual(router.porter_stem(word), want)

    def test_ml_word_families_share_a_stem(self):
        families = [
            ("classify", "classifier", "classification"), ("imbalance", "imbalanced"),
            ("calibrate", "calibration", "calibrated"), ("detect", "detection", "detector"),
            ("tune", "tuning", "tuned"), ("cluster", "clusters", "clustering"),
            ("explain", "explanation"), ("anonymise", "anonymize"), ("optimisation", "optimization"),
            ("bias", "biased"),
        ]
        for fam in families:
            with self.subTest(family=fam):
                self.assertEqual(len({router.stem(w) for w in fam}), 1, [router.stem(w) for w in fam])

    def test_unrelated_words_kept_apart(self):
        self.assertNotEqual(router.stem("customer"), router.stem("custom"))
        self.assertNotEqual(router.stem("general"), router.stem("generation"))


class TestTokenize(unittest.TestCase):
    def test_stopwords_dropped_and_stemmed(self):
        self.assertEqual(router.tokenize("How do I tune the models?"), {"tune", "model"})

    def test_letter_compounds_and_ab(self):
        toks = router.tokenize("Explain Q-learning, k-means and A/B tests; recall@k")
        self.assertTrue({"qlearn", "kmean", "ab"} <= toks)
        self.assertNotIn("k", toks)
        self.assertNotIn("q", toks)


class TestAnalyzeTask(unittest.TestCase):
    def test_negated_clause(self):
        t = router.analyze_task("Train a detector. I don't need a lecture on convolution theory.")
        self.assertIn("convolut", t.negated)
        self.assertNotIn("convolut", t.weights)
        self.assertIn("detect", t.weights)

    def test_already_done_clause(self):
        t = router.analyze_task("My data is already cleaned and imputed; now I need new features.")
        self.assertTrue({"clean", "imput"} <= t.negated)
        self.assertIn("featur", t.weights)

    def test_contrastive_not_after_comma(self):
        t = router.analyze_task("This is a privacy task, not a bias question.")
        self.assertIn("bia", t.negated)

    def test_state_negation_is_not_exclusion(self):
        # A negated state or symptom is the problem to debug, not an unwanted skill.
        cases = {
            "why is my network not training": {"train"},
            "my network is not training": {"train"},  # the docs/ROUTING.md example
            "Why isn't my LSTM learning?": {"lstm", "learn"},
            "My RAG chatbot is not retrieving the right documents": {"retriev", "document"},
            "the validation loss is not improving after epoch 3": {"epoch", "improv"},
            "my gradient boosting model is not calibrated": {"calibr"},
            "My classifier never predicts the minority class": {"predict", "minor", "class"},
            "Positive reinforcement with treats isn't working": {"work"},
        }
        for text, stems in cases.items():
            with self.subTest(text=text):
                t = router.analyze_task(text)
                self.assertEqual(t.negated, set())
                self.assertTrue(stems <= set(t.weights), sorted(t.weights))

    def test_negative_looking_phrases_that_exclude_nothing(self):
        for text in ("I have no idea how to tune xgboost hyperparameters", "I'm not sure how to tune xgboost",
                     "I don't know how to tune xgboost", "I've had no luck tuning xgboost"):
            with self.subTest(text=text):
                t = router.analyze_task(text)
                self.assertEqual(t.negated, set())
                self.assertIn("tune", t.weights)

    def test_avoided_problem_is_the_topic(self):
        # "without leakage" names the problem to prevent - the skill's own topic.
        for text in ("How do I split my data without leakage?",
                     "Evaluate my classifier without data leakage between train and test",
                     "Train a churn classifier with no data leakage",
                     "fine-tune without overfitting"):
            with self.subTest(text=text):
                t = router.analyze_task(text)
                self.assertEqual(t.negated, set())
                self.assertTrue({"leakag"} <= set(t.weights) or "overfit" in t.weights)

    def test_request_negation_still_excludes(self):
        cases = {
            "Explain this one decision. This is not a fairness audit across groups.": {"fair", "audit"},
            "Use PCA; this is not feature selection for a supervised model.": {"featur", "select"},
            "This isn't a one-off cleanup of a CSV: run it on a schedule.": {"cleanup", "csv"},
            "Score my retriever. I'm not asking about agent tool use.": {"agent", "tool"},
            "We are not evaluating an existing agent yet.": {"evalu", "agent"},
            "my model is not a transformer": {"transform"},
            "No neural networks, no transformers: just k-means": {"neural", "network", "transform"},
            "Improve my prompt without fine-tuning": {"fine", "tune"},
            "I do not want to retrain anything": {"retrain"},
        }
        for text, stems in cases.items():
            with self.subTest(text=text):
                t = router.analyze_task(text)
                self.assertTrue(stems <= t.negated, sorted(t.negated))

    def test_word_in_both_scopes_stays_positive(self):
        t = router.analyze_task("No deep learning here, just learning a k-means model")
        self.assertIn("learn", t.weights)
        self.assertNotIn("learn", t.negated)

    def test_synonym_expansion_is_down_weighted(self):
        t = router.analyze_task("half the cells are blank")
        self.assertEqual(t.weights.get("miss"), router.SYNONYM_WEIGHT)
        self.assertIn("blank", t.synonyms_used)

    def test_anchor_tiers(self):
        self.assertTrue(router.analyze_task("tune my xgboost").has_anchor)          # strong anchor
        self.assertTrue(router.analyze_task("train a model").has_anchor)            # two weak anchors
        self.assertFalse(router.analyze_task("I'm building a model railway").has_anchor)  # one weak
        self.assertFalse(router.analyze_task("bake a chocolate cake").has_anchor)


class TestScoring(unittest.TestCase):
    def setUp(self):
        self.strong = make_skill("strong-fit", capabilities=["fraud-detection", "class-imbalance"])
        self.weak = make_skill("weak-fit", capabilities=["time-series-forecasting"])
        self.index = router.build_index([self.strong, self.weak])

    def test_capability_match_outscores_no_match(self):
        task = "build a fraud detection model under class imbalance"
        s_strong = router.score_skill(self.strong, task, index=self.index)
        s_weak = router.score_skill(self.weak, task, index=self.index)
        self.assertGreater(s_strong.score, s_weak.score)
        self.assertTrue(any("capability match: fraud-detection, class-imbalance" in r for r in s_strong.reasons))

    def test_token_set_input_still_supported(self):
        tokens = router.tokenize("build a fraud detection model under class imbalance")
        self.assertGreater(router.score_skill(self.strong, tokens, "workflow", index=self.index).score,
                           router.score_skill(self.weak, tokens, "workflow", index=self.index).score)

    def test_single_word_of_multiword_tag_is_not_a_tag_match(self):
        hit = router.score_skill(self.strong, "fraud", index=self.index)
        self.assertFalse(any(r.startswith("capability match") for r in hit.reasons))

    def test_type_match_bonus_needs_evidence(self):
        wf = make_skill("wf-skill", type="workflow", capabilities=["widget-assembly"])
        ref = make_skill("ref-skill", type="reference", capabilities=["widget-assembly"])
        idx = router.build_index([wf, ref])
        self.assertGreater(router.score_skill(wf, "build the widget assembly", index=idx).score,
                           router.score_skill(ref, "build the widget assembly", index=idx).score)
        # No lexical evidence -> no intent bonus at all (the old router gave +2 regardless).
        self.assertEqual(router.score_skill(wf, "build a birdhouse", index=idx).score, 0.0)

    def test_idf_downweights_shared_words(self):
        skills = [make_skill(f"s{i}", capabilities=["common-word", f"unique{i}-word"]) for i in range(5)]
        idx = router.build_index(skills)
        self.assertGreater(idx.idf["unique0"], idx.idf["common"])

    def test_not_for_clause_is_negative_evidence(self):
        a = make_skill("alpha", capabilities=["shared-topic"],
                       description='Use for shared topic. Trigger phrases: "x y". NOT for gizmo work (see beta).')
        b = make_skill("beta", capabilities=["shared-topic"],
                       description='Use for shared topic. Trigger phrases: "x y". NOT for other things (see alpha).')
        idx = router.build_index([a, b])
        ha = router.score_skill(a, "shared topic gizmo", index=idx)
        hb = router.score_skill(b, "shared topic gizmo", index=idx)
        self.assertLess(ha.score, hb.score)
        self.assertTrue(any("NOT-for" in r for r in ha.reasons))

    def test_negated_terms_cost_the_skill(self):
        cnn = make_skill("cnn-ish", capabilities=["convolution-arithmetic"])
        idx = router.build_index([cnn, self.weak])
        plain = router.score_skill(cnn, "convolution arithmetic", index=idx).score
        negated = router.score_skill(cnn, "I don't need convolution arithmetic", index=idx).score
        self.assertGreater(plain, 0)
        self.assertLess(negated, 0)


class TestExpandWithRequires(unittest.TestCase):
    def test_prerequisite_prepended(self):
        base = make_skill("base", requires=[])
        dependent = make_skill("dependent", requires=["base"])
        by_slug = {"base": base, "dependent": dependent}
        expanded = router.expand_with_requires(by_slug, ["dependent"])
        self.assertEqual(expanded, ["base", "dependent"])


class TestRouteGating(unittest.TestCase):
    """Uses the real skills; checks the gating contract, not specific labels."""

    def test_off_topic_returns_nothing_with_reason(self):
        res = router.route_details("Plan a three-day itinerary for Kyoto")
        self.assertEqual(res.hits, [])
        self.assertEqual(res.confidence, 0.0)
        self.assertTrue(res.fallback_reason and res.fallback_reason.startswith("low confidence"))
        self.assertEqual(router.route("Plan a three-day itinerary for Kyoto")[1], [])

    def test_hits_clear_threshold_and_relative_or_residual_bar(self):
        res = router.route_details("Deploy the model as an API and then monitor it for data drift once it's live.")
        self.assertGreaterEqual(len(res.hits), 2)
        top = res.hits[0].score
        for h in res.hits:
            self.assertGreaterEqual(h.score, res.threshold)
            self.assertTrue(h.score >= router.RELATIVE_CUTOFF * top or any("residual" in r for r in h.reasons))
            self.assertGreater(h.confidence, 0.5)
            self.assertLess(h.confidence, 1.0)

    def test_top_k_caps_selection(self):
        self.assertLessEqual(len(router.route("Build a RAG pipeline over our wiki and then measure retrieval "
                                              "recall@k and answer faithfulness, then fine-tune", top_k=1)[1]), 2)

    def test_prerequisite_only_with_selected_dependent(self):
        _, hits = router.route("Compute recall@k and MRR for my retriever and check citation correctness.")
        slugs = [h.slug for h in hits]
        self.assertIn("rag-evaluation", slugs)
        self.assertIn("rag-pipeline", slugs)
        self.assertLess(slugs.index("rag-pipeline"), slugs.index("rag-evaluation"))
        # nothing selected -> no prerequisite pulled in on its own
        self.assertEqual(router.route("What's the capital of Australia?")[1], [])

    def test_route_hits_keep_public_fields(self):
        _, hits = router.route("explain attention")
        self.assertTrue(hits)
        for h in hits:
            for attr in ("slug", "score", "reasons", "type", "domain", "confidence"):
                self.assertTrue(hasattr(h, attr), attr)

    def test_min_score_override(self):
        self.assertEqual(router.route("explain attention", min_score=1e9)[1], [])


class TestCli(unittest.TestCase):
    def run_cli(self, *args, seed="0"):
        env = dict(os.environ, PYTHONHASHSEED=seed, PYTHONIOENCODING="utf-8")
        return subprocess.run([sys.executable, ROUTER, *args], capture_output=True, text=True, env=env,
                              encoding="utf-8", timeout=60)

    def test_json_has_confidence_and_fallback(self):
        out = json.loads(self.run_cli("--json", "explain attention").stdout)
        self.assertEqual(out["route"][0]["slug"], "attention-mechanisms")
        self.assertIsNone(out["fallback_reason"])
        self.assertGreater(out["confidence"], 0.5)
        for key in ("slug", "score", "confidence", "type", "domain", "reasons"):
            self.assertIn(key, out["route"][0])
        off = json.loads(self.run_cli("--json", "bake a chocolate cake").stdout)
        self.assertEqual((off["route"], off["confidence"]), ([], 0.0))
        self.assertTrue(off["fallback_reason"])

    def test_text_output(self):
        res = self.run_cli("bake a chocolate cake")
        self.assertEqual(res.returncode, 0)
        self.assertIn("no skill selected", res.stdout)
        res = self.run_cli("handle missing values and encode categorical features before training")
        self.assertIn("data-preprocessing", res.stdout)
        self.assertIn("confidence:", res.stdout)

    def test_deterministic_across_hash_seeds(self):
        queries = ["Build a fraud detection model and evaluate it under severe class imbalance",
                   "I don't want to fine-tune anything; just improve my prompt so it returns JSON",
                   "Our RAG chatbot will index customer emails: strip PII and defend against prompt injection.",
                   "bake a chocolate cake"]
        for q in queries:
            outs = []
            for seed in ("0", "1", "4242"):
                res = self.run_cli("--json", q, seed=seed)
                with self.subTest(q=q, seed=seed):
                    # A crash would give identical (empty) stdout for every seed.
                    self.assertEqual(res.returncode, 0, res.stderr)
                    self.assertEqual(json.loads(res.stdout)["task"], q)
                outs.append(res.stdout)
            with self.subTest(q=q):
                self.assertEqual(len(set(outs)), 1)


if __name__ == "__main__":
    unittest.main()
