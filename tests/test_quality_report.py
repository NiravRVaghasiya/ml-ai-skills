"""Tests for scripts/quality_report.py (stdlib only).

The report builder is pure, so most tests inject small fixture inputs instead of
running the real tools. One smoke test runs the static mode on the real repo.
The coverage-record side of the provenance contract (tests/examples/harness.py)
is tested here too, because the report is what consumes it.
"""
from __future__ import annotations

import argparse
import contextlib
import copy
import hashlib
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path
from unittest import mock

import testutil  # noqa: F401  (inserts scripts/ and schema/ onto sys.path as a side effect)
import quality_report as qr
from examples import harness

REPO_ROOT = Path(__file__).resolve().parent.parent
STATUS_FIELDS = ("structural", "examples", "evals", "human_review")
GIT = shutil.which("git")
DROP = object()  # marks a field to remove from a fixture record


def sha(slug: str) -> str:
    """A stand-in SKILL.md sha256 for a fixture skill (64 lowercase hex, like the real one)."""
    return hashlib.sha256(f"SKILL.md of {slug}".encode()).hexdigest()


def _tool(stdout="", code=0, command="python scripts/x.py"):
    return {"command": command, "exit_code": code, "stdout": stdout, "stderr": ""}


def _inventory(slug, blocks=2, illustrative=(), type_="workflow"):
    return {"skill": slug, "type": type_, "domain": "classical-ml", "lifecycle": "stable", "risk_level": "low",
            "evidence_level": "established-practice", "last_review_date": "2026-10-27", "python_blocks": blocks,
            "illustrative_blocks": len(illustrative), "illustrative_indices": list(illustrative),
            "sha256": sha(slug)}


def _cov(slug, blocks, total, **over):
    """A harness v2 coverage line for the current fixture SKILL.md of `slug`."""
    rec = {"schema": qr.COVERAGE_SCHEMA, "schema_version": 2, "skill": slug, "blocks": list(blocks),
           "total_blocks": total, "skill_sha256": sha(slug), "git_revision": "deadbeef", "git_dirty": False,
           "python": "3.13.2", "platform": "Windows", "recorded_at": "2026-10-29T00:00:00Z", "env": {},
           "test_id": None, "test_module": "examples." + qr._example_module_for(slug)}
    rec.update(over)
    return json.dumps({k: v for k, v in rec.items() if v is not DROP})


def alpha_full(**over):
    return _cov("alpha", [0, 1], 3, **over)  # every runnable block of the fixture alpha (block 2 is illustrative)


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


def _results(suite, tests, include_slow=False, provenance=True, shas=None):
    doc = {"schema": qr.RESULTS_SCHEMA, "schema_version": 1, "suite": suite, "command": "c",
           "include_slow": include_slow, "python": "3.13.2", "platform": "Windows", "env": {}, "tests": tests}
    if provenance:
        doc["provenance"] = {"git_revision": "deadbeef", "git_dirty": False,
                             "skill_sha256": shas if shas is not None else {s: sha(s) for s in ("alpha", "beta",
                                                                                                "gamma")}}
    return doc


def _reviews_doc(reviews):
    return {"schema": qr.REVIEWS_SCHEMA, "schema_version": 1, "description": "d", "reviews": reviews}


def _reviews(reviews=(), present=True):
    return {"path": "docs/reviews.json", "present": present, "doc": _reviews_doc(list(reviews)) if present else None,
            "error": None}


def _review(slug="alpha", **over):
    rec = {"skill": slug, "reviewer": "A. Person", "date": "2026-10-28", "skill_sha256": sha(slug),
           "scope": "full", "outcome": "approved"}
    rec.update(over)
    return rec


def _scan(**over):
    out = {"declared": None, "harness_calls": [], "slug_constant": [], "unknown_slugs": [], "parse_error": None}
    out.update(over)
    return out


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
        "example_modules": {}, "reviews": _reviews(), "today": "2026-10-29",
        "environment": {"python": "3.13.2", "platform": "Windows", "git_revision": "deadbeef", "git_dirty": False},
        "options": {"run_tests": False},
        "claims": [{"skill": "alpha", "verification": "runtime-test"}],
    }
    raw.update(overrides)
    return raw


def by_slug(report):
    return {s["skill"]: s for s in report["skills"]}


def report(**overrides):
    return qr.build_report(make_raw(**overrides), "T")


