"""Tests for scripts/check_freshness.py: policy, PEP 440 subset, constraint parsing, local-version check."""
import contextlib
import copy
import datetime as dt
import importlib.util
import io
import json
import unittest

import testutil  # noqa: F401  (inserts scripts/ and schema/ onto sys.path)
from testutil import make_skill

import check_freshness as cf

TODAY = dt.date(2026, 10, 27)


def policy_dict(**overrides):
    base = {
        "policy_version": 1,
        "default_max_age_days": 180,
        "domain_max_age_days": {"llm": {"max_age_days": 90, "reason": "fast"},
                                "foundations": {"max_age_days": 365, "reason": "slow"}},
        "capability_max_age_days": {"mcp-tool-integration": {"max_age_days": 60, "reason": "spec churn"},
                                    "agent-loop-design": {"max_age_days": 75, "reason": "x"}},
        "skill_max_age_days": {"pinned-skill": {"max_age_days": 30, "reason": "pinned"}},
        "ignore_imports": {"src": "placeholder"},
        "known_stale_patterns": [
            {"id": "dead-api", "pattern": r"sklearn\.cross_validation", "level": "STALE",
             "message": "gone", "claim": None},
            {"id": "maybe-dead", "pattern": r"\bgpt-4-\d{4}\b", "level": "REVIEW_DUE",
             "message": "check snapshot", "claim": None},
        ],
    }
    base.update(overrides)
    return base


POLICY = cf.policy_from_dict(policy_dict())


def days_ago(n: int) -> str:
    return (TODAY - dt.timedelta(days=n)).isoformat()


def codes(findings, level=None):
    return [f["code"] for f in findings if level is None or f["level"] == level]


class TestPolicyResolution(unittest.TestCase):
    def test_precedence_skill_capability_domain_default(self):
        self.assertEqual(POLICY.max_age_for("pinned-skill", "llm", ["mcp-tool-integration"])[0], 30)
        self.assertEqual(POLICY.max_age_for("x", "llm", ["mcp-tool-integration"])[0], 60)
        self.assertEqual(POLICY.max_age_for("x", "llm", [])[0], 90)
        self.assertEqual(POLICY.max_age_for("x", "foundations", [])[0], 365)
        self.assertEqual(POLICY.max_age_for("x", "classical-ml", [])[0], 180)
        self.assertEqual(POLICY.max_age_for(None)[0], 180)

    def test_strictest_matching_capability_wins(self):
        days, rule = POLICY.max_age_for("x", "llm", ["agent-loop-design", "mcp-tool-integration"])
        self.assertEqual((days, rule), (60, "capability override 'mcp-tool-integration'"))

    def test_boundary_day_counts(self):
        def review_codes(slug, domain, age):
            s = make_skill(slug, domain=domain, last_verified=days_ago(age))
            return codes(cf.check_skill(s, TODAY, POLICY), "REVIEW_DUE")

        self.assertNotIn("review-overdue", review_codes("a", "llm", 90))   # exactly at the limit: still fresh
        self.assertIn("review-overdue", review_codes("a", "llm", 91))
        self.assertNotIn("review-overdue", review_codes("a", "foundations", 365))
        self.assertIn("review-overdue", review_codes("a", "foundations", 366))
        self.assertNotIn("review-overdue", review_codes("pinned-skill", "foundations", 30))
        self.assertIn("review-overdue", review_codes("pinned-skill", "foundations", 31))
        self.assertNotIn("review-overdue", review_codes("a", "specialized", 180))
        self.assertIn("review-overdue", review_codes("a", "specialized", 181))

    def test_future_and_invalid_dates(self):
        s = make_skill("a", last_verified=(TODAY + dt.timedelta(days=1)).isoformat())
        self.assertIn("last-verified-in-future", codes(cf.check_skill(s, TODAY, POLICY)))
        s = make_skill("a", last_verified="2026-02-30")
        self.assertIn("invalid-last-verified", codes(cf.check_skill(s, TODAY, POLICY), "STALE"))
        s = make_skill("a", last_verified="")
        self.assertIn("no-last-verified", codes(cf.check_skill(s, TODAY, POLICY), "REVIEW_DUE"))


