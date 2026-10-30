"""Tests for scripts/check_freshness.py: policy, PEP 440 subset, constraint parsing, local-version check."""
import contextlib
import copy
import datetime as dt
import importlib.util
import io
import json
import random
import re
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

    def test_stale_level_requires_a_verified_backing_claim(self):
        body = "\n## Overview\nx\n\n## Workflow\n1. s\n```python\nfrom sklearn.cross_validation import KFold\n```\n"
        skill = make_skill("a", body=body)

        def policy(claim):
            return cf.policy_from_dict(policy_dict(known_stale_patterns=[
                {"id": "dead-api", "pattern": r"sklearn\.cross_validation", "level": "STALE", "message": "gone",
                 "claim": claim}]))

        def claim(verification, status):
            return {"c1": {"id": "c1", "verification": verification, "status": status}}

        def stale_finding(pol, claims):
            return next(f for f in cf.check_skill(skill, TODAY, pol, claims=claims) if f["code"] == "dead-api")

        for verification, status in (("runtime-test", "current"), ("human-review", "current"),
                                     ("runtime-test", "review-due")):
            with self.subTest(verification=verification, status=status):
                f = stale_finding(policy("c1"), claim(verification, status))
                self.assertEqual(f["level"], "STALE")
                self.assertNotIn("level_reason", f)
        not_backed = "not verified by a runtime test or human review"
        for pol, claims, why in ((policy("c1"), claim("unverified", "unverified"), not_backed),
                                 (policy("c1"), claim("runtime-test", "disputed"), "or is disputed"),
                                 # recorded kinds that say nothing about API behaviour cannot back STALE
                                 (policy("c1"), claim("automated-doc-check", "current"), not_backed),
                                 (policy("c1"), claim("local-version-check", "current"), not_backed),
                                 (policy("c1"), {}, "not in the claims registry"),
                                 (policy(None), claim("runtime-test", "current"), "cites no claim")):
            with self.subTest(why=why, claims=claims):
                f = stale_finding(pol, claims)
                # still flagged for review, never dropped, and the reason is part of the finding
                self.assertEqual((f["level"], f["policy_level"]), ("REVIEW_DUE", "STALE"))
                self.assertIn(why, f["level_reason"])
                self.assertIn("STALE not applied", f["message"])
        # library callers that pass no registry keep the policy level (no silent downgrade)
        self.assertEqual(stale_finding(policy("c1"), None)["level"], "STALE")
        report = cf.build_report([skill], TODAY, policy("c1"), claims=claim("unverified", "unverified"))
        self.assertEqual(report["counts"]["STALE"], 0)
        self.assertEqual([p["pattern"] for p in report["stale_patterns_not_applied"]], ["dead-api"])

    def test_cli_fails_only_for_verified_stale_patterns(self):
        import tempfile
        from pathlib import Path

        src = (testutil.REPO_ROOT / "attention-mechanisms" / "SKILL.md").read_text(encoding="utf-8")
        with tempfile.TemporaryDirectory() as d:
            root = Path(d) / "root"
            (root / "attention-mechanisms").mkdir(parents=True)
            (root / "attention-mechanisms" / "SKILL.md").write_text(src + "\nSee dead.api.zzz for history.\n",
                                                                     encoding="utf-8")
            pol = Path(d) / "policy.json"
            pol.write_text(json.dumps({"policy_version": 1, "default_max_age_days": 100000, "known_stale_patterns": [
                {"id": "dead-zzz", "pattern": r"dead\.api\.zzz", "level": "STALE", "message": "gone",
                 "claim": "c1"}]}), encoding="utf-8")
            claims = Path(d) / "claims.json"

            def run(verification, status):
                claims.write_text(json.dumps({"registry_version": 1, "claims": [
                    {"id": "c1", "verification": verification, "status": status}]}), encoding="utf-8")
                with contextlib.redirect_stdout(io.StringIO()) as out:
                    code = cf.main(["--root", str(root), "--policy", str(pol), "--claims", str(claims),
                                    "--today", "2026-10-29"])
                return code, out.getvalue()

            code, out = run("runtime-test", "current")
            self.assertEqual(code, 1, out)
            self.assertIn("STALE      [static-pattern]", out)
            code, out = run("unverified", "unverified")
            self.assertEqual(code, 0, out)
            self.assertIn("REVIEW_DUE [static-pattern]", out)
            self.assertIn("known_stale_patterns 'dead-zzz' is level STALE in the policy but is reported as "
                          "REVIEW_DUE", out)
            for bad in ("{not json", json.dumps({"claims": {}})):
                claims.write_text(bad, encoding="utf-8")
                with self.subTest(registry=bad), contextlib.redirect_stderr(io.StringIO()) as err:
                    self.assertEqual(cf.main(["--root", str(root), "--policy", str(pol), "--claims", str(claims)]),
                                     2)
                self.assertIn("claims registry", err.getvalue())
            with contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(cf.main(["--root", str(root), "--policy", str(pol), "--claims",
                                          str(Path(d) / "missing.json")]), 2)

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
        # ">=1.0," (empty clause) is accepted by packaging but rejected here on purpose: in
        # version_constraints it is more likely a typo than an intent (docs/FRESHNESS.md).
        for bad in ("===1.0", ">=1.*", "~=1", "^1.0", ">=1.0+local", "==1.0a1.*", ">=1.0,", "~=1.0.*",
                    "==1.0.post1.*", "<1.0+cpu"):
            with self.subTest(bad=bad), self.assertRaises(cf.InvalidVersion):
                cf.parse_specifier_set(bad)

    def test_exclusive_ordered_comparisons(self):
        # Fixed stdlib-only cases for PEP 440's <V / >V exclusions, read literally ("of V" means V
        # exactly), which is what packaging >= 26.1 does. Older packaging compared base versions, which
        # is where the reviewer's four '>V-pre vs V+local' disagreements (docs/KNOWN-ISSUES.md #15) and
        # the post/pre cases below came from.
        cases = [
            (">1.0rc1", "1.0+cpu", True), (">1.0rc1", "1.0", True), (">1.0rc1", "1.0rc1.post1", False),
            (">1.0rc1", "1.0rc1+x", False), (">1.0.dev0", "1.0a1+local", True), (">1.0.dev0", "1.0.dev0+x", False),
            (">1.0.dev0", "1.0.post1", True), (">1.0.post1", "1.0.post2+1", True), (">1.0.post1", "1.0.post1+1", False),
            (">1!1.0a1", "1!1.0+x", True), (">1.0", "1.0+cpu", False), (">1.0", "1.0.post1", False),
            (">1.0", "1.0.post1.dev1", False), (">1.0", "1.0.post1+cpu", False), (">1.0", "1.1+cpu", True),
            (">1.0", "1.0.1", True), ("<1.1", "1.0+cpu", True), ("<1.0", "1.0+cpu", False), ("<1.0", "1.0rc1", False),
            ("<1.0", "1.0.dev0+cpu", False), ("<1.0", "0.9+cpu", True), ("<1.0.post1", "1.0rc1", True),
            ("<1.0.post1", "1.0.post1.dev1", False), ("<1.0rc2", "1.0rc1+x", True), ("<0", "0.dev0", False),
            ("<=1.0", "1.0+cpu", True), (">=1.0", "1.0+cpu", True), ("==1.0+cpu", "1.0", False),
            ("!=1.0+cpu", "1.0", True), ("==1.0+CPU", "1.0+cpu", True), ("==1.0+01", "1.0+1", True),
        ]
        for spec, ver, expected in cases:
            with self.subTest(spec=spec, ver=ver):
                self.assertEqual(cf.contains(cf.parse_specifier_set(spec), ver), expected)


