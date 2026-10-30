"""Tests for scripts/check_claims.py (docs/claims.json validation). Offline."""
import contextlib
import datetime as dt
import io
import json
import tempfile
import unittest
from pathlib import Path

import testutil  # noqa: F401  (inserts scripts/ and schema/ onto sys.path)
from testutil import make_skill

import check_claims as cc
import check_freshness as cf

TODAY = dt.date(2026, 10, 27)


def make_policy(level="REVIEW_DUE", claim="tooling-claim"):
    return cf.policy_from_dict({
        "policy_version": 1, "default_max_age_days": 180,
        "domain_max_age_days": {"llm": {"max_age_days": 90, "reason": "fast"}},
        "known_stale_patterns": [{"id": "dead", "pattern": "x", "level": level, "message": "m", "claim": claim}],
    })


# REVIEW_DUE level so an unverified backing claim is fine; the STALE cases are tested separately.
POLICY = make_policy()
SKILLS = [make_skill("rag-pipeline", domain="llm"), make_skill("data-preprocessing", domain="classical-ml")]


def good_claim(**overrides):
    claim = {
        "id": "faiss-pads-minus-one",
        "skill": "rag-pipeline",
        "claim": "IndexFlatIP.search pads ids with -1 when k > ntotal",
        "verification": "runtime-test",
        "evidence": "tests/test_check_claims.py::TestCheckClaims::test_good_registry_is_valid",
        "source_url": "https://github.com/facebookresearch/faiss/wiki/FAQ",
        "source_accessed": "2026-10-27",
        "applies_to": {"package": "faiss-cpu", "api": "IndexFlatIP.search", "version": "1.15.1"},
        "reviewer": None,
        "status": "current",
        "last_reviewed": "2026-10-27",
    }
    claim.update(overrides)
    return claim


def tooling_claim(**overrides):
    return good_claim(id="tooling-claim", skill=None, verification="unverified", status="unverified",
                      evidence="model knowledge", source_url=None, source_accessed=None, **overrides)


def registry(*claims):
    return {"registry_version": 1, "claims": list(claims)}


