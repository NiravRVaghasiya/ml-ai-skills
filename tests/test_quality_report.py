"""Tests for scripts/quality_report.py (stdlib only).

The report builder is pure, so most tests inject small fixture inputs instead of
running the real tools. One smoke test runs the static mode on the real repo.
"""
from __future__ import annotations

import contextlib
import copy
import io
import json
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

import testutil  # noqa: F401  (inserts scripts/ and schema/ onto sys.path as a side effect)
import quality_report as qr

REPO_ROOT = Path(__file__).resolve().parent.parent
STATUS_FIELDS = ("structural", "examples", "evals", "human_review")


def _tool(stdout="", code=0, command="python scripts/x.py"):
    return {"command": command, "exit_code": code, "stdout": stdout, "stderr": ""}


def _inventory(slug, blocks=2, illustrative=(), type_="workflow"):
    return {"skill": slug, "type": type_, "domain": "classical-ml", "lifecycle": "stable", "risk_level": "low",
            "evidence_level": "established-practice", "last_review_date": "2026-10-27", "python_blocks": blocks,
            "illustrative_blocks": len(illustrative), "illustrative_indices": list(illustrative)}


def _router_doc(test_task=0.8, test_far=0.0):
    split = {"precision": 0.7, "recall": 0.9, "top1_accuracy": 0.8, "task_accuracy": test_task,
             "false_activation_rate": test_far, "mean_unnecessary_activations": 0.4,
             "forbidden_selection_rate": 0.2, "n_cases": 10, "counts": {"expected": 3}}
    return {"top_k": 5, "splits": {"all": split, "dev": split, "test": split}, "misrouted": [
        {"id": "x-1", "split": "test"}, {"id": "x-2", "split": "dev"}], "threshold_failures": [], "split": "all"}


def _baseline():
    return {"date": "2026-10-27", "benchmark_sha256": "abc", "metrics": {"test": {
        "precision": 0.7, "recall": 0.9, "top1_accuracy": 0.8, "task_accuracy": 0.8,
        "false_activation_rate": 0.0, "mean_unnecessary_activations": 0.4, "forbidden_selection_rate": 0.2}}}


def _results(suite, tests, include_slow=False):
    return {"schema": qr.RESULTS_SCHEMA, "schema_version": 1, "suite": suite, "command": "c",
            "include_slow": include_slow, "python": "3.13.2", "platform": "Windows", "env": {}, "tests": tests}


def make_raw(**overrides):
    """A consistent fixture: alpha (workflow, 2 blocks), beta (reference, no blocks), gamma (workflow)."""
    evals = {"ok": True, "errors": [], "warnings": [], "fixture_self_tests": {"ok": 6, "total": 6},
             "coverage": {"summary": {"skills_total": 3}, "gaps": [], "skills": [
                 {"skill": "alpha", "cases": 2, "non_happy_path_cases": 2, "hard_assertion_cases": 2,
                  "executable_cases": 1, "has_plan": True, "missing": []},
                 {"skill": "beta", "cases": 1, "non_happy_path_cases": 1, "hard_assertion_cases": 1,
                  "executable_cases": 0, "has_plan": True, "missing": []},
                 {"skill": "gamma", "cases": 0, "non_happy_path_cases": 0, "hard_assertion_cases": 0,
                  "executable_cases": 0, "has_plan": False, "missing": ["plan.json", "cases"]}]}}
    raw = {
        "validate_skills": _tool(json.dumps({"errors": [], "warnings": [{"skill": "alpha", "code": "w"}]})),
        "generate_schema": _tool("ok"),
        "check_contract": _tool("public contract matches"),
        "validate_evals": _tool(json.dumps(evals)),
        "eval_router": _tool(json.dumps(_router_doc())),
        "check_freshness": _tool(json.dumps({"kinds_run": ["static-pattern"], "counts": {"STALE": 0, "INFO": 1},
                                             "note": "n", "findings": [{"skill": "alpha", "level": "INFO"}]})),
        "check_claims": _tool(json.dumps({"summary": {"claims": 1}, "errors": [], "findings": []})),
        "test_results": [], "example_coverage": [], "claude_cli": False,
        "baseline": _baseline(), "benchmark_sha256": "abc",
        "skills": [_inventory("alpha", blocks=3, illustrative=(2,)), _inventory("beta", blocks=0, type_="reference"),
                   _inventory("gamma", blocks=1)],
        "environment": {"python": "3.13.2", "platform": "Windows", "git_revision": "deadbeef", "git_dirty": False},
        "options": {"run_tests": False},
        "claims": [{"skill": "alpha", "verification": "runtime-test"}],
    }
    raw.update(overrides)
    return raw


