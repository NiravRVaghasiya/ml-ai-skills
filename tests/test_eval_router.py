"""Tests for scripts/eval_router.py and the labelled routing benchmark.

Three layers:
  1. metric math on synthetic fake routers (no dependency on the real router),
  2. well-formedness + coverage of evals/routing/benchmark.json,
  3. a regression floor: the current scripts/router.py must not score worse
     than evals/routing/baseline.json on any headline metric.
"""
import contextlib
import copy
import io
import json
import shutil
import tempfile
import unittest
from dataclasses import dataclass, field
from pathlib import Path

import testutil  # noqa: F401  (inserts scripts/ and schema/ onto sys.path as a side effect)
import eval_router as er
import skills_lib as sl

REPO_ROOT = testutil.REPO_ROOT


@dataclass
class FakeHit:
    slug: str
    score: float = 1.0
    reasons: list = field(default_factory=lambda: ["fake"])


def fake_router(table: dict):
    """route(task, top_k) returning pre-baked hits per query; top_k is ignored
    so tests control the exact selection (including prerequisite extras)."""
    def route(task, top_k=5):
        return "workflow", [h if isinstance(h, FakeHit) else FakeHit(h) for h in table.get(task, [])]
    return route


def case(cid, category, expected=(), acceptable=(), forbidden=(), split="dev"):
    return {"id": cid, "query": cid, "category": category, "split": split, "expected": list(expected),
            "acceptable": list(acceptable), "forbidden": list(forbidden), "notes": ""}