class TestCheckClaims(unittest.TestCase):
    def check(self, data, today=TODAY):
        return cc.check_registry(data, SKILLS, POLICY, today, testutil.REPO_ROOT)

    def error_codes(self, *claims):
        errors, _ = self.check(registry(*claims, tooling_claim()))
        return {e["code"] for e in errors}

    def test_good_registry_is_valid(self):
        errors, findings = self.check(registry(good_claim(), tooling_claim()))
        self.assertEqual(errors, [])
        self.assertEqual(findings, [])

    def test_human_review_claim_is_valid_with_reviewer_and_source(self):
        c = good_claim(id="hr", verification="human-review", reviewer="A. Reviewer", evidence="read the FAQ entry")
        self.assertEqual(self.error_codes(c), set())

    def test_schema_errors(self):
        c = good_claim()
        del c["evidence"]
        self.assertIn("schema-required", self.error_codes(c))
        self.assertIn("schema-enum", self.error_codes(good_claim(status="fine")))
        self.assertIn("schema-enum", self.error_codes(good_claim(verification="vibes")))
        self.assertIn("schema-additionalProperties", self.error_codes(good_claim(extra=1)))
        self.assertIn("schema-pattern", self.error_codes(good_claim(id="Bad Id")))
        self.assertIn("schema-pattern", self.error_codes(good_claim(source_url="ftp://x")))
        self.assertIn("schema-required", self.error_codes(good_claim(applies_to={"api": "x"})))
        errors, _ = self.check({"claims": []})
        self.assertTrue(errors)

    def test_static_pattern_is_not_verification(self):
        codes = self.error_codes(good_claim(verification="static-pattern"))
        self.assertEqual(codes, {"static-pattern-is-not-verification"})

    def test_unknown_skill(self):
        self.assertIn("unknown-skill", self.error_codes(good_claim(skill="no-such-skill")))

    def test_bad_and_future_dates(self):
        self.assertIn("schema-format", self.error_codes(good_claim(last_reviewed="2026-02-30")))
        self.assertIn("schema-format", self.error_codes(good_claim(source_accessed="27/10/2026")))
        # one day of slack: a claim recorded "tomorrow" in a timezone ahead of UTC is not an error
        self.assertEqual(self.error_codes(good_claim(last_reviewed="2026-10-28", source_accessed="2026-10-28")),
                         set())
        self.assertIn("date-in-future", self.error_codes(good_claim(last_reviewed="2026-10-29")))
        self.assertIn("date-in-future", self.error_codes(good_claim(source_accessed="2026-10-29")))
        self.assertIn("source-without-access-date", self.error_codes(good_claim(source_accessed=None)))

    def test_runtime_test_evidence_must_exist(self):
        self.assertIn("runtime-test-evidence", self.error_codes(good_claim(evidence="ran it locally once")))
        self.assertIn("runtime-test-evidence", self.error_codes(good_claim(evidence="tests/test_nope.py")))
        self.assertIn("runtime-test-evidence",
                      self.error_codes(good_claim(evidence="tests/test_check_claims.py::NoSuchClass")))
        self.assertIn("runtime-test-evidence", self.error_codes(good_claim(evidence="tests/../../outside.py")))
        self.assertEqual(self.error_codes(good_claim(evidence="tests/test_check_claims.py::TestRepoRegistry")), set())

    def test_runtime_test_method_must_belong_to_the_named_class(self):
        # test_good_registry_is_valid is a method of TestCheckClaims, not TestRepoRegistry
        wrong = "tests/test_check_claims.py::TestRepoRegistry::test_good_registry_is_valid"
        errors, _ = self.check(registry(good_claim(evidence=wrong), tooling_claim()))
        self.assertEqual([e["code"] for e in errors], ["runtime-test-evidence"])
        self.assertIn("test_good_registry_is_valid", errors[0]["message"])
        # a module-level helper is not a ::method of a class
        self.assertIn("runtime-test-evidence", self.error_codes(
            good_claim(evidence="tests/test_check_claims.py::TestCheckClaims::good_claim")))
        # an undefined method in a real class
        self.assertIn("runtime-test-evidence", self.error_codes(
            good_claim(evidence="tests/test_check_claims.py::TestCheckClaims::NoSuchMethod")))

    def test_runtime_test_evidence_must_be_a_test_module(self):
        for ev in ("tests/testutil.py", "tests/examples/harness.py",
                   "see tests/testutil.py::make_skill (not actually run)"):
            with self.subTest(evidence=ev):
                self.assertIn("runtime-test-evidence", self.error_codes(good_claim(evidence=ev)))
        self.assertIn("version-required", self.error_codes(
            good_claim(applies_to={"package": "faiss-cpu", "api": "x", "version": None})))

    def test_kind_specific_requirements(self):
        self.assertIn("reviewer-required", self.error_codes(good_claim(verification="human-review",
                                                                       evidence="read it")))
        self.assertIn("source-required", self.error_codes(good_claim(
            verification="automated-doc-check", source_url=None, source_accessed=None)))
        self.assertIn("package-required", self.error_codes(good_claim(
            verification="local-version-check", applies_to={"package": None, "api": "x", "version": "1"})))

    def test_status_must_agree_with_verification(self):
        self.assertIn("status-mismatch", self.error_codes(good_claim(status="unverified")))
        self.assertIn("status-mismatch", self.error_codes(good_claim(
            id="u", verification="unverified", status="current", evidence="none")))

    def test_duplicate_ids(self):
        self.assertIn("duplicate-id", self.error_codes(good_claim(), good_claim()))

    def test_stale_pattern_backed_by_unverified_claim_is_review_due(self):
        policy = make_policy(level="STALE")
        errors, findings = cc.check_registry(registry(good_claim(), tooling_claim()), SKILLS, policy, TODAY,
                                             testutil.REPO_ROOT)
        self.assertEqual(errors, [])
        self.assertEqual([(f["claim"], f["code"]) for f in findings], [("tooling-claim", "stale-pattern-unverified")])
        disputed = good_claim(id="tooling-claim", skill=None, status="disputed")
        _, findings = cc.check_registry(registry(disputed), SKILLS, policy, TODAY, testutil.REPO_ROOT)
        self.assertIn("stale-pattern-unverified", {f["code"] for f in findings})
        # automated-doc-check / local-version-check are recorded kinds but say nothing about API
        # behaviour, so they cannot back a STALE verdict either (same rule as check_freshness)
        for kind in ("automated-doc-check", "local-version-check"):
            with self.subTest(verification=kind):
                weak = {"id": "tooling-claim", "verification": kind, "status": "current"}
                _, findings = cc.check_registry(registry(dict(good_claim(id="tooling-claim", skill=None),
                                                              **weak)),
                                                SKILLS, policy, TODAY, testutil.REPO_ROOT)
                self.assertIn("stale-pattern-unverified", {f["code"] for f in findings})
                self.assertIsNotNone(cf.stale_backing_problem(policy.patterns[0], {"tooling-claim": weak}))
        # backed by a verified (runtime-test) claim: nothing to report
        verified = good_claim(id="tooling-claim", skill=None)
        errors, findings = cc.check_registry(registry(verified), SKILLS, policy, TODAY, testutil.REPO_ROOT)
        self.assertEqual((errors, findings), ([], []))
        # a verified claim whose review clock ran out keeps its STALE verdict (only the clock is reported),
        # matching check_freshness.stale_backing_problem
        overdue = good_claim(id="tooling-claim", skill=None, status="review-due")
        _, findings = cc.check_registry(registry(overdue), SKILLS, policy, TODAY, testutil.REPO_ROOT)
        self.assertEqual([f["code"] for f in findings], ["status-review-due"])
        self.assertIsNone(cf.stale_backing_problem(policy.patterns[0], {"tooling-claim": overdue}))
        self.assertIsNotNone(cf.stale_backing_problem(policy.patterns[0], {"tooling-claim": tooling_claim()}))

    def test_stale_pattern_without_claim_is_review_due(self):
        _, findings = cc.check_registry(registry(good_claim()), SKILLS, make_policy(level="STALE", claim=None),
                                        TODAY, testutil.REPO_ROOT)
        self.assertEqual([f["code"] for f in findings], ["stale-pattern-without-claim"])
        _, findings = cc.check_registry(registry(good_claim()), SKILLS, make_policy(claim=None), TODAY,
                                        testutil.REPO_ROOT)
        self.assertEqual(findings, [])  # REVIEW_DUE patterns may be unbacked: they never fail CI

    def test_policy_claim_reference_must_exist(self):
        errors, _ = self.check(registry(good_claim()))  # no tooling-claim
        self.assertIn("policy-claim-missing", {e["code"] for e in errors})

    def test_review_due_from_policy_age(self):
        llm_limit = TODAY - dt.timedelta(days=90)
        at_limit = good_claim(last_reviewed=llm_limit.isoformat(), source_accessed=llm_limit.isoformat())
        _, findings = self.check(registry(at_limit, tooling_claim()))
        self.assertEqual(findings, [])
        over = (llm_limit - dt.timedelta(days=1)).isoformat()
        _, findings = self.check(registry(good_claim(last_reviewed=over, source_accessed=over), tooling_claim()))
        self.assertEqual([f["code"] for f in findings], ["review-overdue"])
        self.assertIn("limit 90", findings[0]["message"])
        # skill-less claims use the default (180 days)
        old = (TODAY - dt.timedelta(days=181)).isoformat()
        _, findings = self.check(registry(good_claim(), tooling_claim(last_reviewed=old)))
        self.assertEqual([f["claim"] for f in findings], ["tooling-claim"])

    def test_recorded_review_due_and_disputed_are_reported(self):
        _, findings = self.check(registry(good_claim(status="disputed"), tooling_claim()))
        self.assertEqual([f["code"] for f in findings], ["status-disputed"])