def by_slug(report):
    return {s["skill"]: s for s in report["skills"]}


class TestPerSkillStatus(unittest.TestCase):
    def test_tiers_are_separate_and_nothing_is_labelled_verified(self):
        raw = make_raw(example_coverage=[json.dumps({"skill": "alpha", "blocks": [0, 1], "total_blocks": 3})])
        rep = qr.build_report(raw, "T")
        skills = by_slug(rep)
        self.assertEqual(skills["alpha"]["evidence_tiers"],
                         ["structural-valid", "examples-executed", "evals-covered-offline"])
        self.assertEqual(skills["beta"]["examples"]["status"], "no-runnable-blocks")
        self.assertEqual(skills["beta"]["evidence_tiers"], ["structural-valid", "evals-covered-offline"])
        self.assertEqual(skills["gamma"]["examples"]["status"], "not-executed")
        self.assertEqual(skills["gamma"]["evals"]["status"], "partial")
        self.assertEqual(skills["gamma"]["evidence_tiers"], ["structural-valid"])
        for s in rep["skills"]:
            self.assertEqual(s["human_review"]["status"], "not-recorded")
            self.assertFalse(s["evals"]["model_graded"])
            self.assertNotIn("human-reviewed", s["evidence_tiers"])
            for field in STATUS_FIELDS:
                self.assertNotIn("verified", s[field]["status"])
            for tier in s["evidence_tiers"]:
                self.assertNotIn("verified", tier)
        self.assertEqual(rep["summary"]["skills_human_reviewed"], 0)
        self.assertEqual(rep["summary"]["model_graded_evals"], "unmeasured")

    def test_validator_error_makes_skill_invalid_and_is_a_hard_failure(self):
        raw = make_raw(validate_skills=_tool(json.dumps({"errors": [{"skill": "gamma", "code": "missing-section"}],
                                                         "warnings": []}), code=1))
        rep = qr.build_report(raw, "T")
        self.assertEqual(by_slug(rep)["gamma"]["structural"], {"status": "invalid", "errors": 1, "warnings": 0})
        self.assertNotIn("structural-valid", by_slug(rep)["gamma"]["evidence_tiers"])
        self.assertTrue(any("validate_skills" in h for h in rep["summary"]["hard_failures"]))
        self.assertEqual(rep["checks"]["validate_skills"]["error_codes"], ["missing-section"])

    def test_example_status_without_coverage_is_not_measured(self):
        rep = qr.build_report(make_raw(), "T")
        alpha = by_slug(rep)["alpha"]["examples"]
        self.assertEqual(alpha["status"], "not-measured")
        self.assertIsNone(alpha["executed_blocks"])
        self.assertIsNone(rep["summary"]["skills_examples_all_runnable_executed"])
        self.assertIn("example-block-coverage", [c["check"] for c in rep["skipped_checks"]])

    def test_illustrative_blocks_do_not_count_as_executed(self):
        # Block 2 of alpha is illustrative; executing it explicitly must not make a partial run look complete.
        raw = make_raw(example_coverage=[json.dumps({"skill": "alpha", "blocks": [0, 2], "total_blocks": 3})])
        alpha = by_slug(qr.build_report(raw, "T"))["alpha"]["examples"]
        self.assertEqual((alpha["runnable_blocks"], alpha["executed_blocks"], alpha["status"]),
                         (2, 1, "partially-executed"))

    def test_failing_example_tests_override_block_coverage(self):
        tests = [{"id": "examples.test_alpha.T.test_ok", "outcome": "passed"},
                 {"id": "examples.test_alpha.T.test_bad", "outcome": "failed", "message": "AssertionError"}]
        raw = make_raw(example_coverage=[json.dumps({"skill": "alpha", "blocks": [0, 1], "total_blocks": 3})],
                       test_results=[_results("examples", tests)])
        rep = qr.build_report(raw, "T")
        alpha = by_slug(rep)["alpha"]
        self.assertEqual(alpha["examples"]["status"], "example-tests-failing")
        self.assertEqual(alpha["examples"]["example_tests"]["failed"], 1)
        self.assertNotIn("examples-executed", alpha["evidence_tiers"])
        self.assertTrue(any(h.startswith("examples tests") for h in rep["summary"]["hard_failures"]))

    def test_bad_coverage_lines_are_counted_not_trusted(self):
        raw = make_raw(example_coverage=["not json", json.dumps({"skill": "alpha"}), ""])
        rep = qr.build_report(raw, "T")
        self.assertEqual(rep["summary"]["coverage_lines_ignored"], 2)
        self.assertEqual(by_slug(rep)["alpha"]["examples"]["status"], "not-executed")