def _differential_matrix():
    """Versions x specifiers covering epochs, pre/dev/post, implicit post (-N), local labels, trailing zeros."""
    bases = ["0", "1.0", "1.0.0", "1.2", "1!1.0"]
    suffixes = ["", "a", "rc1", ".dev0", ".dev1", "a1.dev1", ".post1", ".post1.dev1", "-2", "rc1.post1"]
    locals_ = ["", "+local", "+1.a"]
    versions = sorted({b + s + loc for b in bases for s in suffixes for loc in locals_})
    spec_versions = sorted({b + s for b in bases for s in suffixes} | {b + loc for b in bases for loc in locals_})
    specs = [op + v for op in ("==", "!=", ">=", "<=", ">", "<", "~=") for v in spec_versions]
    specs += [op + b + ".*" for op in ("==", "!=", ">=", "~=") for b in bases]
    specs += ["", ">=1.0,<2", ">=1.0,<2,!=1.2.*", "~=1.0,!=1.0.post1", " >= 1.0 , < 2.0 ", "==v1.0",
              ">=1.0a1,<1.0", "===1.0", ">=1.0,"]
    return versions, specs


def _packaging_version() -> tuple[int, ...]:
    import packaging

    return tuple(int(p) for p in packaging.__version__.split(".")[:2] if p.isdigit())