class TestPolicyValidation(unittest.TestCase):
    def errs(self, **overrides):
        return cf.validate_policy(policy_dict(**overrides))

    def test_valid_policy(self):
        self.assertEqual(cf.validate_policy(policy_dict()), [])

    def test_repo_policy_is_valid_against_real_skills(self):
        skills = testutil.sl.load_all_skills()
        policy = cf.load_policy(cf.DEFAULT_POLICY_PATH, skills)
        self.assertTrue(any(p.level == "STALE" for p in policy.patterns))

    def test_bad_values_rejected(self):
        self.assertTrue(self.errs(default_max_age_days=0))
        self.assertTrue(self.errs(default_max_age_days=True))
        self.assertTrue(self.errs(policy_version=2))
        self.assertTrue(self.errs(unexpected=1))
        self.assertTrue(self.errs(domain_max_age_days={"not-a-domain": {"max_age_days": 5, "reason": "r"}}))
        self.assertTrue(self.errs(domain_max_age_days={"llm": {"max_age_days": 5}}))  # reason required
        self.assertTrue(self.errs(domain_max_age_days={"llm": {"max_age_days": 5, "reason": " "}}))
        self.assertTrue(self.errs(domain_max_age_days={"llm": 90}))

    def test_bad_patterns_rejected(self):
        good = policy_dict()["known_stale_patterns"][0]
        for bad in ({**good, "pattern": "("}, {**good, "level": "ERROR"}, {**good, "id": "Not Kebab"},
                    {k: v for k, v in good.items() if k != "message"}, {**good, "extra": 1}):
            with self.subTest(bad=bad):
                self.assertTrue(self.errs(known_stale_patterns=[bad]))
        self.assertTrue(self.errs(known_stale_patterns=[good, copy.deepcopy(good)]))  # duplicate id

    def test_acknowledged_inconclusive_validation(self):
        good = {"url": "https://dl.acm.org/doi/x", "http_status": [403], "reason": "bot wall",
                "acknowledged_on": "2026-10-27"}
        self.assertEqual(self.errs(acknowledged_inconclusive=[good]), [])
        host = {k: v for k, v in good.items() if k != "url"} | {"host": "dl.acm.org"}
        self.assertEqual(self.errs(acknowledged_inconclusive=[host]), [])
        bad_cases = (
            {**good, "host": "dl.acm.org"},                    # both url and host
            {k: v for k, v in good.items() if k != "url"},     # neither
            {**good, "http_status": [404]},                    # broken links are never acknowledged
            {**good, "http_status": []},
            {**good, "http_status": [200]},
            {**good, "reason": ""},
            {**good, "acknowledged_on": "2026-02-30"},
            {**good, "url": "ftp://x"},
            {**good, "extra": 1},
        )
        for bad in bad_cases:
            with self.subTest(bad=bad):
                self.assertTrue(self.errs(acknowledged_inconclusive=[bad]))
        ack = cf.policy_from_dict(policy_dict(acknowledged_inconclusive=[good, host])).acknowledged
        self.assertTrue(ack[0].matches("https://dl.acm.org/doi/x", 403))
        self.assertFalse(ack[0].matches("https://dl.acm.org/doi/x", 503))
        self.assertFalse(ack[0].matches("https://dl.acm.org/doi/y", 403))
        self.assertTrue(ack[1].matches("https://dl.acm.org/doi/y", 403))

    def test_unknown_skill_or_capability_rejected_when_known_sets_given(self):
        data = policy_dict()
        errs = cf.validate_policy(data, skill_slugs={"other"}, capabilities={"agent-loop-design"})
        self.assertTrue(any("pinned-skill" in e for e in errs))
        self.assertTrue(any("mcp-tool-integration" in e for e in errs))

    def test_cli_invalid_policy_exits_2(self):
        import tempfile
        from pathlib import Path

        with tempfile.TemporaryDirectory() as d:
            bad = Path(d) / "p.json"
            bad.write_text(json.dumps(policy_dict(default_max_age_days=-1)), encoding="utf-8")
            with contextlib.redirect_stderr(io.StringIO()) as err:
                self.assertEqual(cf.main(["--policy", str(bad)]), 2)
            self.assertIn("default_max_age_days", err.getvalue())
            missing = Path(d) / "missing.json"
            with contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(cf.main(["--policy", str(missing)]), 2)