class TestStructuralNeedsValidatorOutput(unittest.TestCase):
    def test_validator_crash_gives_no_structural_tier(self):
        rep = qr.build_report(make_raw(validate_skills=_tool("Traceback (most recent call last): crash", code=1)), "T")
        self.assertIn("validate_skills.py did not produce JSON", rep["summary"]["hard_failures"])
        self.assertEqual(rep["checks"]["validate_skills"]["status"], "error")
        self.assertIsNone(rep["summary"]["skills_structural_valid"])
        for s in rep["skills"]:
            self.assertEqual(s["structural"]["status"], "not-measured", s["skill"])
            self.assertNotIn("structural-valid", s["evidence_tiers"])

    def test_validator_json_without_error_lists_is_not_trusted(self):
        rep = qr.build_report(make_raw(validate_skills=_tool(json.dumps({"unexpected": True}))), "T")
        self.assertIsNone(rep["summary"]["skills_structural_valid"])
        self.assertEqual(by_slug(rep)["alpha"]["structural"]["status"], "not-measured")

    def test_validator_not_run_gives_no_structural_tier(self):
        raw = make_raw()
        del raw["validate_skills"]
        rep = qr.build_report(raw, "T")
        self.assertEqual(rep["checks"]["validate_skills"]["status"], "not-run")
        self.assertIsNone(rep["summary"]["skills_structural_valid"])
        for s in rep["skills"]:
            self.assertEqual(s["structural"]["status"], "not-measured")
            self.assertNotIn("structural-valid", s["evidence_tiers"])


class TestEvidenceProvenance(unittest.TestCase):
    def test_coverage_with_wrong_block_count_is_stale(self):
        # Coverage from an older alpha with 7 blocks must not count for the current 3-block alpha.
        raw = make_raw(example_coverage=[json.dumps({"skill": "alpha", "blocks": [0, 1], "total_blocks": 7})])
        rep = qr.build_report(raw, "T")
        alpha = by_slug(rep)["alpha"]
        self.assertEqual(alpha["examples"]["status"], "coverage-stale")
        self.assertEqual(alpha["examples"]["executed_blocks"], 0)
        self.assertIn("total_blocks 7", alpha["examples"]["stale_reasons"][0])
        self.assertNotIn("examples-executed", alpha["evidence_tiers"])
        self.assertEqual(rep["summary"]["skills_with_stale_example_evidence"], ["alpha"])
        self.assertIn({"kind": "example-coverage-stale", "skills": ["alpha"]}, rep["inputs"])

    def test_coverage_without_total_blocks_or_with_other_sha_is_stale(self):
        for rec in ({"skill": "alpha", "blocks": [0, 1]},
                    {"skill": "alpha", "blocks": [0, 1], "total_blocks": 3, "skill_sha256": "old"}):
            with self.subTest(rec=rec):
                inv = _inventory("alpha", blocks=3, illustrative=(2,))
                inv["sha256"] = "new"
                raw = make_raw(example_coverage=[json.dumps(rec)], skills=[inv])
                alpha = by_slug(qr.build_report(raw, "T"))["alpha"]
                self.assertEqual(alpha["examples"]["status"], "coverage-stale")
                self.assertNotIn("examples-executed", alpha["evidence_tiers"])

    def test_example_results_for_a_changed_skill_are_stale(self):
        inv = _inventory("alpha", blocks=3, illustrative=(2,))
        inv["sha256"] = "current"
        doc = _results("examples", [{"id": "examples.test_alpha.T.test_bad", "outcome": "failed"}])
        doc["provenance"] = {"git_revision": "0ld", "git_dirty": False, "skill_sha256": {"alpha": "older"}}
        raw = make_raw(skills=[inv], test_results=[doc],
                       example_coverage=[json.dumps({"skill": "alpha", "blocks": [0, 1], "total_blocks": 3})])
        rep = qr.build_report(raw, "T")
        alpha = by_slug(rep)["alpha"]["examples"]
        self.assertEqual(alpha["status"], "example-results-stale")
        self.assertTrue(any("sha256 differs" in r for r in alpha["stale_reasons"]))
        prov = rep["tests"]["examples"]["provenance"]
        self.assertEqual((prov["recorded"], prov["matches_current_revision"], prov["stale_skills"]),
                         (True, False, ["alpha"]))

    def test_results_without_provenance_are_reported_as_untied(self):
        rep = qr.build_report(make_raw(test_results=[_results("unit", [{"id": "t", "outcome": "passed"}])]), "T")
        prov = rep["tests"]["unit"]["provenance"]
        self.assertFalse(prov["recorded"])
        self.assertIsNone(prov["matches_current_revision"])

    def test_coverage_without_example_results_lists_outcomes_as_skipped(self):
        raw = make_raw(example_coverage=[json.dumps({"skill": "alpha", "blocks": [0, 1], "total_blocks": 3})])
        checks = {c["check"]: c["reason"] for c in qr.build_report(raw, "T")["skipped_checks"]}
        self.assertIn("not whether the tests' assertions passed", checks["example-test-outcomes"])

    def test_collected_results_record_provenance(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "tests").mkdir()
            (root / "alpha").mkdir()
            (root / "alpha" / "SKILL.md").write_bytes(b"---\nname: alpha\n---\n")
            doc = qr.collect_tests(root, "unit")
            while str(root / "tests") in sys.path:
                sys.path.remove(str(root / "tests"))
        self.assertEqual(qr.validate_results(doc), [])
        self.assertIn("git_revision", doc["provenance"])
        self.assertEqual(list(doc["provenance"]["skill_sha256"]), ["alpha"])