class TestMetricMath(unittest.TestCase):
    def setUp(self):
        self.cases = [
            case("A", "typical", expected=["a"], acceptable=["b"]),
            case("B", "no-skill"),
            case("C", "no-skill", split="test"),
            case("D", "conflicting", expected=["a"], forbidden=["d"], split="test"),
        ]
        self.route = fake_router({"A": ["a", "b", "c"], "B": [], "C": ["x"], "D": ["d", "a"]})

    def test_hand_computed_metrics(self):
        m = er.evaluate(self.cases, self.route, top_k=5)["splits"]["all"]
        self.assertEqual(m["n_cases"], 4)
        self.assertEqual(m["precision"], 0.5)          # (a,b from A + a from D) / 6 selected
        self.assertEqual(m["recall"], 1.0)             # both expected 'a's selected
        self.assertEqual(m["top1_accuracy"], 0.5)      # A first='a' ok; D first='d' miss
        self.assertEqual(m["task_accuracy"], 0.5)      # A, B pass; C activated; D forbidden
        self.assertEqual(m["false_activation_rate"], 0.5)
        self.assertEqual(m["mean_unnecessary_activations"], 0.75)  # c, x, d over 4 cases
        self.assertEqual(m["forbidden_selection_rate"], 1.0)
        self.assertEqual(m["mean_selected"], 1.5)

    def test_per_split_and_category(self):
        rep = er.evaluate(self.cases, self.route, top_k=5)
        self.assertEqual(rep["splits"]["dev"]["n_cases"], 2)
        self.assertEqual(rep["splits"]["test"]["n_cases"], 2)
        self.assertEqual(rep["splits"]["test"]["task_accuracy"], 0.0)
        self.assertEqual(rep["categories"]["no-skill"]["false_activation_rate"], 0.5)
        self.assertEqual(rep["categories"]["paraphrase"]["n_cases"], 0)
        self.assertIsNone(rep["categories"]["paraphrase"]["precision"])

    def test_split_filter_only_evaluates_that_split(self):
        rep = er.evaluate(self.cases, self.route, top_k=5, split="test")
        self.assertEqual(list(rep["splits"]), ["test"])
        self.assertEqual({r["id"] for r in rep["cases"]}, {"C", "D"})

    def test_misrouted_ids_and_reasons(self):
        mis = {m["id"]: m["reasons"] for m in er.evaluate(self.cases, self.route)["misrouted"]}
        self.assertEqual(set(mis), {"C", "D"})
        self.assertTrue(any("forbidden" in r for r in mis["D"]))

    def test_prerequisite_hits_count_as_selected(self):
        cases = [case("P", "typical", expected=["dep"])]
        route = fake_router({"P": [FakeHit("base", 0.0, ["pulled in as a hard prerequisite"]), FakeHit("dep")]})
        m = er.evaluate(cases, route)["splits"]["all"]
        self.assertEqual(m["counts"]["selected"], 2)
        self.assertEqual(m["precision"], 0.5)      # 'base' not labelled -> unnecessary
        self.assertEqual(m["top1_accuracy"], 1.0)  # a leading prerequisite is skipped for top-1

    def test_top1_skips_scored_prerequisite_of_selected_dependent(self):
        # The real router orders a prerequisite before its dependent even when
        # the prerequisite earned its slot by score (no 'pulled in' reason), so
        # top-1 must use the declared `requires` map, not just reasons.
        reqs = {"dep": ["base"]}
        cases = [case("S1", "typical", expected=["dep"], acceptable=["base"]),
                 case("S2", "typical", expected=["dep"], acceptable=["base"]),
                 case("S3", "typical", expected=["base"], acceptable=["dep"]),
                 case("S4", "typical", expected=["dep"])]
        route = fake_router({"S1": ["base", "dep"],          # prerequisite first -> look past it
                             "S2": ["base", "other", "dep"],  # first non-prerequisite is wrong
                             "S3": ["base", "dep"],          # prerequisite itself expected
                             "S4": ["base"]})                # 'base' not a prereq of anything selected
        rep = er.evaluate(cases, route, requires=reqs)
        self.assertEqual(rep["splits"]["all"]["counts"]["top1_correct"], 2)
        by_id = {r["id"]: r for r in rep["cases"]}
        self.assertEqual(by_id["S1"]["prerequisites"], ["base"])
        self.assertEqual(by_id["S4"]["prerequisites"], [])

    def test_is_top1_correct_edge_cases(self):
        self.assertFalse(er.is_top1_correct([], ["a"]))
        self.assertFalse(er.is_top1_correct(["p"], ["a"], ["p"]))  # only prerequisites selected
        self.assertTrue(er.is_top1_correct(["p", "q", "a"], ["a"], ["p", "q"]))

    def test_top_k_below_one_rejected(self):
        for bad in (0, -1):
            with self.assertRaises(ValueError):
                er.evaluate(self.cases, self.route, top_k=bad)

    def test_empty_expected_with_acceptable(self):
        cases = [case("Q", "adversarial", acceptable=["ok"], forbidden=["bad"]),
                 case("R", "adversarial", acceptable=["ok"], forbidden=["bad"])]
        route = fake_router({"Q": ["ok"], "R": ["other"]})
        rep = er.evaluate(cases, route)
        self.assertEqual(rep["splits"]["all"]["task_accuracy"], 0.5)
        self.assertEqual(rep["splits"]["all"]["forbidden_selection_rate"], 0.0)
        self.assertIsNone(rep["splits"]["all"]["top1_accuracy"])   # no case has expected skills
        self.assertIsNone(rep["splits"]["all"]["recall"])

    def test_duplicate_hits_deduplicated(self):
        self.assertEqual(er.selected_slugs([FakeHit("a"), FakeHit("b"), FakeHit("a")]), ["a", "b"])

    def test_router_selecting_nothing(self):
        m = er.evaluate(self.cases, fake_router({}))["splits"]["all"]
        self.assertIsNone(m["precision"])
        self.assertEqual(m["recall"], 0.0)
        self.assertEqual(m["false_activation_rate"], 0.0)
        self.assertEqual(m["task_accuracy"], 0.5)

    def test_deterministic(self):
        a = er.evaluate(self.cases, self.route)
        b = er.evaluate(self.cases, self.route)
        self.assertEqual(json.dumps(a, sort_keys=True), json.dumps(b, sort_keys=True))