class TestStaticPatterns(unittest.TestCase):
    def test_stale_and_review_levels(self):
        body = ("\n## Overview\nx\n\n## Workflow\n1. s\n```python\nfrom sklearn.cross_validation import KFold\n"
                "model = 'gpt-4-0613'\n```\n")
        f = cf.check_skill(make_skill("a", body=body), TODAY, POLICY)
        stale = [x for x in f if x["kind"] == "static-pattern"]
        self.assertEqual({(x["code"], x["level"]) for x in stale},
                         {("dead-api", "STALE"), ("maybe-dead", "REVIEW_DUE")})

    def test_every_finding_has_a_documented_kind_and_level(self):
        body = "\n## Workflow\n```python\nimport numpy as np\nimport src\nfrom sklearn.cross_validation import x\n```\n"
        s = make_skill("a", body=body, version_constraints=["free text entry", "numpy>=1.0: arrays"])
        for f in cf.check_skill(s, TODAY, POLICY, version_lookup=lambda d: "2.0"):
            self.assertIn(f["kind"], cf.FINDING_KINDS)
            self.assertIn(f["level"], cf.LEVELS)

    def test_report_explains_kinds(self):
        report = cf.build_report([make_skill("a")], TODAY, POLICY)
        self.assertEqual(set(report["kinds"]), {"static-pattern", "review-clock", "metadata"})
        self.assertIn("NOT verification", report["kinds"]["static-pattern"])
        report = cf.build_report([make_skill("a")], TODAY, POLICY, version_lookup=lambda d: None)
        self.assertIn("local-version-check", report["kinds"])


class TestPep440Subset(unittest.TestCase):
    def test_version_ordering(self):
        V = cf.Version.parse
        chain = ["1.0.dev1", "1.0a1", "1.0b2", "1.0rc1", "1.0", "1.0.post1", "1.1"]
        for lo, hi in zip(chain, chain[1:]):
            self.assertLess(V(lo), V(hi), (lo, hi))
        self.assertEqual(V("1.0"), V("1.0.0"))
        self.assertEqual(V("2.13.0+cpu").public, V("2.13.0"))
        self.assertTrue(V("1!0.1") > V("99.0"))
        for bad in ("1.x", "", "latest", "1..2"):
            with self.assertRaises(cf.InvalidVersion):
                V(bad)

    def test_specifier_semantics(self):
        cases = [
            (">=1.4", "1.4", True), (">=1.4", "1.3.9", False), ("<=1.4", "1.4.0", True),
            ("<2", "1.99", True), ("<2", "2.0", False), ("<2.0", "2.0rc1", False), ("<2.0rc2", "2.0rc1", True),
            (">1.0", "1.0.post1", False), (">1.0", "1.0.1", True), ("!=1.5", "1.5.0", False),
            ("==1.15.1", "1.15.1", True), ("==1.15.1", "1.15.2", False), ("==1.2.*", "1.2.9", True),
            ("==1.2.*", "1.3", False), ("!=1.2.*", "1.3", True), ("~=1.4.2", "1.4.9", True),
            ("~=1.4.2", "1.5", False), ("~=2.2", "2.9", True), ("~=2.2", "3.0", False),
            (">=0.7,<0.8", "0.7.5", True), (">=0.7,<0.8", "0.8.0", False), (">=2.6", "2.13.0+cpu", True),
            ("==2.13.0", "2.13.0+cpu", True),
            # ~= keeps the epoch (it was once dropped, so ~=1!1.0 matched nothing)
            ("~=1!1.0", "1!1.0", True), ("~=1!1.0", "1!1.5", True), ("~=1!1.0", "1!2.0", False),
            ("~=1!1.0", "1.5", False), ("~=1.0", "1!1.5", False), ("~=0!1.0", "1.5", True),
        ]
        for spec, ver, expected in cases:
            with self.subTest(spec=spec, ver=ver):
                self.assertEqual(cf.contains(cf.parse_specifier_set(spec), ver), expected)

    def test_unsupported_specifiers_raise(self):
        for bad in ("===1.0", ">=1.*", "~=1", "^1.0", ">=1.0+local", "==1.0a1.*"):
            with self.subTest(bad=bad), self.assertRaises(cf.InvalidVersion):
                cf.parse_specifier_set(bad)