class TestHumanReview(unittest.TestCase):
    def test_claim_level_review_never_counts_as_a_reviewed_skill(self):
        claims = [{"skill": "alpha", "verification": "human-review", "status": "current"},
                  {"skill": "beta", "verification": "human-review", "status": "disputed"},
                  {"skill": "gamma", "verification": "human-review", "status": "review-due"}]
        rep = qr.build_report(make_raw(claims=claims), "T")
        skills = by_slug(rep)
        self.assertEqual(skills["alpha"]["human_review"],
                         {"status": "claim-level-review-only", "current_human_reviewed_claims": 1})
        self.assertEqual(skills["beta"]["human_review"]["status"], "not-recorded")
        self.assertEqual(skills["gamma"]["human_review"]["status"], "not-recorded")
        self.assertEqual(rep["summary"]["skills_human_reviewed"], 0)
        self.assertEqual(rep["summary"]["skills_with_claim_level_human_review"], 1)


class TestChecks(unittest.TestCase):
    def test_router_regression_and_benchmark_hash_mismatch_fail(self):
        rep = qr.build_report(make_raw(eval_router=_tool(json.dumps(_router_doc(test_task=0.7, test_far=0.1)))), "T")
        regs = rep["checks"]["eval_router"]["baseline"]["regressions"]
        self.assertIn("test task_accuracy 0.7 < baseline 0.8", regs)
        self.assertIn("test false_activation_rate 0.1 > baseline 0.0", regs)
        self.assertEqual(rep["checks"]["eval_router"]["status"], "fail")
        self.assertTrue(any(h.startswith("eval_router") for h in rep["summary"]["hard_failures"]))

        rep = qr.build_report(make_raw(benchmark_sha256="other"), "T")
        self.assertFalse(rep["checks"]["eval_router"]["baseline"]["benchmark_sha256_matches"])
        self.assertEqual(rep["checks"]["eval_router"]["status"], "fail")

    def test_router_at_baseline_passes_and_counts_misrouted(self):
        rep = qr.build_report(make_raw(), "T")
        router = rep["checks"]["eval_router"]
        self.assertEqual(router["status"], "pass")
        self.assertEqual(router["misrouted"], {"dev": 1, "test": 1})
        self.assertNotIn("counts", router["splits"]["test"])

    def test_evals_are_reported_as_offline_only(self):
        ev = qr.build_report(make_raw(), "T")["checks"]["validate_evals"]
        self.assertEqual(ev["status"], "pass")
        self.assertFalse(ev["model_graded"])
        self.assertIn("No model output was graded", ev["note"])

    def test_stale_freshness_and_contract_failure_are_hard_failures(self):
        raw = make_raw(check_freshness=_tool(json.dumps({"counts": {"STALE": 2}, "findings": []}), code=1),
                       check_contract=_tool("CONTRACT ERROR: x", code=1))
        hard = qr.build_report(raw, "T")["summary"]["hard_failures"]
        self.assertIn("check_freshness: 2 STALE finding(s)", hard)
        self.assertIn("check_contract: exit 1", hard)

    def test_tool_without_json_is_reported(self):
        rep = qr.build_report(make_raw(validate_evals=_tool("Traceback ...", code=1)), "T")
        self.assertEqual(rep["checks"]["validate_evals"]["status"], "fail")
        self.assertIn("validate_evals.py did not produce JSON", rep["summary"]["hard_failures"])