class TestPerSkillStatus(unittest.TestCase):
    def test_tiers_are_separate_and_nothing_is_labelled_verified(self):
        rep = report(example_coverage=[alpha_full()])
        skills = by_slug(rep)
        # Coverage without test results: executed, but nothing says the tests passed.
        self.assertEqual(skills["alpha"]["examples"]["status"], "coverage-only-outcome-unknown")
        self.assertEqual(skills["alpha"]["examples"]["coverage_status"], "all-runnable-blocks-executed")
        self.assertEqual(skills["alpha"]["examples"]["outcome"], "not-measured")
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
        self.assertEqual(rep["summary"]["skills_examples_coverage_only_outcome_unknown"], ["alpha"])
        self.assertIsNone(rep["summary"]["skills_examples_executed_and_passed"])

    def test_executed_and_passed_needs_coverage_and_passing_results_at_the_same_sha(self):
        tests = [{"id": "examples.test_alpha.T.test_ok", "outcome": "passed"},
                 {"id": "examples.test_alpha.T.test_gpu", "outcome": "skipped", "reason": "no CUDA"}]
        rep = report(example_coverage=[alpha_full()], test_results=[_results("examples", tests)])
        alpha = by_slug(rep)["alpha"]
        self.assertEqual((alpha["examples"]["status"], alpha["examples"]["outcome"]), ("executed-and-passed", "passed"))
        self.assertEqual(alpha["evidence_tiers"],
                         ["structural-valid", "examples-executed", "example-tests-passed", "evals-covered-offline"])
        self.assertEqual(alpha["examples"]["example_tests"]["skipped"], 1)
        self.assertEqual(rep["summary"]["skills_examples_executed_and_passed"], 1)

    def test_passing_results_without_full_coverage_are_not_executed_and_passed(self):
        tests = [{"id": "examples.test_alpha.T.test_ok", "outcome": "passed"}]
        alpha = by_slug(report(example_coverage=[_cov("alpha", [0], 3)],
                               test_results=[_results("examples", tests)]))["alpha"]
        self.assertEqual((alpha["examples"]["status"], alpha["examples"]["outcome"]), ("partially-executed", "passed"))
        self.assertNotIn("example-tests-passed", alpha["evidence_tiers"])

    def test_results_not_tied_to_the_current_sha_give_no_pass(self):
        tests = [{"id": "examples.test_alpha.T.test_ok", "outcome": "passed"}]
        for label, doc in (("no provenance", _results("examples", tests, provenance=False)),
                           ("no sha for alpha", _results("examples", tests, shas={}))):
            with self.subTest(label):
                rep = report(example_coverage=[alpha_full()], test_results=[doc])
                alpha = by_slug(rep)["alpha"]["examples"]
                # No sha recorded for alpha: results-stale (provenance present) or unattributable (absent).
                self.assertNotEqual(alpha["status"], "executed-and-passed")
                self.assertIn(alpha["outcome"], ("results-unattributable", "results-stale"))
        rep = report(example_coverage=[alpha_full()], test_results=[_results("examples", tests, provenance=False)])
        self.assertEqual(by_slug(rep)["alpha"]["examples"]["status"], "coverage-only-outcome-unknown")
        self.assertEqual(rep["summary"]["skills_with_unattributable_example_evidence"], ["alpha"])

    def test_all_skipped_results_are_not_a_pass(self):
        tests = [{"id": "examples.test_alpha.T.test_ok", "outcome": "skipped", "reason": "slow"}]
        alpha = by_slug(report(example_coverage=[alpha_full()], test_results=[_results("examples", tests)]))["alpha"]
        self.assertEqual((alpha["examples"]["status"], alpha["examples"]["outcome"]),
                         ("coverage-only-outcome-unknown", "all-skipped"))

    def test_example_status_without_coverage_is_not_measured(self):
        rep = report()
        alpha = by_slug(rep)["alpha"]["examples"]
        self.assertEqual(alpha["status"], "not-measured")
        self.assertIsNone(alpha["executed_blocks"])
        self.assertIsNone(rep["summary"]["skills_examples_all_runnable_executed"])
        self.assertIn("example-block-coverage", [c["check"] for c in rep["skipped_checks"]])

    def test_illustrative_blocks_do_not_count_as_executed(self):
        # Block 2 of alpha is illustrative; executing it explicitly must not make a partial run look complete.
        alpha = by_slug(report(example_coverage=[_cov("alpha", [0, 2], 3)]))["alpha"]["examples"]
        self.assertEqual((alpha["runnable_blocks"], alpha["executed_blocks"], alpha["status"]),
                         (2, 1, "partially-executed"))

    def test_failing_example_tests_override_block_coverage(self):
        tests = [{"id": "examples.test_alpha.T.test_ok", "outcome": "passed"},
                 {"id": "examples.test_alpha.T.test_bad", "outcome": "failed", "message": "AssertionError"}]
        rep = report(example_coverage=[alpha_full()], test_results=[_results("examples", tests)])
        alpha = by_slug(rep)["alpha"]
        self.assertEqual(alpha["examples"]["status"], "example-tests-failing")
        self.assertEqual(alpha["examples"]["example_tests"]["failed"], 1)
        self.assertNotIn("examples-executed", alpha["evidence_tiers"])
        self.assertTrue(any(h.startswith("examples tests") for h in rep["summary"]["hard_failures"]))
        self.assertEqual(rep["summary"]["skills_example_tests_failing"], ["alpha"])

    def test_failures_are_reported_even_when_results_cannot_be_tied_to_the_tree(self):
        tests = [{"id": "examples.test_alpha.T.test_bad", "outcome": "error", "message": "boom"}]
        alpha = by_slug(report(example_coverage=[alpha_full()],
                               test_results=[_results("examples", tests, provenance=False)]))["alpha"]
        self.assertEqual(alpha["examples"]["status"], "example-tests-failing")

    def test_several_examples_documents_are_merged_in_either_order(self):
        # Regression: per-skill status used the first document and the suite totals the last, so a
        # failure in the first one vanished from tests.examples, the hard failures and the exit code.
        ok = _results("examples", [{"id": "examples.test_alpha.T.test_ok", "outcome": "passed"}])
        bad = _results("examples", [{"id": "examples.test_alpha.T.test_bad", "outcome": "failed"}])
        for label, docs in (("ok, bad", [ok, bad]), ("bad, ok", [bad, ok])):
            with self.subTest(label):
                rep = report(example_coverage=[alpha_full()], test_results=copy.deepcopy(docs))
                alpha = by_slug(rep)["alpha"]["examples"]
                self.assertEqual((alpha["status"], alpha["outcome"]), ("example-tests-failing", "failing"))
                self.assertEqual(alpha["example_tests"]["failed"], 1)
                self.assertEqual(alpha["example_results"], {"current": 2, "stale": 0, "untied": 0})
                ex = rep["tests"]["examples"]
                self.assertEqual((ex["documents"], ex["total"], ex["passed"], ex["failed"]), (2, 2, 1, 1))
                self.assertEqual([t["id"] for t in ex["failures"]], ["examples.test_alpha.T.test_bad"])
                self.assertEqual(len(ex["provenance"]["documents"]), 2)
                self.assertTrue(any(h.startswith("examples tests: 1 failed") for h in rep["summary"]["hard_failures"]))

    def test_a_stale_document_failure_does_not_override_current_passing_results(self):
        old = _results("examples", [{"id": "examples.test_alpha.T.test_bad", "outcome": "failed"}],
                       shas={"alpha": "0" * 64, "beta": sha("beta"), "gamma": sha("gamma")})
        new = _results("examples", [{"id": "examples.test_alpha.T.test_ok", "outcome": "passed"}])
        rep = report(example_coverage=[alpha_full()], test_results=[old, new])
        alpha = by_slug(rep)["alpha"]["examples"]
        self.assertEqual((alpha["status"], alpha["outcome"]), ("executed-and-passed", "passed"))
        self.assertEqual(alpha["example_results"], {"current": 1, "stale": 1, "untied": 0})
        self.assertTrue(any("sha256 differs" in r for r in alpha["stale_reasons"]))
        # The old failure is still a suite failure: it is never hidden from the exit code.
        self.assertEqual(rep["tests"]["examples"]["failed"], 1)
        self.assertTrue(rep["summary"]["hard_failures"])
        prov = rep["tests"]["examples"]["provenance"]
        self.assertEqual((prov["recorded"], prov["stale_skills"]), (True, ["alpha"]))

    def test_merged_unit_documents_keep_installer_runs(self):
        fast = _results("unit", [{"id": "t.A.test_x", "outcome": "passed"}])
        slow = _results("unit", [{"id": "test_install.A.test_y", "outcome": "passed"}], include_slow=True)
        rep = report(test_results=[fast, slow])
        unit = rep["tests"]["unit"]
        self.assertEqual((unit["documents"], unit["total"], unit["include_slow"], unit["command"]), (2, 2, True, "c"))
        self.assertNotIn("installer-tests", [c["check"] for c in rep["skipped_checks"]])

    def test_bad_coverage_lines_are_counted_not_trusted(self):
        rep = report(example_coverage=["not json", json.dumps({"skill": "alpha"}), "",
                                       alpha_full(schema_version=99), alpha_full(schema="other/schema")])
        self.assertEqual(rep["summary"]["coverage_lines_ignored"], 4)
        self.assertEqual(by_slug(rep)["alpha"]["examples"]["status"], "not-executed")