def _is_exclusive(spec_text: str) -> bool:
    return any(re.match(r"\s*[<>](?!=)", part) for part in spec_text.split(","))


@unittest.skipUnless(importlib.util.find_spec("packaging"), "packaging not installed: the differential "
                     "comparison needs it; the fixed cases in TestPep440Subset still run")
class TestPep440AgreesWithPackaging(unittest.TestCase):
    """The stdlib subset must agree with the packaging library wherever both accept a specifier.

    Allowed differences, all documented in docs/FRESHNESS.md: `===` (arbitrary equality) and an empty
    clause such as '>=1.0,' are rejected here although packaging accepts them. Nothing packaging
    rejects may be accepted here. The exclusive operators (<, >) follow packaging >= 26.1, which changed
    them; against older packaging only the other operators are compared.
    """

    def compare(self, keep):
        from packaging.specifiers import InvalidSpecifier, SpecifierSet
        from packaging.version import Version

        versions, specs = _differential_matrix()
        specs = [t for t in specs if keep(t)]
        parsed = [(v, Version(v), cf.Version.parse(v)) for v in versions]
        out_of_subset, wrong = [], []
        for text in specs:
            try:
                theirs = SpecifierSet(text)
            except InvalidSpecifier:
                theirs = None
            try:
                ours = cf.parse_specifier_set(text)
            except cf.InvalidVersion:
                ours = None
            if theirs is None:
                self.assertIsNone(ours, f"{text!r} is invalid for packaging but accepted by the subset")
                continue
            if ours is None:
                out_of_subset.append(text)
                continue
            wrong += [(text, v) for v, pv, ov in parsed
                      if theirs.contains(pv, prereleases=True) != cf.contains(ours, ov)]
        self.assertEqual(wrong, [], f"{len(wrong)} disagreement(s), first: {wrong[:5]}")
        return out_of_subset

    def test_inclusive_and_equality_operators_match_packaging(self):
        out_of_subset = self.compare(lambda t: not _is_exclusive(t))
        self.assertEqual(sorted(out_of_subset), sorted(["===1.0", ">=1.0,"]))

    def test_exclusive_operators_match_packaging(self):
        if _packaging_version() < (26, 1):
            self.skipTest("packaging < 26.1 reads PEP 440's <V / >V exclusions by base version; the subset "
                          "follows packaging >= 26.1 (fixed cases in TestPep440Subset still run)")
        self.assertEqual(self.compare(_is_exclusive), [])

    def test_version_parsing_and_ordering_match_packaging(self):
        from packaging.version import InvalidVersion, Version

        versions, _ = _differential_matrix()
        odd = ["v1.0", " 1.0 ", "1.0-rc1", "1.0_a_1", "1.0.alpha1", "1.0preview2", "1.0-r1", "1.0.rev",
               "1.0c1", "1.0+abc-def_1", "1.0.post", "1.0-dev", "1.x", "", "1..0", "1.0+", "1.0-", "latest",
               "1.0+a..b", "01.002", "1!", "!1.0", "1.0.dev-1", "1.0a1b2"]
        for text in odd:
            try:
                theirs = Version(text)
            except InvalidVersion:
                theirs = None
            with self.subTest(version=text):
                if theirs is None:
                    with self.assertRaises(cf.InvalidVersion):
                        cf.Version.parse(text)
                else:
                    self.assertEqual(cf.Version.parse(text), cf.Version.parse(str(theirs)))
                    versions.append(text)
        rng = random.Random(440)
        rng.shuffle(versions)
        self.assertEqual([str(Version(v)) for v in sorted(versions, key=lambda v: cf.Version.parse(v).key())],
                         [str(v) for v in sorted(Version(v) for v in versions)])
        for a, b in zip(versions, versions[1:]):
            self.assertEqual(Version(a) == Version(b), cf.Version.parse(a) == cf.Version.parse(b), (a, b))


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


@unittest.skipUnless(importlib.util.find_spec("tensorflow"), "tensorflow not installed (no CI job installs it; "
                     "run in a venv with tensorflow-cpu to refresh claim tensorflow-2-removed-contrib)")
class TestTensorflowContribGone(unittest.TestCase):
    """Runtime evidence for the 'tensorflow-contrib' STALE pattern (claim tensorflow-2-removed-contrib)."""

    def test_tf_contrib_not_importable(self):
        import tensorflow as tf

        self.assertTrue(tf.__version__.startswith("2."), tf.__version__)
        self.assertFalse(hasattr(tf, "contrib"))
        with self.assertRaises(ImportError):
            importlib.import_module("tensorflow.contrib")


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
