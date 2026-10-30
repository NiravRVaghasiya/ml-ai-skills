"""Tests for scripts/eval_router.py and the labelled routing benchmark.

Four layers:
  1. metric math on synthetic fake routers (no dependency on the real router),
     including the all-negatives false activation, abstention, no-skill
     precision/recall, Wilson intervals, per-skill metrics and the confusion matrix;
  2. the CLI, on a synthetic benchmark that meets every rule (synthetic_benchmark);
  3. well-formedness, coverage, family and label-provenance rules of
     evals/routing/benchmark.json;
  4. a regression floor: the current scripts/router.py must not score worse
     than evals/routing/baseline.json on any headline metric.
The CI gate (run_gate / resolve_base) is tested in tests/test_router_gate.py.
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


PREFIX = {"typical": "typ", "paraphrase": "par", "ambiguous": "amb", "multi-skill": "mul", "no-skill": "non",
          "conflicting": "con", "adversarial": "adv"}


def synthetic_benchmark(skills) -> dict:
    """A benchmark over `skills` that passes every validate/coverage/split-integrity
    rule, so CLI and gate tests do not depend on the real benchmark's current state
    (which the coverage rules can legitimately reject mid-change). Queries are the
    case ids; use it with a fake or null router."""
    slugs = sorted(s.slug for s in skills)
    requires = er.requires_map(skills)
    cases: list[dict] = []

    def add(category, split, expected=(), forbidden=()):
        cid = f"{PREFIX[category]}-{sum(c['category'] == category for c in cases) + 1:03d}"
        prereqs = sorted({p for s in expected for p in er.prerequisite_closure(s, requires)} - set(expected))
        cases.append({"id": cid, "query": f"synthetic case {cid}", "category": category, "split": split,
                      "family": cid, "expected": list(expected), "acceptable": prereqs, "forbidden": list(forbidden),
                      "notes": "", "labellers": ["fixture"], "adjudication": "single-labeller"})

    for split in er.SPLITS:  # every skill expected once per split
        for slug in slugs:
            add("typical", split, [slug])
    for i in range(15):
        split = "test" if i % 5 in (1, 3) else "dev"
        a, b = slugs[i % len(slugs)], slugs[(i + 1) % len(slugs)]
        for category in ("paraphrase", "ambiguous"):
            add(category, split, [a])
        add("multi-skill", split, [a, b] if b not in er.prerequisite_closure(a, requires) else [a, slugs[i + 3]])
        add("no-skill", split)
        add("adversarial", split)
        banned = {a, *er.prerequisite_closure(a, requires)}
        add("conflicting", split, [a], [next(s for s in reversed(slugs) if s not in banned)])
    return {"version": 1, "description": "synthetic", "cases": cases}


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

    def test_negatives_abstention_and_no_skill_metrics(self):
        m = er.evaluate(self.cases, self.route)["splits"]["all"]
        # negatives = B, C (empty expected); C selected 'x' -> 1 of 2 activated
        self.assertEqual(m["negative_false_activation_rate"], 0.5)
        self.assertEqual(m["abstention_rate"], 0.25)            # only B selected nothing
        self.assertEqual(m["positive_abstention_rate"], 0.0)    # A and D both selected something
        self.assertEqual(m["no_skill_precision"], 1.0)          # the one abstention (B) was right
        self.assertEqual(m["no_skill_recall"], 0.5)             # 1 of 2 negatives abstained
        self.assertEqual(m["n_families"], 4)
        self.assertEqual(m["intervals"]["task_accuracy"], {"k": 2, "n": 4, "low": 0.15, "high": 0.85})

    def test_negative_false_activation_counts_every_category_and_respects_acceptable(self):
        # The no-skill-only rate misses adversarial cases; the all-negatives rate does not,
        # and selecting a tolerated (acceptable) skill is not an activation.
        cases = [case("N", "no-skill"), case("H", "adversarial", forbidden=["bad"]),
                 case("T", "adversarial", acceptable=["ok"]), case("P", "typical", expected=["a"])]
        route = fake_router({"N": [], "H": ["bad"], "T": ["ok"], "P": []})
        m = er.evaluate(cases, route)["splits"]["all"]
        self.assertEqual(m["false_activation_rate"], 0.0)
        self.assertEqual(m["negative_false_activation_rate"], round(1 / 3, 4))
        self.assertEqual(m["positive_abstention_rate"], 1.0)
        self.assertEqual(m["counts"]["negative_cases"], 3)

    def test_wilson_interval_reference_values(self):
        # Standard Wilson score values (z = 1.96): 0/10 -> [0, 0.2775], 5/10 -> [0.2366, 0.7634].
        self.assertEqual(er.wilson_interval(0, 10), (0.0, 0.2775))
        self.assertEqual(er.wilson_interval(10, 10), (0.7225, 1.0))
        self.assertEqual(er.wilson_interval(5, 10), (0.2366, 0.7634))
        self.assertEqual(er.wilson_interval(0, 0), (None, None))

    def test_every_proportion_has_an_interval(self):
        m = er.evaluate(self.cases, self.route)["splits"]["all"]
        self.assertEqual(set(m["intervals"]), set(er.PROPORTIONS))
        for metric, iv in m["intervals"].items():
            with self.subTest(metric=metric):
                self.assertEqual(m[metric], er._ratio(iv["k"], iv["n"]))

    def test_per_class_and_confusion(self):
        rep = er.evaluate(self.cases, self.route)
        pc = rep["per_class"]["all"]
        self.assertEqual((pc["a"]["support"], pc["a"]["recall"], pc["a"]["precision"]), (2, 1.0, 1.0))
        self.assertEqual((pc["d"]["selected"], pc["d"]["precision"], pc["d"]["forbidden_selected"]), (1, 0.0, 1))
        self.assertIsNone(pc["d"]["recall"])  # never expected
        conf = rep["confusion"]["all"]
        self.assertEqual(conf["a"], {"a": 2, "b": 1, "c": 1, "d": 1})
        self.assertEqual(conf[er.NONE_LABEL], {er.NONE_LABEL: 1, "x": 1})
        top = {(c["expected"], c["selected"]): c["ids"] for c in rep["top_confusions"]["all"]}
        self.assertEqual(top, {("a", "c"): ["A"], ("a", "d"): ["D"], (er.NONE_LABEL, "x"): ["C"]})
        missed = er.top_confusions([{"id": "M", "expected": ["a"], "acceptable": [], "forbidden": [],
                                     "selected": []}])
        self.assertEqual(missed, [{"expected": "a", "selected": er.NONE_LABEL, "count": 1, "ids": ["M"]}])

    def test_split_category_breakdown(self):
        rep = er.evaluate(self.cases, self.route)
        self.assertEqual(rep["split_categories"]["test"]["no-skill"]["n_cases"], 1)
        self.assertEqual(rep["split_categories"]["dev"]["typical"]["task_accuracy"], 1.0)
        self.assertNotIn("all", rep["split_categories"])


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

    def test_negative_false_activation_is_gated(self):
        # The gate and the regression floor must catch a recall gain bought with
        # activations on off-topic text, not only on the no-skill category.
        self.assertIn("negative_false_activation_rate", er.LOWER_IS_BETTER)
        self.assertEqual(er.THRESHOLD_FLAGS["max_negative_false_activation"], ("negative_false_activation_rate", "max"))
        fails = er.check_thresholds({"negative_false_activation_rate": 0.3}, {"max_negative_false_activation": 0.2})
        self.assertEqual(len(fails), 1)


class TestCli(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        # A router that never selects anything: recall 0, no false activations.
        self.null_router = self.tmp / "null_router.py"
        self.null_router.write_text("def route(task, top_k=5):\n    return 'workflow', []\n", encoding="utf-8")
        # CLI behaviour is tested on a synthetic benchmark that meets every rule, so a
        # coverage gap in the real benchmark (see TestBenchmarkWellFormed) is reported
        # there and does not turn these tests into exit-2 checks.
        self.bench = synthetic_benchmark(sl.load_all_skills())
        self.bench_path = self.tmp / "benchmark.json"
        self.bench_path.write_text(json.dumps(self.bench, indent=2) + "\n", encoding="utf-8")

    def run_main(self, *argv, benchmark=True):
        out = io.StringIO()
        argv = (["--benchmark", str(self.bench_path)] if benchmark else []) + list(argv)
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
            try:
                code = er.main(argv)
            except SystemExit as exc:  # argparse usage errors
                code = exc.code
        return code, out.getvalue()

    def test_synthetic_benchmark_is_valid(self):
        skills = sl.load_all_skills()
        valid = {s.slug for s in skills}
        self.assertEqual(er.validate_benchmark(self.bench, valid, er.requires_map(skills)), [])
        self.assertEqual(er.coverage_errors(self.bench, valid), [])
        self.assertEqual(er.split_integrity_errors(self.bench), [])

    def test_json_output_and_threshold_exit_code(self):
        code, out = self.run_main("--router", str(self.null_router), "--json", "--max-false-activation", "0",
                                  "--max-negative-false-activation", "0")
        self.assertEqual(code, 0)
        data = json.loads(out)
        self.assertEqual(data["splits"]["all"]["false_activation_rate"], 0.0)
        self.assertEqual(data["splits"]["all"]["negative_false_activation_rate"], 0.0)
        self.assertEqual(data["splits"]["all"]["abstention_rate"], 1.0)
        for key in ("per_class", "confusion", "top_confusions", "split_categories"):
            self.assertIn(key, data)
        self.assertNotIn("cases", data)
        code, _ = self.run_main("--router", str(self.null_router), "--min-recall", "0.5")
        self.assertEqual(code, 1)

    def test_split_flag(self):
        _, out = self.run_main("--router", str(self.null_router), "--json", "--split", "dev")
        data = json.loads(out)
        self.assertEqual(list(data["splits"]), ["dev"])
        self.assertEqual(list(data["per_class"]), ["dev"])

    def test_malformed_benchmark_exit_2(self):
        bench = copy.deepcopy(self.bench)
        bench["cases"].append(copy.deepcopy(bench["cases"][0]))  # duplicate id
        bad = self.tmp / "bad.json"
        bad.write_text(json.dumps(bench), encoding="utf-8")
        code, _ = self.run_main("--benchmark", str(bad), "--router", str(self.null_router), benchmark=False)
        self.assertEqual(code, 2)

    def test_family_spanning_splits_exit_2(self):
        bench = copy.deepcopy(self.bench)
        dev = next(c for c in bench["cases"] if c["split"] == "dev")
        test = next(c for c in bench["cases"] if c["split"] == "test")
        test["family"] = dev["family"]
        bad = self.tmp / "span.json"
        bad.write_text(json.dumps(bench), encoding="utf-8")
        code, _ = self.run_main("--benchmark", str(bad), "--router", str(self.null_router), benchmark=False)
        self.assertEqual(code, 2)

    def test_structurally_malformed_benchmark_exit_2(self):
        # Must be 2 (benchmark broken), never 1 (router regressed) or a traceback.
        variants = {"truncated": '{"version": 1, "cases": [', "list root": "[1, 2]",
                    "non-object case": '{"version": 1, "cases": [5]}', "empty cases": '{"version": 1, "cases": []}'}
        for label, text in variants.items():
            bad = self.tmp / "bad.json"
            bad.write_text(text, encoding="utf-8")
            with self.subTest(label):
                self.assertEqual(self.run_main("--benchmark", str(bad), "--router", str(self.null_router),
                                               benchmark=False)[0], 2)
        missing = self.tmp / "does-not-exist.json"
        self.assertEqual(self.run_main("--benchmark", str(missing), "--router", str(self.null_router),
                                       benchmark=False)[0], 2)

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
        self.assertEqual(base["benchmark_sha256"], er.benchmark_sha256(self.bench_path))
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

    def test_every_case_has_family_and_label_provenance(self):
        for c in self.cases:
            with self.subTest(case=c["id"]):
                for key in er.PROVENANCE_KEYS:
                    self.assertIn(key, c)
                self.assertIn(c["adjudication"], er.ADJUDICATION_STATUSES)

    def test_no_family_spans_dev_and_test(self):
        # A family with any dev (tuning) case is dev as a whole: paraphrases of a tuned
        # request are not held out. (Independent of the coverage rules, which the
        # re-split can break until new held-out families are added.)
        self.assertEqual(er.split_integrity_errors(self.bench), [])

    def test_labels_written_after_reading_the_router_are_dev_only(self):
        # labeller-b wrote cases for known router failure modes after reading the
        # router, so those labels are not blind and must never be held out.
        for c in self.cases:
            if "labeller-b" in c["labellers"]:
                self.assertEqual(c["split"], "dev", c["id"])

    def test_provenance_validation(self):
        good = dict(case("p-001", "typical", expected=["data-privacy"]), family="privacy-attack",
                    labellers=["labeller-a"], adjudication="single-labeller")
        review = {"reviewers": ["reviewer-1", "reviewer-2"], "agreement": "full", "unresolved": []}
        ok_variants = {
            "single labeller": good,
            "agreed": dict(good, adjudication="agreed", label_review=review),
            "adjudicated, numeric agreement": dict(good, adjudication="adjudicated",
                                                   label_review=dict(review, agreement=0.5)),
            "disputed": dict(good, adjudication="disputed",
                             label_review=dict(review, agreement="partial", unresolved=["expected"])),
            "older benchmark without the fields": case("p-001", "typical", expected=["data-privacy"]),
        }
        bad_variants = {
            "family not kebab": dict(good, family="Privacy Attack"),
            "family not a string": dict(good, family=3),
            "no labellers": dict(good, labellers=[]),
            "duplicate labellers": dict(good, labellers=["a", "a"]),
            "unknown adjudication": dict(good, adjudication="reviewed"),
            "agreed without review": dict(good, adjudication="agreed"),
            "review on a single-labeller case": dict(good, label_review=review),
            "review missing a key": dict(good, adjudication="agreed", label_review={"reviewers": ["r"]}),
            "review extra key": dict(good, adjudication="agreed", label_review=dict(review, kappa=0.4)),
            "no reviewers": dict(good, adjudication="agreed", label_review=dict(review, reviewers=[])),
            "agreement out of range": dict(good, adjudication="adjudicated", label_review=dict(review, agreement=2)),
            "agreement unknown word": dict(good, adjudication="adjudicated", label_review=dict(review, agreement="most")),
            "agreed but partial": dict(good, adjudication="agreed", label_review=dict(review, agreement="partial")),
            "unresolved but not disputed": dict(good, adjudication="adjudicated",
                                                label_review=dict(review, unresolved=["expected"])),
            "disputed with nothing unresolved": dict(good, adjudication="disputed", label_review=review),
        }
        for label, variant in ok_variants.items():
            with self.subTest(ok=label):
                self.assertEqual(er.validate_benchmark({"version": 1, "cases": [variant]}, self.slugs), [])
        for label, variant in bad_variants.items():
            with self.subTest(bad=label):
                self.assertTrue(er.validate_benchmark({"version": 1, "cases": [variant]}, self.slugs))

    def test_split_integrity_errors(self):
        a = dict(case("s-001", "typical", expected=["data-privacy"]), family="f", labellers=["l"],
                 adjudication="single-labeller")
        b = dict(a, id="s-002", split="test")
        self.assertEqual(er.split_integrity_errors({"cases": [a, dict(b, family="g")]}), [])
        errs = er.split_integrity_errors({"cases": [a, b]})
        self.assertEqual(len(errs), 1)
        self.assertIn("family 'f' spans splits (dev: s-001; test: s-002)", errs[0])
        self.assertTrue(er.split_integrity_errors({"cases": [case("s-003", "typical", expected=["data-privacy"])]}))
        self.assertTrue(er.split_integrity_errors([1]))


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
        # Numbers are only comparable on the same benchmark, so compare against
        # the latest PRE-IMPROVEMENT entry measured on this benchmark (re-measure
        # the keyword router from commit 8328c60 with --router when the benchmark
        # changes; see its history note).
        same = [h for h in self.baseline["history"]
                if "PRE-IMPROVEMENT" in h["note"] and h["benchmark_sha256"] == self.baseline["benchmark_sha256"]]
        self.assertTrue(same, "no PRE-IMPROVEMENT keyword-router entry measured on the current benchmark")
        pre = same[-1]["metrics"]["all"]
        cur = self.baseline["metrics"]["all"]
        for metric in er.HEADLINE_METRICS:
            if metric not in pre:  # e.g. negative_false_activation_rate, added after that entry was recorded
                continue
            with self.subTest(metric=metric):
                if metric in er.HIGHER_IS_BETTER:
                    self.assertGreaterEqual(cur[metric], pre[metric])
                else:
                    self.assertLessEqual(cur[metric], pre[metric])

    def test_current_floor_gates_every_headline_metric(self):
        for split in ("dev", "test", "all"):
            with self.subTest(split=split):
                self.assertEqual(set(self.baseline["metrics"][split]), set(er.HEADLINE_METRICS))

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