class TestStructuralNeedsValidatorOutput(unittest.TestCase):
    def test_validator_crash_gives_no_structural_tier(self):
        rep = report(validate_skills=_tool("Traceback (most recent call last): crash", code=1))
        self.assertIn("validate_skills.py did not produce JSON", rep["summary"]["hard_failures"])
        self.assertEqual(rep["checks"]["validate_skills"]["status"], "error")
        self.assertIsNone(rep["summary"]["skills_structural_valid"])
        for s in rep["skills"]:
            self.assertEqual(s["structural"]["status"], "not-measured", s["skill"])
            self.assertNotIn("structural-valid", s["evidence_tiers"])

    def test_validator_json_without_error_lists_is_not_trusted(self):
        rep = report(validate_skills=_tool(json.dumps({"unexpected": True})))
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

    def test_validator_error_makes_skill_invalid_and_is_a_hard_failure(self):
        rep = report(validate_skills=_tool(json.dumps({"errors": [{"skill": "gamma", "code": "missing-section"}],
                                                       "warnings": []}), code=1))
        self.assertEqual(by_slug(rep)["gamma"]["structural"], {"status": "invalid", "errors": 1, "warnings": 0})
        self.assertNotIn("structural-valid", by_slug(rep)["gamma"]["evidence_tiers"])
        self.assertTrue(any("validate_skills" in h for h in rep["summary"]["hard_failures"]))
        self.assertEqual(rep["checks"]["validate_skills"]["error_codes"], ["missing-section"])


class TestEvidenceProvenance(unittest.TestCase):
    def test_coverage_with_wrong_block_count_is_stale(self):
        # Coverage from an older alpha with 7 blocks must not count for the current 3-block alpha.
        rep = report(example_coverage=[_cov("alpha", [0, 1], 7)])
        alpha = by_slug(rep)["alpha"]
        self.assertEqual(alpha["examples"]["status"], "coverage-stale")
        self.assertEqual(alpha["examples"]["executed_blocks"], 0)
        self.assertIn("total_blocks 7", alpha["examples"]["stale_reasons"][0])
        self.assertNotIn("examples-executed", alpha["evidence_tiers"])
        self.assertEqual(rep["summary"]["skills_with_stale_example_evidence"], ["alpha"])
        self.assertIn({"kind": "example-coverage-stale", "skills": ["alpha"]}, rep["inputs"])

    def test_coverage_for_another_sha_is_stale_even_with_the_same_block_count(self):
        alpha = by_slug(report(example_coverage=[alpha_full(skill_sha256="0" * 64)]))["alpha"]
        self.assertEqual(alpha["examples"]["status"], "coverage-stale")
        self.assertIn("skill_sha256 differs", alpha["examples"]["stale_reasons"][0])
        self.assertNotIn("examples-executed", alpha["evidence_tiers"])

    def test_a_leftover_stale_record_does_not_hide_full_current_coverage(self):
        # The harness appends, so a reused coverage file keeps records for an older SKILL.md.
        rep = report(example_coverage=[alpha_full(), alpha_full(skill_sha256="0" * 64)])
        alpha = by_slug(rep)["alpha"]["examples"]
        self.assertEqual((alpha["coverage_status"], alpha["executed_blocks"]), ("all-runnable-blocks-executed", 2))
        self.assertIn("skill_sha256 differs", alpha["stale_reasons"][0])
        self.assertIn({"kind": "example-coverage-stale", "skills": ["alpha"]}, rep["inputs"])
        # Current records that do not cover every runnable block: the stale record is the signal to re-run.
        alpha = by_slug(report(example_coverage=[_cov("alpha", [0], 3), alpha_full(skill_sha256="0" * 64)]))
        self.assertEqual(alpha["alpha"]["examples"]["coverage_status"], "coverage-stale")

    def test_require_coverage_revision_makes_records_without_one_unattributable(self):
        line = alpha_full(git_revision=None)
        self.assertEqual(by_slug(report(example_coverage=[line]))["alpha"]["examples"]["coverage_status"],
                         "all-runnable-blocks-executed")  # default: the SKILL.md sha256 ties it to the tree
        rep = report(example_coverage=[line], options={"require_coverage_revision": True})
        alpha = by_slug(rep)["alpha"]["examples"]
        self.assertEqual((alpha["coverage_status"], alpha["executed_blocks"]), ("coverage-unattributable", 0))
        self.assertIn("no git_revision", alpha["unattributable_reasons"][0])
        rep = report(example_coverage=[alpha_full()], options={"require_coverage_revision": True})
        self.assertEqual(by_slug(rep)["alpha"]["examples"]["coverage_status"], "all-runnable-blocks-executed")

    def test_coverage_without_sha_or_total_blocks_is_unattributable(self):
        for label, line in (("no sha", alpha_full(skill_sha256=DROP)),
                            ("no total_blocks", alpha_full(total_blocks=DROP)),
                            ("legacy record", json.dumps({"skill": "alpha", "blocks": [0, 1], "total_blocks": 3}))):
            with self.subTest(label):
                rep = report(example_coverage=[line])
                alpha = by_slug(rep)["alpha"]
                self.assertEqual(alpha["examples"]["status"], "coverage-unattributable")
                self.assertEqual(alpha["examples"]["executed_blocks"], 0)
                self.assertTrue(alpha["examples"]["unattributable_reasons"])
                self.assertNotIn("examples-executed", alpha["evidence_tiers"])
                self.assertIn({"kind": "example-coverage-unattributable", "skills": ["alpha"]}, rep["inputs"])
                self.assertEqual(rep["summary"]["skills_with_unattributable_example_evidence"], ["alpha"])

    def test_an_unattributable_record_does_not_hide_usable_ones(self):
        alpha = by_slug(report(example_coverage=[alpha_full(), _cov("alpha", [0], 3, skill_sha256=DROP)]))["alpha"]
        self.assertEqual(alpha["examples"]["coverage_status"], "all-runnable-blocks-executed")
        self.assertTrue(alpha["examples"]["unattributable_reasons"])

    def test_records_for_unknown_skills_are_listed(self):
        rep = report(example_coverage=[_cov("no-such-skill", [0], 1)])
        self.assertIn("example-coverage-unknown-skills", [i["kind"] for i in rep["inputs"]])

    def test_coverage_provenance_is_summarised(self):
        rep = report(example_coverage=[alpha_full(), _cov("gamma", [0], 1, git_revision=None, git_dirty=True,
                                                          platform="Linux")],
                     inputs=[{"kind": "example-coverage", "source": "c.jsonl", "records": 2}])
        prov = next(i for i in rep["inputs"] if i["kind"] == "example-coverage-provenance")
        self.assertEqual((prov["records"], prov["accepted"], prov["records_without_revision"]), (2, 2, 1))
        self.assertEqual(prov["git_revisions"], ["deadbeef"])
        self.assertEqual(prov["git_dirty"], [False, True])
        self.assertEqual(prov["records_with_untracked_files"], 0)
        self.assertEqual(prov["platforms"], ["Linux", "Windows"])
        self.assertIsNone(prov["matches_current_revision"])  # one record names no revision: unknown
        self.assertEqual(prov["recorded_between"], ["2026-10-29T00:00:00Z", "2026-10-29T00:00:00Z"])
        ingested = next(i for i in rep["inputs"] if i["kind"] == "example-coverage")
        self.assertIn("example-coverage-provenance", ingested["provenance"])
        prov = next(i for i in report(example_coverage=[alpha_full()])["inputs"]
                    if i["kind"] == "example-coverage-provenance")
        self.assertIs(prov["matches_current_revision"], True)
        prov = next(i for i in report(example_coverage=[alpha_full(git_revision="0ther")])["inputs"]
                    if i["kind"] == "example-coverage-provenance")
        self.assertIs(prov["matches_current_revision"], False)

    def test_example_results_for_a_changed_skill_are_stale(self):
        doc = _results("examples", [{"id": "examples.test_alpha.T.test_bad", "outcome": "failed"}],
                       shas={"alpha": "older"})
        doc["provenance"]["git_revision"] = "0ld"
        rep = report(test_results=[doc], example_coverage=[alpha_full()])
        alpha = by_slug(rep)["alpha"]["examples"]
        self.assertEqual(alpha["status"], "example-results-stale")
        self.assertTrue(any("sha256 differs" in r for r in alpha["stale_reasons"]))
        prov = rep["tests"]["examples"]["provenance"]
        self.assertEqual((prov["recorded"], prov["matches_current_revision"], "alpha" in prov["stale_skills"]),
                         (True, False, True))

    def test_results_without_provenance_are_reported_as_untied(self):
        rep = report(test_results=[_results("unit", [{"id": "t", "outcome": "passed"}], provenance=False)])
        prov = rep["tests"]["unit"]["provenance"]
        self.assertFalse(prov["recorded"])
        self.assertIsNone(prov["matches_current_revision"])

    def test_coverage_without_example_results_lists_outcomes_as_skipped(self):
        checks = {c["check"]: c["reason"] for c in report(example_coverage=[alpha_full()])["skipped_checks"]}
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