class TestRepoRegistry(unittest.TestCase):
    def test_docs_claims_json_is_valid(self):
        skills = testutil.sl.load_all_skills()
        data = json.loads(cc.DEFAULT_CLAIMS_PATH.read_text(encoding="utf-8"))
        policy = cf.load_policy(cf.DEFAULT_POLICY_PATH, skills)
        # Same rule CI applies (check_registry allows one day of future slack), so a claim recorded
        # today in a zone ahead of UTC does not fail on a UTC runner.
        errors, _ = cc.check_registry(data, skills, policy, dt.date.today())
        self.assertEqual(errors, [])
        ids = {c["id"] for c in data["claims"]}
        self.assertIn("faiss-indexflatip-pads-missing-neighbours", ids)

    def test_cli_exit_codes(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "claims.json"
            path.write_text(json.dumps(registry(good_claim(skill="no-such-skill"))), encoding="utf-8")
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(cc.main(["--claims", str(path)]), 1)
            path.write_text("{not json", encoding="utf-8")
            with contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(cc.main(["--claims", str(path)]), 2)
        with contextlib.redirect_stdout(io.StringIO()) as out:
            self.assertEqual(cc.main(["--json"]), 0)
        self.assertIn("by_verification", out.getvalue())

    def test_text_output_reports_stale_pattern_without_claim(self):
        # That finding has claim None; the text report once crashed formatting it (TypeError, exit 1)
        # although REVIEW_DUE findings are informational without --strict.
        policy = json.loads(cf.DEFAULT_POLICY_PATH.read_text(encoding="utf-8"))
        policy["known_stale_patterns"].append(
            {"id": "x-test", "pattern": "zzzqqq", "level": "STALE", "message": "m", "claim": None})
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "policy.json"
            path.write_text(json.dumps(policy), encoding="utf-8")
            with contextlib.redirect_stdout(io.StringIO()) as out:
                code = cc.main(["--policy", str(path)])
        line = next(ln for ln in out.getvalue().splitlines() if "'x-test'" in ln)
        self.assertEqual(code, 0, out.getvalue())
        self.assertTrue(line.startswith("REVIEW_DUE -"), line)
        self.assertIn("review-due finding(s)", out.getvalue())

    def test_strict_fails_on_review_due(self):
        far_future = "2030-01-01"  # every seeded claim is older than any policy limit by then
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(cc.main(["--today", far_future]), 0)
            self.assertEqual(cc.main(["--today", far_future, "--strict"]), 1)


if __name__ == "__main__":
    unittest.main()