class TestConstraintParsing(unittest.TestCase):
    def test_new_convention(self):
        c = cf.parse_constraint("faiss-cpu==1.15.1: IndexFlatIP.search (verified by runtime test)")
        self.assertEqual((c.status, c.dist, c.spec_text), ("parsed", "faiss-cpu", "==1.15.1"))
        self.assertTrue(c.api.startswith("IndexFlatIP"))
        c = cf.parse_constraint("implicit >= 0.7 , < 0.8: ALS")
        self.assertEqual((c.dist, c.spec_text), ("implicit", ">=0.7,<0.8"))
        c = cf.parse_constraint("Scikit_Learn[extra]>=1.4: GridSearchCV")
        self.assertEqual(c.dist, "scikit-learn")

    def test_import_and_alias_names_map_to_dists(self):
        self.assertEqual(cf.normalize_dist("sklearn"), "scikit-learn")
        self.assertEqual(cf.normalize_dist("cv2"), "opencv-python")
        self.assertEqual(cf.normalize_dist("faiss"), "faiss-cpu")
        self.assertEqual(cf.normalize_dist("PIL"), "pillow")
        self.assertEqual(cf.normalize_dist("langchain_text_splitters"), "langchain-text-splitters")
        self.assertEqual(cf.parse_constraint("PyTorch: autocast").dist, "torch")

    def test_legacy_free_text_is_review_not_error(self):
        entry = "anthropic (Python SDK): messages.create() stable since GA (model-knowledge estimate)"
        self.assertEqual(cf.parse_constraint(entry).status, "free-text")
        s = make_skill("a", version_constraints=[entry, "numpy>=1.x: bad", "feast: FeatureView"])
        f = cf.check_skill(s, TODAY, POLICY)
        by_code = {x["code"]: x for x in f}
        self.assertEqual(by_code["constraint-free-text"]["level"], "REVIEW_DUE")
        self.assertIn("unparseable (free text)", by_code["constraint-free-text"]["message"])
        self.assertEqual(by_code["constraint-invalid-specifier"]["level"], "REVIEW_DUE")
        self.assertEqual(by_code["constraint-no-specifier"]["level"], "INFO")
        self.assertNotIn("STALE", {x["level"] for x in f})

    def test_import_coverage(self):
        body = "\n## Workflow\n```python\nimport numpy as np\nfrom sklearn import svm\nimport json\nimport src\n```\n"
        f = cf.check_skill(make_skill("a", body=body), TODAY, POLICY)
        self.assertIn("no-version-constraints", codes(f, "REVIEW_DUE"))
        msg = next(x["message"] for x in f if x["code"] == "no-version-constraints")
        self.assertNotIn("json", msg)  # stdlib
        self.assertNotIn("src", msg)   # policy ignore_imports
        s = make_skill("a", body=body, version_constraints=["scikit-learn>=1.4: svm"])
        f = cf.check_skill(s, TODAY, POLICY)
        cov = next(x for x in f if x["code"] == "import-without-constraint")
        self.assertIn("numpy", cov["message"])
        self.assertNotIn("sklearn'", cov["message"])