class TestAttribution(unittest.TestCase):
    KNOWN = {"alpha", "beta", "gamma"}

    def test_static_scan_finds_declarations_harness_calls_and_slug_constants(self):
        src = textwrap.dedent('''
            from examples import harness
            SLUG = "alpha"
            OTHER = "gamma"
            def f():
                harness.run_blocks(SLUG, [0])
                harness.python_blocks(OTHER)
                harness.sl.load_skill(harness.REPO_ROOT / "beta")
                harness.run_blocks("not-a-skill")
        ''')
        scan = qr.scan_example_module(src, self.KNOWN)
        self.assertEqual((scan["declared"], scan["harness_calls"], scan["slug_constant"], scan["unknown_slugs"]),
                         (None, ["alpha", "beta", "gamma"], ["alpha"], ["not-a-skill"]))
        self.assertEqual(qr.scan_example_module('SKILLS = ("gamma", "alpha")\n', self.KNOWN)["declared"],
                         ["alpha", "gamma"])
        self.assertEqual(qr.scan_example_module('SKILL = "beta"\n', self.KNOWN)["declared"], ["beta"])
        self.assertEqual(qr.scan_example_module("SKILLS = ()\n", self.KNOWN)["declared"], [])
        self.assertIn("SyntaxError", qr.scan_example_module("def (:\n", self.KNOWN)["parse_error"])

    def test_differently_named_module_is_attributed_by_scan(self):
        # The case module-name matching missed (e.g. test_data_pipelines_object_store.py).
        tests = [{"id": "examples.test_alpha_object_store.T.test_bad", "outcome": "failed"}]
        rep = report(example_modules={"test_alpha_object_store": _scan(slug_constant=["alpha"],
                                                                       harness_calls=["alpha"])},
                     example_coverage=[alpha_full(test_module="examples.test_alpha_object_store")],
                     test_results=[_results("examples", tests)])
        self.assertEqual(by_slug(rep)["alpha"]["examples"]["status"], "example-tests-failing")
        mod = rep["example_attribution"]["modules"]["test_alpha_object_store"]
        self.assertEqual((mod["skills"], mod["status"]), (["alpha"], "attributed"))
        self.assertEqual(set(mod["sources"]), {"harness-record", "harness-call", "slug-constant"})
        self.assertEqual(by_slug(rep)["alpha"]["examples"]["test_modules"], ["test_alpha_object_store"])

    def test_harness_records_attribute_a_module_without_any_static_hint(self):
        tests = [{"id": "examples.test_misc.T.test_x", "outcome": "error", "message": "boom"}]
        rep = report(example_coverage=[_cov("gamma", [0], 1, test_module="examples.test_misc")],
                     test_results=[_results("examples", tests)])
        self.assertEqual(rep["example_attribution"]["modules"]["test_misc"]["sources"], {"harness-record": ["gamma"]})
        self.assertEqual(by_slug(rep)["gamma"]["examples"]["status"], "example-tests-failing")

    def test_module_name_is_only_a_fallback(self):
        tests = [{"id": "examples.test_gamma.T.test_x", "outcome": "passed"}]
        rep = report(test_results=[_results("examples", tests)])
        self.assertEqual(rep["example_attribution"]["modules"]["test_gamma"]["sources"], {"module-name": ["gamma"]})

    def test_unattributed_module_is_listed_and_counts_for_no_skill(self):
        tests = [{"id": "examples.test_misc.T.test_x", "outcome": "failed"},
                 {"id": "examples.test_alpha.T.test_ok", "outcome": "passed"}]
        rep = report(example_coverage=[alpha_full()], test_results=[_results("examples", tests)],
                     example_modules={"test_misc": _scan(), "test_alpha": _scan(slug_constant=["alpha"])})
        mod = rep["example_attribution"]["modules"]["test_misc"]
        self.assertEqual((mod["status"], mod["skills"], mod["outcomes"]["failed"]),
                         ("unattributed-test-module", [], 1))
        self.assertEqual(rep["summary"]["unattributed_test_modules"], ["test_misc"])
        self.assertEqual(by_slug(rep)["alpha"]["examples"]["status"], "executed-and-passed")
        self.assertEqual(rep["tests"]["examples"]["failed"], 1)  # still a suite failure
        self.assertTrue(any(h.startswith("examples tests") for h in rep["summary"]["hard_failures"]))

    def test_explicit_declaration_replaces_the_static_scan(self):
        tests = [{"id": "examples.test_harness.T.test_x", "outcome": "failed"}]
        rep = report(test_results=[_results("examples", tests)],
                     example_modules={"test_harness": _scan(declared=[], harness_calls=["gamma"])})
        mod = rep["example_attribution"]["modules"]["test_harness"]
        self.assertEqual((mod["status"], mod["skills"]), ("declared-no-skill", []))
        self.assertEqual(by_slug(rep)["gamma"]["examples"]["outcome"], "no-attributed-tests")
        self.assertEqual(rep["summary"]["unattributed_test_modules"], [])

    def test_declared_no_skill_excludes_harness_records(self):
        # SKILLS = () (harness self-tests) stays evidence for no skill even when the module ran
        # a real skill's blocks through the harness; those records are listed, not attributed.
        tests = [{"id": "examples.test_harness.T.test_x", "outcome": "failed"}]
        rep = report(example_coverage=[alpha_full(test_module="examples.test_harness")],
                     test_results=[_results("examples", tests)],
                     example_modules={"test_harness": _scan(declared=[], harness_calls=["alpha"])})
        mod = rep["example_attribution"]["modules"]["test_harness"]
        self.assertEqual((mod["status"], mod["skills"], mod["excluded_harness_records"]),
                         ("declared-no-skill", [], ["alpha"]))
        alpha = by_slug(rep)["alpha"]["examples"]
        self.assertEqual((alpha["coverage_status"], alpha["outcome"]), ("all-runnable-blocks-executed",
                                                                        "no-attributed-tests"))
        self.assertTrue(rep["summary"]["hard_failures"])  # the self-test failure still fails the suite

    def test_harness_records_the_declaration_omits_are_added_and_flagged(self):
        rep = report(example_coverage=[_cov("gamma", [0], 1, test_module="examples.test_alpha")],
                     example_modules={"test_alpha": _scan(declared=["alpha"])})
        mod = rep["example_attribution"]["modules"]["test_alpha"]
        self.assertEqual((mod["skills"], mod["undeclared_harness_records"]), (["alpha", "gamma"], ["gamma"]))

    def test_import_and_setupclass_failures_are_attributed(self):
        # Regression: these ids were dropped by the earlier parser, hiding the failure from the skill.
        for tid in ("unittest.loader._FailedTest.examples.test_alpha", "setUpClass (examples.test_alpha.T)"):
            with self.subTest(tid):
                rep = report(example_coverage=[alpha_full()],
                             test_results=[_results("examples", [{"id": tid, "outcome": "error"}])])
                self.assertEqual(by_slug(rep)["alpha"]["examples"]["status"], "example-tests-failing")

    def test_unparseable_test_ids_are_listed(self):
        rep = report(test_results=[_results("examples", [{"id": "weird id", "outcome": "failed"}])])
        self.assertEqual(rep["example_attribution"]["unparseable_test_ids"], ["weird id"])
        self.assertEqual(rep["example_attribution"]["unparseable_test_ids_total"], 1)