class TestSkippedChecks(unittest.TestCase):
    def test_always_skipped_checks_are_listed_with_reasons(self):
        skipped = {c["check"]: c["reason"] for c in qr.build_report(make_raw(), "T")["skipped_checks"]}
        for check in ("unit-tests", "example-tests", "network", "model-evals", "github-actions", "other-platforms",
                      "generated-artifacts", "executable-evals"):
            self.assertIn(check, skipped)
            self.assertTrue(skipped[check])
        self.assertIn("not on PATH", skipped["claude-cli"])

    def test_unit_run_without_slow_tests_lists_installer_tests_as_skipped(self):
        raw = make_raw(test_results=[_results("unit", [{"id": "t.A.test_x", "outcome": "passed"}])], claude_cli=True)
        rep = qr.build_report(raw, "T")
        checks = [c["check"] for c in rep["skipped_checks"]]
        self.assertNotIn("unit-tests", checks)
        self.assertIn("installer-tests", checks)
        self.assertNotIn("claude-cli", checks)
        self.assertEqual(rep["tests"]["unit"]["passed"], 1)
        self.assertIsNone(rep["tests"]["examples"])

    def test_skip_reasons_are_aggregated(self):
        tests = [{"id": "a", "outcome": "skipped", "reason": "PyYAML not installed"},
                 {"id": "b", "outcome": "skipped", "reason": "PyYAML not installed"},
                 {"id": "c", "outcome": "skipped", "reason": "claude CLI not on PATH"},
                 {"id": "d", "outcome": "passed"}]
        unit = qr.build_report(make_raw(test_results=[_results("unit", tests)]), "T")["tests"]["unit"]
        self.assertEqual((unit["total"], unit["passed"], unit["skipped"]), (4, 1, 3))
        self.assertEqual(unit["skip_reasons"], [{"reason": "PyYAML not installed", "count": 2},
                                                {"reason": "claude CLI not on PATH", "count": 1}])

    def test_invalid_results_file_is_a_hard_failure(self):
        rep = qr.build_report(make_raw(test_results=[{"schema": "something-else"}]), "T")
        self.assertIn("an ingested test-results file is invalid", rep["summary"]["hard_failures"])
        self.assertIsNone(rep["tests"]["unit"])


class TestDeterminism(unittest.TestCase):
    def test_only_generated_at_differs(self):
        raw = make_raw(example_coverage=[json.dumps({"skill": "alpha", "blocks": [1, 0], "total_blocks": 3})])
        a = qr.build_report(copy.deepcopy(raw), "2026-10-28T00:00:00Z")
        b = qr.build_report(copy.deepcopy(raw), "2026-10-29T12:00:00Z")
        self.assertNotEqual(a["generated_at"], b["generated_at"])
        for field in qr.NONDETERMINISTIC_FIELDS:
            a.pop(field)
            b.pop(field)
        self.assertEqual(json.dumps(a, sort_keys=True), json.dumps(b, sort_keys=True))

    def test_same_timestamp_gives_identical_bytes(self):
        raw = make_raw()
        self.assertEqual(json.dumps(qr.build_report(copy.deepcopy(raw), "T"), sort_keys=True),
                         json.dumps(qr.build_report(copy.deepcopy(raw), "T"), sort_keys=True))


FIXTURE_TESTS = textwrap.dedent('''
    import unittest

    class Sample(unittest.TestCase):
        def test_pass(self):
            pass

        def test_fail(self):
            self.assertEqual(1, 2)

        def test_error(self):
            raise RuntimeError("boom")

        @unittest.skip("needs a GPU")
        def test_skip(self):
            pass

        def test_subtests(self):
            for i in range(3):
                with self.subTest(i=i):
                    self.assertNotEqual(i, 1)
''')