class TestLocalVersionCheck(unittest.TestCase):
    def run_check(self, installed):
        s = make_skill("a", version_constraints=[
            "scikit-learn>=1.4: GridSearchCV", "torch>=2.6: export", "faiss-cpu==1.15.1: IndexFlatIP",
            "feast: FeatureView", "weird>=1.0: x", "free text"])
        f = cf.check_skill(s, TODAY, POLICY, version_lookup=installed.get)
        return {x["dist"]: x for x in f if x["kind"] == "local-version-check"}

    def test_injected_versions(self):
        got = self.run_check({"scikit-learn": "1.9.0", "torch": "2.5.1", "feast": "0.40.0", "weird": "not-a-version"})
        self.assertEqual(got["scikit-learn"]["level"], "OK")
        self.assertEqual(got["torch"]["level"], "REVIEW_DUE")
        self.assertIn("does NOT satisfy", got["torch"]["message"])
        self.assertEqual(got["faiss-cpu"]["level"], "SKIPPED")
        self.assertIn("not installed", got["faiss-cpu"]["message"])
        self.assertEqual(got["feast"]["level"], "INFO")
        self.assertEqual(got["weird"]["code"], "installed-version-unparseable")
        self.assertEqual(len(got), 5)  # the free-text entry is never compared

    def test_disabled_by_default(self):
        s = make_skill("a", version_constraints=["torch>=2.6: export"])
        self.assertFalse([x for x in cf.check_skill(s, TODAY, POLICY) if x["kind"] == "local-version-check"])

    def test_default_lookup_handles_missing_dist(self):
        self.assertIsNone(cf.installed_version("definitely-not-an-installed-dist-xyz"))


class TestCli(unittest.TestCase):
    def test_json_report_on_repo(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = cf.main(["--json", "--today", "2026-10-27"])
        report = json.loads(out.getvalue())
        self.assertEqual(code, 1 if report["counts"]["STALE"] else 0)
        self.assertEqual(report["policy"], "schema/freshness_policy.json")
        self.assertIn("NOT 'verified current'", report["note"])

    def test_validate_policy_flag(self):
        with contextlib.redirect_stdout(io.StringIO()) as out:
            self.assertEqual(cf.main(["--validate-policy"]), 0)
        self.assertIn("valid freshness policy", out.getvalue())


@unittest.skipUnless(importlib.util.find_spec("sklearn"), "scikit-learn not installed")
class TestKnownStaleApisStillGone(unittest.TestCase):
    """Runtime evidence for the STALE patterns cited by docs/claims.json.

    If one of these starts importing again, the matching known_stale_patterns
    entry in schema/freshness_policy.json is wrong and must be removed.
    """

    def test_sklearn_cross_validation_gone(self):
        with self.assertRaises(ImportError):
            importlib.import_module("sklearn.cross_validation")

    def test_sklearn_externals_joblib_gone(self):
        with self.assertRaises(ImportError):
            importlib.import_module("sklearn.externals.joblib")

    def test_sklearn_preprocessing_imputer_gone(self):
        import sklearn.impute
        import sklearn.preprocessing

        self.assertFalse(hasattr(sklearn.preprocessing, "Imputer"))
        self.assertTrue(hasattr(sklearn.impute, "SimpleImputer"))


@unittest.skipUnless(importlib.util.find_spec("openai"), "openai not installed")
class TestOpenAiModuleLevelApiRemoved(unittest.TestCase):
    """Runtime evidence for the two openai-* STALE patterns (claim openai-v1-removed-module-level-completion).

    openai>=1.0 keeps `openai.Completion` / `openai.ChatCompletion` as proxies
    that raise APIRemovedInV1 when used. The call fails before any client or
    network is set up, so this needs no API key and makes no request.
    """

    def test_module_level_completion_apis_raise_removed(self):
        import openai

        for name in ("Completion", "ChatCompletion"):
            with self.subTest(api=f"openai.{name}.create"):
                with self.assertRaises(Exception) as ctx:
                    getattr(openai, name).create(model="unused", prompt="unused")
                self.assertEqual(type(ctx.exception).__name__, "APIRemovedInV1")


if __name__ == "__main__":
    unittest.main()