class TestHumanReview(unittest.TestCase):
    def test_claim_level_review_never_counts_as_a_reviewed_skill(self):
        claims = [{"skill": "alpha", "verification": "human-review", "status": "current"},
                  {"skill": "beta", "verification": "human-review", "status": "disputed"},
                  {"skill": "gamma", "verification": "human-review", "status": "review-due"}]
        rep = report(claims=claims)
        skills = by_slug(rep)
        self.assertEqual(skills["alpha"]["human_review"],
                         {"status": "claim-level-review-only", "per_skill_review_measured": True,
                          "per_skill_records": 0, "current_records": 0, "latest_current": None,
                          "current_human_reviewed_claims": 1})
        self.assertEqual(skills["beta"]["human_review"]["status"], "not-recorded")
        self.assertEqual(skills["gamma"]["human_review"]["status"], "not-recorded")
        self.assertNotIn("human-reviewed", skills["alpha"]["evidence_tiers"])
        self.assertEqual(rep["summary"]["skills_human_reviewed"], 0)
        self.assertEqual(rep["summary"]["skills_with_claim_level_human_review"], 1)

    def test_current_full_approved_review_counts(self):
        rep = report(reviews=_reviews([_review()]))
        alpha = by_slug(rep)["alpha"]
        self.assertEqual(alpha["human_review"]["status"], "reviewed")
        self.assertEqual(alpha["human_review"]["latest_current"],
                         {"reviewer": "A. Person", "date": "2026-10-28", "scope": "full", "outcome": "approved"})
        self.assertIn("human-reviewed", alpha["evidence_tiers"])
        self.assertEqual(rep["summary"]["skills_human_reviewed"], 1)
        self.assertEqual(rep["checks"]["reviews"]["status"], "pass")

    def test_review_of_another_sha_is_stale(self):
        rep = report(reviews=_reviews([_review(skill_sha256="0" * 64)]))
        self.assertEqual(by_slug(rep)["alpha"]["human_review"]["status"], "review-stale")
        self.assertEqual(rep["summary"]["skills_human_review_stale"], ["alpha"])
        self.assertEqual(rep["summary"]["skills_human_reviewed"], 0)

    def test_partial_scope_and_requested_changes_are_not_reviewed(self):
        cases = {"partially-reviewed": [_review(scope="code")],
                 "review-changes-requested": [_review(), _review(outcome="changes-requested", reviewer="B")],
                 "reviewed": [_review(date="2026-10-29"), _review(outcome="rejected", reviewer="B")]}
        for status, records in cases.items():
            with self.subTest(status):
                rep = report(reviews=_reviews(records))
                self.assertEqual(by_slug(rep)["alpha"]["human_review"]["status"], status)
                self.assertEqual(rep["summary"]["skills_human_reviewed"], int(status == "reviewed"))

    def test_invalid_reviews_file_is_a_hard_failure_and_counts_nothing(self):
        rep = report(reviews=_reviews([_review(), _review(skill="no-such-skill")]))
        self.assertEqual(rep["checks"]["reviews"]["status"], "fail")
        self.assertTrue(any(h.startswith("docs/reviews.json") for h in rep["summary"]["hard_failures"]))
        self.assertEqual(by_slug(rep)["alpha"]["human_review"]["status"], "not-recorded")
        self.assertIsNone(rep["summary"]["skills_human_reviewed"])
        self.assertIn("per-skill-human-review", [c["check"] for c in rep["skipped_checks"]])
        unreadable = {"path": "docs/reviews.json", "present": True, "doc": None, "error": "JSONDecodeError: x"}
        self.assertEqual(report(reviews=unreadable)["checks"]["reviews"]["status"], "fail")

    def test_missing_reviews_file_is_listed_as_skipped(self):
        rep = report(reviews=_reviews(present=False))
        self.assertEqual(rep["checks"]["reviews"]["status"], "not-run")
        self.assertIsNone(rep["summary"]["skills_human_reviewed"])
        self.assertFalse(rep["summary"]["hard_failures"])
        self.assertIn("not found", {c["check"]: c["reason"] for c in rep["skipped_checks"]}["per-skill-human-review"])
        self.assertFalse(by_slug(rep)["alpha"]["human_review"]["per_skill_review_measured"])

    def test_validate_reviews_rejects_malformed_records(self):
        known = {"alpha"}
        bad = {"unknown key": _review(extra="x"), "missing key": {k: v for k, v in _review().items() if k != "scope"},
               "short sha": _review(skill_sha256="abc"), "upper-case sha": _review(skill_sha256=sha("alpha").upper()),
               "bad date": _review(date="28-10-2026"), "impossible date": _review(date="2026-02-30"),
               "future date": _review(date="2026-11-05"), "unknown skill": _review(skill="beta"),
               "bad scope": _review(scope="most"), "bad outcome": _review(outcome="lgtm"),
               "empty reviewer": _review(reviewer=" "), "non-string": _review(reviewer=7)}
        for label, rec in bad.items():
            with self.subTest(label):
                self.assertTrue(qr.validate_reviews(_reviews_doc([rec]), known, "2026-10-29"), label)
        self.assertEqual(qr.validate_reviews(_reviews_doc([_review(date="2026-10-30")]), known, "2026-10-29"), [],
                         "one day ahead is allowed (writer in a zone ahead of UTC)")
        self.assertTrue(qr.validate_reviews(_reviews_doc([_review(), _review()]), known, "2026-10-29"))
        self.assertTrue(qr.validate_reviews({"schema": "x"}, known, None))
        self.assertTrue(qr.validate_reviews({**_reviews_doc([]), "schema_version": 2}, known, None))
        self.assertTrue(qr.validate_reviews({**_reviews_doc([]), "extra": 1}, known, None))
        self.assertTrue(qr.validate_reviews({**_reviews_doc([]), "reviews": {}}, known, None))
        self.assertEqual(qr.validate_reviews(_reviews_doc([_review(notes="read twice")]), known, None), [])

    def test_shipped_reviews_file_is_valid(self):
        info = qr.load_reviews(REPO_ROOT)
        self.assertTrue(info["present"])
        self.assertIsNone(info["error"])
        known = {d.name for d in testutil.sl.iter_skill_dirs(REPO_ROOT)}
        self.assertEqual(qr.validate_reviews(info["doc"], known, "2999-01-01"), [])