class TestThresholds(unittest.TestCase):
    METRICS = {"precision": 0.5, "recall": 0.9, "top1_accuracy": None, "task_accuracy": 0.6,
               "false_activation_rate": 0.2, "mean_unnecessary_activations": 1.0, "forbidden_selection_rate": 0.1}

    def test_pass(self):
        self.assertEqual(er.check_thresholds(self.METRICS, {"min_precision": 0.5, "max_false_activation": 0.2}), [])

    def test_min_and_max_failures(self):
        fails = er.check_thresholds(self.METRICS, {"min_task_accuracy": 0.7, "max_forbidden_rate": 0.05})
        self.assertEqual(len(fails), 2)

    def test_undefined_metric_fails_threshold(self):
        self.assertEqual(len(er.check_thresholds(self.METRICS, {"min_top1": 0.1})), 1)


class TestCli(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        # A router that never selects anything: recall 0, no false activations.
        self.null_router = self.tmp / "null_router.py"
        self.null_router.write_text("def route(task, top_k=5):\n    return 'workflow', []\n", encoding="utf-8")

    def run_main(self, *argv):
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
            try:
                code = er.main(list(argv))
            except SystemExit as exc:  # argparse usage errors
                code = exc.code
        return code, out.getvalue()

    def test_json_output_and_threshold_exit_code(self):
        code, out = self.run_main("--router", str(self.null_router), "--json", "--max-false-activation", "0")
        self.assertEqual(code, 0)
        data = json.loads(out)
        self.assertEqual(data["splits"]["all"]["false_activation_rate"], 0.0)
        self.assertNotIn("cases", data)
        code, _ = self.run_main("--router", str(self.null_router), "--min-recall", "0.5")
        self.assertEqual(code, 1)

    def test_split_flag(self):
        _, out = self.run_main("--router", str(self.null_router), "--json", "--split", "dev")
        self.assertEqual(list(json.loads(out)["splits"]), ["dev"])

    def test_malformed_benchmark_exit_2(self):
        bench = er.load_benchmark()
        bench["cases"].append(copy.deepcopy(bench["cases"][0]))  # duplicate id
        bad = self.tmp / "bad.json"
        bad.write_text(json.dumps(bench), encoding="utf-8")
        code, _ = self.run_main("--benchmark", str(bad), "--router", str(self.null_router))
        self.assertEqual(code, 2)

    def test_structurally_malformed_benchmark_exit_2(self):
        # Must be 2 (benchmark broken), never 1 (router regressed) or a traceback.
        variants = {"truncated": '{"version": 1, "cases": [', "list root": "[1, 2]",
                    "non-object case": '{"version": 1, "cases": [5]}', "empty cases": '{"version": 1, "cases": []}'}
        for label, text in variants.items():
            bad = self.tmp / "bad.json"
            bad.write_text(text, encoding="utf-8")
            with self.subTest(label):
                self.assertEqual(self.run_main("--benchmark", str(bad), "--router", str(self.null_router))[0], 2)
        missing = self.tmp / "does-not-exist.json"
        self.assertEqual(self.run_main("--benchmark", str(missing), "--router", str(self.null_router))[0], 2)

    def test_top_k_must_be_positive(self):
        for bad in ("0", "-1", "x"):
            with self.subTest(bad):
                self.assertEqual(self.run_main("--router", str(self.null_router), "--top-k", bad)[0], 2)
        code, out = self.run_main("--router", str(self.null_router), "--top-k", "3", "--json")
        self.assertEqual((code, json.loads(out)["top_k"]), (0, 3))

    def test_write_baseline(self):
        target = self.tmp / "baseline.json"
        code, _ = self.run_main("--router", str(self.null_router), "--top-k", "3", "--write-baseline",
                                str(target), "--baseline-date", "2026-01-02", "--baseline-note", "null router")
        self.assertEqual(code, 0)
        base = json.loads(target.read_text(encoding="utf-8"))
        self.assertEqual(set(base), {"note", "date", "command", "top_k", "benchmark_sha256", "metrics"})
        self.assertEqual((base["date"], base["note"], base["top_k"]), ("2026-01-02", "null router", 3))
        self.assertEqual(base["benchmark_sha256"], er.benchmark_sha256())
        self.assertIn("--write-baseline", base["command"])
        self.assertEqual(set(base["metrics"]), {"dev", "test", "all"})
        for split in base["metrics"].values():
            self.assertEqual(set(split), set(er.HEADLINE_METRICS))
        self.assertEqual(base["metrics"]["all"]["recall"], 0.0)

    def test_human_table_lists_misrouted(self):
        _, out = self.run_main("--router", str(self.null_router))
        self.assertIn("misrouted cases", out)
        self.assertIn("typ-001", out)  # null router misses every expected skill


class TestBenchmarkWellFormed(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.bench = er.load_benchmark()
        cls.cases = cls.bench["cases"]
        cls.skills = sl.load_all_skills()
        cls.slugs = {s.slug for s in cls.skills}

    def test_validator_clean(self):
        self.assertEqual(er.validate_benchmark(self.bench, self.slugs, er.requires_map(self.skills)), [])
        self.assertEqual(er.coverage_errors(self.bench, self.slugs), [])

    def test_unique_ids(self):
        ids = [c["id"] for c in self.cases]
        self.assertEqual(len(ids), len(set(ids)))

    def test_categories_and_splits_valid(self):
        for c in self.cases:
            self.assertIn(c["category"], er.CATEGORIES, c["id"])
            self.assertIn(c["split"], er.SPLITS, c["id"])

    def test_all_slugs_are_skill_folders(self):
        for c in self.cases:
            for key in er.LABEL_KEYS:
                for slug in c[key]:
                    self.assertTrue((REPO_ROOT / slug / "SKILL.md").is_file(), f"{c['id']}: {slug}")

    def test_expected_and_forbidden_disjoint(self):
        for c in self.cases:
            self.assertFalse(set(c["expected"]) & set(c["forbidden"]), c["id"])

    def test_coverage_requirements(self):
        self.assertGreaterEqual(len(self.cases), 150)
        for cat in er.CATEGORIES:
            self.assertGreaterEqual(sum(c["category"] == cat for c in self.cases), 15, cat)
        for slug in self.slugs:
            self.assertGreaterEqual(sum(slug in c["expected"] for c in self.cases), 2, slug)
        test_frac = sum(c["split"] == "test" for c in self.cases) / len(self.cases)
        self.assertTrue(0.35 <= test_frac <= 0.45, test_frac)
        for split in er.SPLITS:  # the held-out split must exercise every skill too
            for slug in self.slugs:
                self.assertTrue(any(c["split"] == split and slug in c["expected"] for c in self.cases),
                                f"{slug} never expected in {split}")

    def test_coverage_errors_flags_split_gaps_and_bad_structure(self):
        bench = copy.deepcopy(self.bench)
        for c in bench["cases"]:
            if c["split"] == "test" and "pytorch-patterns" in c["expected"]:
                c["split"] = "dev"
        errs = er.coverage_errors(bench, self.slugs)
        self.assertTrue(any("'pytorch-patterns' is expected in 0 test" in e for e in errs), errs)
        for broken in ([1, 2], {"cases": [5]}, {"cases": None}):
            self.assertTrue(er.coverage_errors(broken, self.slugs))

    def test_no_skill_cases_expect_nothing(self):
        for c in self.cases:
            if c["category"] == "no-skill":
                self.assertEqual((c["expected"], c["acceptable"]), ([], []), c["id"])

    def test_validator_catches_bad_cases(self):
        good = case("x-001", "typical", expected=["rag-evaluation"], acceptable=["rag-pipeline"])
        bad_variants = {
            "unknown skill": dict(good, expected=["no-such-skill"]),
            "overlap": dict(good, forbidden=["rag-evaluation"]),
            "bad category": dict(good, category="misc"),
            "bad split": dict(good, split="train"),
            "no-skill with expected": dict(good, category="no-skill"),
            "forbidden prerequisite": dict(good, acceptable=[], forbidden=["rag-pipeline"]),
            "unaccepted prerequisite": dict(good, acceptable=[]),
            "acceptable dependent, prerequisite unlabelled": dict(good, expected=["llm-evaluation"],
                                                                  acceptable=["rag-evaluation"]),
            "acceptable dependent, prerequisite forbidden": dict(good, expected=["llm-evaluation"],
                                                                 acceptable=["rag-evaluation"],
                                                                 forbidden=["rag-pipeline"]),
            "multi-skill with one": dict(good, category="multi-skill"),
            "extra key": dict(good, oops=1),
        }
        reqs = {"rag-evaluation": ["rag-pipeline"]}
        self.assertEqual(er.validate_benchmark({"version": 1, "cases": [good]}, self.slugs, reqs), [])
        for label, variant in bad_variants.items():
            errs = er.validate_benchmark({"version": 1, "cases": [variant]}, self.slugs, reqs)
            self.assertTrue(errs, label)
        dup = er.validate_benchmark({"version": 1, "cases": [good, dict(good)]}, self.slugs, reqs)
        self.assertTrue(any("duplicate" in e for e in dup))


class TestRegressionFloor(unittest.TestCase):
    """The current router must not regress below the recorded baseline.

    baseline.json's top-level entry is the current floor (the post-improvement
    router); earlier baselines are kept under "history" (oldest first, the
    pre-improvement keyword router at index 0). No tolerance: routing is
    deterministic, so any drop - from router code OR from SKILL.md metadata
    edits, since the router scores names/capabilities/descriptions/Overviews -
    is a real change in selections. If you intentionally change the benchmark
    labels, the router, or skill metadata in a way that moves the numbers,
    re-run the `command` stored in baseline.json to re-baseline, move the old
    top-level entry into "history" (--write-baseline does not preserve it),
    and say why in the commit."""

    @classmethod
    def setUpClass(cls):
        cls.baseline = json.loads(er.BASELINE_PATH.read_text(encoding="utf-8"))

    def test_history_keeps_pre_improvement_baseline(self):
        history = self.baseline.get("history")
        self.assertIsInstance(history, list)
        self.assertTrue(history, "history must keep the pre-improvement baseline")
        self.assertIn("PRE-IMPROVEMENT", history[0]["note"])
        for entry in history:
            self.assertEqual(set(entry["metrics"]), {"dev", "test", "all"})

    def test_current_floor_not_below_pre_improvement(self):
        # A re-baseline may trade metrics off deliberately, but the recorded
        # floor must never be worse than the original keyword router overall.
        pre = self.baseline["history"][0]["metrics"]["all"]
        cur = self.baseline["metrics"]["all"]
        for metric in er.HEADLINE_METRICS:
            with self.subTest(metric=metric):
                if metric in er.HIGHER_IS_BETTER:
                    self.assertGreaterEqual(cur[metric], pre[metric])
                else:
                    self.assertLessEqual(cur[metric], pre[metric])

    def test_current_router_meets_baseline(self):
        baseline = self.baseline
        self.assertEqual(er.benchmark_sha256(), baseline["benchmark_sha256"],
                         "benchmark.json changed since baseline.json was recorded; re-baseline deliberately")
        bench = er.load_benchmark()
        report = er.evaluate(bench["cases"], er.load_route_fn(), top_k=baseline["top_k"])
        for split, floor in baseline["metrics"].items():
            got = report["splits"][split]
            for metric, base in floor.items():
                with self.subTest(split=split, metric=metric):
                    self.assertIsNotNone(got[metric])
                    if metric in er.HIGHER_IS_BETTER:
                        self.assertGreaterEqual(got[metric], base)
                    else:
                        self.assertLessEqual(got[metric], base)


if __name__ == "__main__":
    unittest.main()