class TestCollector(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        (self.root / "tests").mkdir()
        (self.root / "tests" / "test_qrfixture_sample.py").write_text(FIXTURE_TESTS, encoding="utf-8")
        (self.root / "tests" / "test_install_qrfixture.py").write_text(
            "import unittest\n\nclass Slow(unittest.TestCase):\n    def test_slow(self):\n        pass\n",
            encoding="utf-8")
        self.addCleanup(self._cleanup)

    def _cleanup(self):
        for name in ("test_qrfixture_sample", "test_install_qrfixture"):
            sys.modules.pop(name, None)
        tests_dir = str(self.root / "tests")
        while tests_dir in sys.path:
            sys.path.remove(tests_dir)
        self.tmp.cleanup()

    def test_collect_records_every_outcome_and_skip_reason(self):
        doc = qr.collect_tests(self.root, "unit")
        self.assertEqual(qr.validate_results(doc), [])
        self.assertEqual(doc["selection"], ["test_qrfixture_sample"])  # test_install*.py excluded by default
        s = doc["summary"]
        self.assertEqual((s["passed"], s["failed"], s["error"], s["skipped"]), (1, 2, 1, 1))
        self.assertEqual(s["skip_reasons"], [{"reason": "needs a GPU", "count": 1}])
        failed = {t["id"]: t for t in doc["tests"] if t["outcome"] == "failed"}
        self.assertTrue(any("(i=1)" in tid for tid in failed), failed)
        self.assertIn("AssertionError", failed["test_qrfixture_sample.Sample.test_fail"]["message"])

    def test_include_slow_adds_installer_modules(self):
        doc = qr.collect_tests(self.root, "unit", include_slow=True)
        self.assertEqual(doc["selection"], ["test_install_qrfixture", "test_qrfixture_sample"])
        self.assertTrue(doc["include_slow"])

    def test_cli_writes_results_and_exits_1_on_failures(self):
        out = self.root / "results.json"
        with contextlib.redirect_stdout(io.StringIO()):
            code = qr.main(["--root", str(self.root), "--collect-tests", str(out), "--suite", "unit"])
        self.assertEqual(code, 1)
        doc = json.loads(out.read_text(encoding="utf-8"))
        self.assertEqual(doc["schema"], qr.RESULTS_SCHEMA)
        self.assertEqual(doc["summary"]["failed"], 2)

    def test_usage_errors(self):
        for argv in (["--root", str(self.root), "--collect-tests", str(self.root / "x.json")],
                     ["--suite", "unit"],
                     ["--test-results", str(self.root / "missing.json")],
                     ["--today", "28-10-2026"],
                     ["--timestamp", "X"]):
            with self.subTest(argv=argv), contextlib.redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit) as cm:
                    qr.main(argv)
            self.assertEqual(cm.exception.code, 2)

    def test_validate_results_rejects_malformed_documents(self):
        self.assertTrue(qr.validate_results([]))
        self.assertTrue(qr.validate_results({"schema": qr.RESULTS_SCHEMA, "schema_version": 1, "suite": "unit",
                                             "tests": [{"id": "x", "outcome": "maybe"}]}))
        self.assertTrue(qr.validate_results({"schema": qr.RESULTS_SCHEMA, "schema_version": 9, "suite": "nope",
                                             "tests": []}))


class TestRealRepoStaticReport(unittest.TestCase):
    def test_static_report_on_this_repo(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "report.json"
            p = subprocess.run([sys.executable, str(REPO_ROOT / "scripts" / "quality_report.py"), "--out", str(out),
                                "--timestamp", "2026-01-01T00:00:00Z"], cwd=REPO_ROOT, capture_output=True,
                               text=True, timeout=600)
            self.assertIn(p.returncode, (0, 1), p.stderr)  # 1 = a gate reported a hard failure (still a report)
            rep = json.loads(out.read_text(encoding="utf-8"))
        self.assertEqual((rep["schema"], rep["generated_at"]), (qr.REPORT_SCHEMA, "2026-01-01T00:00:00Z"))
        self.assertEqual(len(rep["skills"]), len(testutil.sl.iter_skill_dirs(REPO_ROOT)))
        for s in rep["skills"]:
            self.assertEqual(s["examples"]["status"] in ("not-measured", "no-runnable-blocks"), True, s["skill"])
            self.assertIn(s["human_review"]["status"], ("not-recorded", "claim-level-review-only"))
            self.assertIn(s["structural"]["status"], ("valid", "invalid"), s["skill"])
        self.assertEqual(rep["summary"]["skills_human_reviewed"], 0)
        for check in ("validate_skills", "validate_evals", "eval_router", "check_freshness", "check_claims",
                      "check_contract", "generate_schema"):
            self.assertIn(rep["checks"][check]["status"], ("pass", "fail"), check)
        self.assertEqual(rep["checks"]["check_generated"]["status"], "not-run")


if __name__ == "__main__":
    unittest.main()