class TestChecks(unittest.TestCase):
    def test_router_regression_and_benchmark_hash_mismatch_fail(self):
        rep = report(eval_router=_tool(json.dumps(_router_doc(test_task=0.7, test_far=0.1))))
        regs = rep["checks"]["eval_router"]["baseline"]["regressions"]
        self.assertIn("test task_accuracy 0.7 < baseline 0.8", regs)
        self.assertIn("test false_activation_rate 0.1 > baseline 0.0", regs)
        self.assertEqual(rep["checks"]["eval_router"]["status"], "fail")
        self.assertTrue(any(h.startswith("eval_router") for h in rep["summary"]["hard_failures"]))

        rep = report(benchmark_sha256="other")
        self.assertFalse(rep["checks"]["eval_router"]["baseline"]["benchmark_sha256_matches"])
        self.assertEqual(rep["checks"]["eval_router"]["status"], "fail")

    def test_router_without_json_reports_its_output(self):
        res = {"command": "python scripts/eval_router.py", "exit_code": 2, "stdout": "",
               "stderr": "benchmark error: x\nbenchmark error: y\n"}
        router = report(eval_router=res)["checks"]["eval_router"]
        self.assertEqual((router["status"], router["exit_code"]), ("error", 2))
        self.assertEqual(router["output_tail"], ["benchmark error: x", "benchmark error: y"])

    def test_router_at_baseline_passes_and_counts_misrouted(self):
        router = report()["checks"]["eval_router"]
        self.assertEqual(router["status"], "pass")
        self.assertEqual(router["misrouted"], {"dev": 1, "test": 1})
        self.assertNotIn("counts", router["splits"]["test"])

    def test_evals_are_reported_as_offline_only(self):
        ev = report()["checks"]["validate_evals"]
        self.assertEqual(ev["status"], "pass")
        self.assertFalse(ev["model_graded"])
        self.assertIn("No model output was graded", ev["note"])

    def test_stale_freshness_and_contract_failure_are_hard_failures(self):
        hard = report(check_freshness=_tool(json.dumps({"counts": {"STALE": 2}, "findings": []}), code=1),
                      check_contract=_tool("CONTRACT ERROR: x", code=1))["summary"]["hard_failures"]
        self.assertIn("check_freshness: 2 STALE finding(s)", hard)
        self.assertIn("check_contract: exit 1", hard)

    def test_tool_without_json_is_reported(self):
        rep = report(validate_evals=_tool("Traceback ...", code=1))
        self.assertEqual(rep["checks"]["validate_evals"]["status"], "fail")
        self.assertIn("validate_evals.py did not produce JSON", rep["summary"]["hard_failures"])

    def test_report_schema_is_versioned(self):
        rep = report()
        self.assertEqual((rep["schema"], rep["schema_version"]), (qr.REPORT_SCHEMA, 2))
        self.assertEqual(qr.RESULTS_SCHEMA_VERSION, 1)
        ids = {item["id"] for item in rep["known_limitations"]["items"]}
        self.assertTrue({"human-review-records", "coverage-provenance", "example-attribution"} <= ids)
        self.assertNotIn("no-human-review-records", ids)


class TestSkippedChecks(unittest.TestCase):
    def test_always_skipped_checks_are_listed_with_reasons(self):
        skipped = {c["check"]: c["reason"] for c in report()["skipped_checks"]}
        for check in ("unit-tests", "example-tests", "network", "model-evals", "github-actions", "other-platforms",
                      "generated-artifacts", "executable-evals"):
            self.assertIn(check, skipped)
            self.assertTrue(skipped[check])
        self.assertIn("not on PATH", skipped["claude-cli"])
        self.assertNotIn("per-skill-human-review", skipped)  # an empty, valid reviews file was read

    def test_unit_run_without_slow_tests_lists_installer_tests_as_skipped(self):
        rep = report(test_results=[_results("unit", [{"id": "t.A.test_x", "outcome": "passed"}])], claude_cli=True)
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
        unit = report(test_results=[_results("unit", tests)])["tests"]["unit"]
        self.assertEqual((unit["total"], unit["passed"], unit["skipped"]), (4, 1, 3))
        self.assertEqual(unit["skip_reasons"], [{"reason": "PyYAML not installed", "count": 2},
                                                {"reason": "claude CLI not on PATH", "count": 1}])

    def test_invalid_results_file_is_a_hard_failure(self):
        rep = report(test_results=[{"schema": "something-else"}])
        self.assertIn("an ingested test-results file is invalid", rep["summary"]["hard_failures"])
        self.assertIsNone(rep["tests"]["unit"])


class TestDeterminism(unittest.TestCase):
    def test_only_generated_at_differs(self):
        raw = make_raw(example_coverage=[_cov("alpha", [1, 0], 3)],
                       test_results=[_results("examples", [{"id": "examples.test_alpha.T.t", "outcome": "passed"}])])
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


class TestHarnessCoverageProvenance(unittest.TestCase):
    """tests/examples/harness.py writes what the report needs to tie coverage to a tree and a test."""

    SLUG = "model-deployment"

    def test_record_has_provenance_and_the_calling_test(self):
        rec = harness.coverage_record(self.SLUG, [1, 0, 1], 2)
        self.assertEqual((rec["schema"], rec["schema_version"]), (qr.COVERAGE_SCHEMA, 2))
        self.assertIn(rec["schema_version"], qr.COVERAGE_SCHEMA_VERSIONS)
        self.assertEqual((rec["skill"], rec["blocks"], rec["total_blocks"]), (self.SLUG, [0, 1], 2))
        expected = hashlib.sha256((REPO_ROOT / self.SLUG / "SKILL.md").read_bytes().replace(b"\r\n", b"\n"))
        self.assertEqual(rec["skill_sha256"], expected.hexdigest())
        for key in ("git_revision", "git_dirty", "git_untracked_files", "python", "platform", "recorded_at",
                    "env"):
            self.assertIn(key, rec)
        self.assertRegex(rec["recorded_at"], r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ$")
        self.assertEqual(rec["python"], ".".join(map(str, sys.version_info[:3])))
        self.assertEqual(rec["test_id"], self.id())
        self.assertEqual(rec["test_module"], type(self).__module__)

    def test_setupclass_and_non_test_callers(self):
        seen = {}

        class Probe(unittest.TestCase):
            @classmethod
            def setUpClass(cls):
                seen["class"] = harness.calling_test()

            def test_x(self):
                seen["method"] = harness.calling_test()

        unittest.TestSuite([Probe("test_x")]).run(unittest.TestResult())
        self.assertTrue(seen["class"]["test_id"].endswith("Probe"))
        self.assertTrue(seen["method"]["test_id"].endswith("Probe.test_x"))

        def helper():
            return harness.calling_test()

        # Called from this test (through a helper), the enclosing test is still found.
        self.assertEqual(helper()["test_id"], self.id())

    def test_harness_records_are_accepted_and_attributed_by_the_report(self):
        with tempfile.TemporaryDirectory() as tmp:
            cov = Path(tmp) / "cov.jsonl"
            with mock.patch.dict(os.environ, {harness.COVERAGE_ENV: str(cov)}):
                harness._record_coverage(self.SLUG, [0, 1], len(harness.python_blocks(self.SLUG)))
            lines = cov.read_text(encoding="utf-8").splitlines()
        inventory = {s["skill"]: s for s in qr._skill_inventory(REPO_ROOT)}
        parsed = qr._parse_coverage(lines, inventory)
        self.assertEqual(parsed["executed"], {self.SLUG: {0, 1}})
        self.assertEqual((parsed["stale"], parsed["unattributable"], parsed["bad"]), ({}, {}, 0))
        # Run from tests/ as top level, this module is "test_quality_report": attributed to the skill it ran.
        self.assertEqual(parsed["modules"], {"test_quality_report": {self.SLUG}})
        self.assertEqual(parsed["provenance"]["records_without_test"], 0)

    def test_module_names_from_every_invocation_style_are_attributed(self):
        # discover -s tests (test_x), -t tests (examples.test_x) and from the repo root (tests.examples.test_x).
        for name in ("test_nlp_tasks", "examples.test_nlp_tasks", "tests.examples.test_nlp_tasks"):
            with self.subTest(name=name):
                self.assertEqual(qr._coverage_module(name), "test_nlp_tasks")
        for name in (None, "harness", "tests.examples.sub.test_x", "examples.helpers"):
            with self.subTest(name=name):
                self.assertIsNone(qr._coverage_module(name))

    def test_slow_examples_run_under_either_variable(self):
        cases = {(None, None): False, ("1", None): True, (None, "1"): True, ("0", "0"): False}
        for (slow, require), enabled in cases.items():
            env = {k: v for k, v in ((harness.SLOW_ENV, slow), (harness.REQUIRE_ENV, require)) if v is not None}
            with self.subTest(env=env), mock.patch.dict(os.environ, env):
                for var in (harness.SLOW_ENV, harness.REQUIRE_ENV):
                    if var not in env:
                        os.environ.pop(var, None)
                self.assertEqual(harness.slow_examples_enabled(), enabled)

                @harness.slow("MLflow setup")
                class T(unittest.TestCase):
                    def test_x(self):
                        pass

                result = unittest.TestResult()
                unittest.defaultTestLoader.loadTestsFromTestCase(T).run(result)
                self.assertEqual((result.testsRun - len(result.skipped), len(result.skipped)),
                                 (1, 0) if enabled else (0, 1))
                if not enabled:
                    self.assertIn(f"set {harness.SLOW_ENV}=1", result.skipped[0][1])


@unittest.skipUnless(GIT, "git not found on PATH")
class TestGitRevisionOnlyForTheTreeItself(unittest.TestCase):
    """An extracted release inside another repository must not report that repository's HEAD."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self.tmp.cleanup)
        self.top = Path(self.tmp.name).resolve() / "outer"
        (self.top / "inner").mkdir(parents=True)
        env = {**os.environ, "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull}
        for args in (["init", "-q"], ["-c", "user.name=t", "-c", "user.email=t@example.invalid",
                                      "commit", "-q", "--allow-empty", "-m", "x"]):
            subprocess.run([GIT, *args], cwd=self.top, check=True, capture_output=True, timeout=60, env=env)

    def commit_file(self, name):
        (self.top / name).write_text("v1", encoding="utf-8")
        env = {**os.environ, "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull}
        for args in (["add", name], ["-c", "user.name=t", "-c", "user.email=t@example.invalid",
                                     "commit", "-q", "-m", "add " + name]):
            subprocess.run([GIT, *args], cwd=self.top, check=True, capture_output=True, timeout=60, env=env)

    def test_quality_report_git_state(self):
        state = qr._git_state(self.top)
        self.assertRegex(state["git_revision"] or "", r"^[0-9a-f]{40}$")
        self.assertEqual((state["git_dirty"], state["git_untracked_files"]), (False, 0))
        self.assertEqual(qr._git_state(self.top / "inner"),
                         {"git_revision": None, "git_dirty": None, "git_untracked_files": None})
        # Untracked files (CI logs, coverage output) are counted, not folded into git_dirty.
        (self.top / "untracked.txt").write_text("x", encoding="utf-8")
        state = qr._git_state(self.top)
        self.assertEqual((state["git_dirty"], state["git_untracked_files"]), (False, 1))
        self.commit_file("tracked.txt")
        (self.top / "tracked.txt").write_text("changed", encoding="utf-8")
        state = qr._git_state(self.top)
        self.assertEqual((state["git_dirty"], state["git_untracked_files"]), (True, 1))

    def test_harness_run_provenance(self):
        harness.run_provenance.cache_clear()
        self.addCleanup(harness.run_provenance.cache_clear)
        with mock.patch.object(harness, "REPO_ROOT", self.top / "inner"):
            self.assertIsNone(harness.run_provenance()["git_revision"])
        harness.run_provenance.cache_clear()
        with mock.patch.object(harness, "REPO_ROOT", self.top):
            prov = harness.run_provenance()
        self.assertRegex(prov["git_revision"] or "", r"^[0-9a-f]{40}$")
        self.assertEqual((prov["git_dirty"], prov["git_untracked_files"]), (False, 0))
        # The coverage file the harness writes is untracked but never counted; other files are.
        cov = self.top / "example-coverage.jsonl"
        cov.write_text("", encoding="utf-8")
        (self.top / "examples-pip-freeze.txt").write_text("x\n", encoding="utf-8")
        harness.run_provenance.cache_clear()
        with mock.patch.object(harness, "REPO_ROOT", self.top), \
                mock.patch.dict(os.environ, {harness.COVERAGE_ENV: str(cov)}):
            prov = harness.run_provenance()
        self.assertEqual((prov["git_dirty"], prov["git_untracked_files"]), (False, 1))


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


class TestRunExamplesEnvironment(unittest.TestCase):
    """--run-examples passes the CI variables to the example interpreter when asked."""

    def gather_env(self, **flags):
        seen = {}

        def fake_run_tool(root, args, timeout=600, python=None, env=None):
            if "--suite" in args and "examples" in args:
                seen.update(env or {})
            return {"command": "python " + " ".join(args), "exit_code": 0, "stdout": "", "stderr": ""}

        args = argparse.Namespace(today="2026-10-29", check_generated=False, test_results=None,
                                  example_coverage=None, run_tests=False, include_slow=False,
                                  run_examples=sys.executable, slow_examples=False, require_example_deps=False,
                                  timeout=60)
        for k, v in flags.items():
            setattr(args, k, v)
        with mock.patch.object(qr, "run_tool", side_effect=fake_run_tool):
            raw = qr.gather(REPO_ROOT, args)
        return seen, raw

    def test_require_example_deps_flag(self):
        env, raw = self.gather_env(require_example_deps=True)
        self.assertEqual(env.get("ML_AI_SKILLS_REQUIRE_EXAMPLE_DEPS"), "1")
        self.assertIn(harness.COVERAGE_ENV, env)
        self.assertTrue(raw["options"]["require_example_deps"])
        env, raw = self.gather_env()
        self.assertNotIn("ML_AI_SKILLS_REQUIRE_EXAMPLE_DEPS", env)
        self.assertNotIn("ML_AI_SKILLS_SLOW_EXAMPLES", env)
        self.assertEqual(set(raw["example_modules"]),
                         {p.stem for p in (REPO_ROOT / "tests" / "examples").glob("test_*.py")})
        self.assertEqual(raw["reviews"]["present"], True)


class TestRealRepoStaticReport(unittest.TestCase):
    def test_static_report_on_this_repo(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "report.json"
            p = subprocess.run([sys.executable, str(REPO_ROOT / "scripts" / "quality_report.py"), "--out", str(out),
                                "--timestamp", "2026-01-01T00:00:00Z"], cwd=REPO_ROOT, capture_output=True,
                               text=True, timeout=600)
            self.assertIn(p.returncode, (0, 1), p.stderr)  # 1 = a gate reported a hard failure (still a report)
            rep = json.loads(out.read_text(encoding="utf-8"))
        self.assertEqual((rep["schema"], rep["schema_version"], rep["generated_at"]),
                         (qr.REPORT_SCHEMA, qr.REPORT_SCHEMA_VERSION, "2026-01-01T00:00:00Z"))
        self.assertEqual(len(rep["skills"]), len(testutil.sl.iter_skill_dirs(REPO_ROOT)))
        for s in rep["skills"]:
            self.assertEqual(s["examples"]["status"] in ("not-measured", "no-runnable-blocks"), True, s["skill"])
            self.assertIn(s["human_review"]["status"], ("not-recorded", "claim-level-review-only", "reviewed",
                                                        "partially-reviewed", "review-stale",
                                                        "review-changes-requested"))
            self.assertIn(s["structural"]["status"], ("valid", "invalid"), s["skill"])
            self.assertRegex(s["skill_sha256"], r"^[0-9a-f]{64}$")
        self.assertEqual(rep["checks"]["reviews"]["status"], "pass")
        self.assertEqual(rep["summary"]["skills_human_reviewed"],
                         sum(1 for s in rep["skills"] if s["human_review"]["status"] == "reviewed"))
        for check in ("validate_skills", "validate_evals", "eval_router", "check_freshness", "check_claims",
                      "check_contract", "generate_schema"):
            self.assertIn(rep["checks"][check]["status"], ("pass", "fail"), check)
        self.assertEqual(rep["checks"]["check_generated"]["status"], "not-run")
        # Every example module is accounted for; none is silently missing.
        modules = rep["example_attribution"]["modules"]
        self.assertEqual(set(modules), {p.stem for p in (REPO_ROOT / "tests" / "examples").glob("test_*.py")})
        known = {s["skill"] for s in rep["skills"]}
        for path in sorted((REPO_ROOT / "tests" / "examples").glob("test_*.py")):
            entry, scan = modules[path.stem], qr.scan_example_module(path.read_text(encoding="utf-8"), known)
            with self.subTest(module=path.stem):
                self.assertIn(entry["status"], ("attributed", "declared-no-skill", "unattributed-test-module"))
                # Whatever the module loads or declares is what it is attributed to, whatever its name.
                expected = set(scan["declared"]) if scan["declared"] is not None else (
                    set(scan["harness_calls"]) | set(scan["slug_constant"]))
                self.assertTrue(expected <= set(entry["skills"]), (expected, entry))


if __name__ == "__main__":
    unittest.main()
