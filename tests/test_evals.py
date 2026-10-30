"""Tests for the behavioral-eval framework core (scripts/eval_lib.py) and
scripts/validate_evals.py: hard-assertion grading, regex lint (vacuous
patterns), fixture self-tests, case/plan validation, coverage, and the real
evals/ tree. Pure stdlib; fixture repos are built in temp dirs so the tests
never depend on files other people are editing (except the explicit
real-repo checks at the bottom)."""
from __future__ import annotations

import contextlib
import io
import json
import re
import tempfile
import unittest
from pathlib import Path

from testutil import REPO_ROOT

import eval_lib as el  # scripts/ is on sys.path via testutil
import validate_evals

# Dev fixture labels (written by the pattern-tuning engineer, kept as fixtures; not blind):
#   fail.dev-confident  - on-topic wrong answer stated with full confidence
#   fail.dev-partial    - mostly correct answer with one rubric (`must_not`) violation
#   fail.dev-borderline - hedged or softened wrong answer
#   pass.dev-borderline - correct answer phrased close to a must_not_include pattern
# An optional `-<suffix>` tells apart several fixtures of one kind in a case
# (pass.dev-borderline-approval). Every fail.dev-* fixture must be caught by a
# must_not_include pattern, not only by a missing concept: each one was written to prove a
# specific pattern can fire. Probes written from the rubric before the case's patterns were
# read use the SCHEMA.md `fixprobe` label instead.
DEV_FIXTURE_LABELS = {"fail": re.compile(r"dev-(confident|partial|borderline)(-[a-z0-9]+)?"),
                      "pass": re.compile(r"dev-borderline(-[a-z0-9]+)?")}

SKILL_MD = "---\nname: {slug}\ndisplay_name: {slug}\ntype: {type}\n---\n## Overview\nFixture skill.\n"

LEAK_CASE = r"""id: leak
skill: skill-a
category: misconception
kind: response
input: |
  Can I scale the whole dataset before splitting?
expected_behavior:
  - 'identifies leakage'
must_not:
  - 'says it is fine'
must_include:
  - '\bleak\w*'
  - '\b(split|splitting) (it )?first\b'
must_not_include:
  - '\A\W*(yes|sure)\b'
"""
LEAK_PASS = "No - **split first**, otherwise test-set statistics leak into training.\n"
LEAK_FAIL = "Yes, scaling everything first is fine and simpler.\n"

RUBRIC_CASE = """id: plain
skill: skill-a
category: happy_path
kind: response
input: |
  Walk me through it.
expected_behavior:
  - 'explains the steps'
"""

FAKE_TESTS = '''import unittest


class _Mixin:
    def test_inherited(self):
        pass


class FakeTest(_Mixin, unittest.TestCase):
    def test_ok(self):
        self.assertTrue(True)

    def test_fail(self):
        self.fail("boom")

    @unittest.skip("needs a gpu")
    def test_skip(self):
        pass

    def helper(self):
        pass
'''


def plan_for(slug: str = "skill-a", **overrides) -> dict:
    plan = {
        "skill": slug, "type": "workflow",
        "representative_tasks": [{"task": "scale before split?", "expected_outcome": "flags leakage"}],
        "negative_cases": ["leak"], "assertion_strategy": ["hard-assertions", "rubric"],
        "rubric": ["leakage explained"], "prerequisites": [], "execution_constraints": ["offline"],
        "expected_artifacts": [], "known_limitations": ["regexes check vocabulary only"],
        "unsupported_use_cases": ["model training"],
    }
    plan.update(overrides)
    return plan


def make_repo(root: Path, *, with_plan: bool = True) -> Path:
    """A minimal repo: two skills, one hard-assertion case with fixtures, one rubric-only case."""
    for slug, typ in (("skill-a", "workflow"), ("skill-b", "reference")):
        (root / slug).mkdir(parents=True)
        (root / slug / "SKILL.md").write_text(SKILL_MD.format(slug=slug, type=typ), encoding="utf-8", newline="\n")
    ev = root / "evals" / "skill-a"
    ev.mkdir(parents=True)
    (ev / "leak.yaml").write_text(LEAK_CASE, encoding="utf-8", newline="\n")
    (ev / "leak.pass.md").write_text(LEAK_PASS, encoding="utf-8", newline="\n")
    (ev / "leak.fail.md").write_text(LEAK_FAIL, encoding="utf-8", newline="\n")
    (ev / "plain.yaml").write_text(RUBRIC_CASE, encoding="utf-8", newline="\n")
    if with_plan:
        (ev / "plan.json").write_text(json.dumps(plan_for()), encoding="utf-8")
    ex = root / "tests" / "examples"
    ex.mkdir(parents=True)
    (ex / "__init__.py").write_text("", encoding="utf-8")
    (ex / "test_fake.py").write_text(FAKE_TESTS, encoding="utf-8")
    return root


class RepoCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = make_repo(Path(self._tmp.name))
        self.ev = self.root / "evals" / "skill-a"

    def tearDown(self):
        self._tmp.cleanup()

    def write(self, name: str, text: str) -> Path:
        p = self.ev / name
        p.write_text(text, encoding="utf-8", newline="\n")
        return p

    def suite(self) -> el.EvalSuite:
        return el.load_suite(self.root)

    def errors_for(self, needle: str) -> list[str]:
        return [e for e in self.suite().errors if needle in e]

    def cli(self, *args: str) -> tuple[int, str]:
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = validate_evals.main(["--root", str(self.root), *args])
        return code, out.getvalue()


def case_with(must_include=(), must_not_include=()) -> el.Case:
    data = {"skill": "s", "category": "edge_case", "kind": "response", "input": "q", "expected_behavior": ["x"],
            "must_include": list(must_include), "must_not_include": list(must_not_include)}
    return el.Case(path=Path("evals/s/c.yaml"), data=data, diagnostics=[], sha256="0")


class GradeResponseTests(unittest.TestCase):
    def test_all_must_include_must_match(self):
        case = case_with([r"\bleak\w*", r"\bpipeline\b"])
        self.assertTrue(el.grade_response(case, "Data leakage; use a Pipeline.")["passed"])
        g = el.grade_response(case, "Data leakage only.")
        self.assertFalse(g["passed"])
        self.assertEqual([a["passed"] for a in g["assertions"]], [True, False])

    def test_any_must_not_include_match_fails_and_records_evidence(self):
        case = case_with([r"\bleak\w*"], [r"\bit'?s fine\b"])
        g = el.grade_response(case, "There is leakage, but it's fine.")
        self.assertFalse(g["passed"])
        bad = g["assertions"][1]
        self.assertEqual((bad["type"], bad["passed"], bad["evidence"]), ("must_not_include", False, "it's fine"))

    def test_case_insensitive_and_normalized(self):
        case = case_with([r"data leakage", r"it's not fine", r"split - then fit"])
        text = "**DATA**\n   `leakage`:  it’s not fine.\nSplit — then fit."
        self.assertTrue(el.grade_response(case, text)["passed"])

    def test_rubric_only_case_is_not_gradable(self):
        g = el.grade_response(case_with(), "anything")
        self.assertEqual((g["gradable"], g["passed"], g["assertions"]), (False, False, []))

    def test_grading_is_deterministic(self):
        case = case_with([r"\bleak\w*"], [r"\byes\b"])
        self.assertEqual(el.grade_response(case, "leak yes"), el.grade_response(case, "leak yes"))


class LintTests(unittest.TestCase):
    def test_dot_star_in_must_include_is_flagged_vacuous(self):
        errors, _ = el.lint_patterns(case_with([".*"]))
        self.assertTrue(any(e.startswith("vacuous pattern") and "empty string" in e for e in errors), errors)

    def test_other_empty_matching_patterns_are_flagged(self):
        for p in (r"\w*", r"(leak)?", r"x?|y", r"^"):
            errors, _ = el.lint_patterns(case_with([p]))
            self.assertTrue(any("vacuous" in e for e in errors), (p, errors))

    def test_pattern_matching_content_free_text_is_flagged(self):
        errors, _ = el.lint_patterns(case_with([r"\b(the|a|for)\b"]))
        self.assertTrue(any("content-free" in e for e in errors), errors)

    def test_vacuous_must_not_include_is_flagged(self):
        errors, _ = el.lint_patterns(case_with([r"\bleak"], [".*"]))
        self.assertTrue(any("fails every response" in e for e in errors), errors)

    def test_invalid_and_empty_regex_are_errors(self):
        errors, _ = el.lint_patterns(case_with(["(unclosed", "  "]))
        self.assertTrue(any("not a valid Python regex" in e for e in errors))
        self.assertTrue(any("is empty" in e for e in errors))

    def test_specific_pattern_is_clean(self):
        self.assertEqual(el.lint_patterns(case_with([r"\bleak\w*"], [r"\A\W*yes\b"])), ([], []))


class FixtureSelfTestTests(RepoCase):
    def test_baseline_repo_is_valid(self):
        s = self.suite()
        self.assertEqual(s.errors, [])
        self.assertEqual([(r["fixture"], r["ok"]) for r in s.fixture_results],
                         [("leak.fail.md", True), ("leak.pass.md", True)])

    def test_pass_fixture_that_fails_is_an_error(self):
        self.write("leak.pass.md", "No, split it first.\n")  # never mentions leakage
        self.assertTrue(self.errors_for("pass fixture leak.pass.md fails hard assertion(s) must_include[0]"))

    def test_fail_fixture_that_passes_is_an_error(self):
        self.write("leak.fail.md", LEAK_PASS)
        self.assertTrue(self.errors_for("fail fixture leak.fail.md passes every hard assertion"))

    def test_every_labelled_fixture_is_checked(self):
        self.write("leak.fail.other.md", "No - split first; leakage matters.\n")
        self.assertTrue(self.errors_for("fail fixture leak.fail.other.md passes"))

    def test_missing_fixtures_are_errors(self):
        (self.ev / "leak.fail.md").unlink()
        self.assertTrue(self.errors_for("no fail fixture (leak.fail.md)"))
        (self.ev / "leak.pass.md").unlink()
        self.assertTrue(self.errors_for("no pass fixture (leak.pass.md)"))

    def test_vacuous_pattern_is_reported_by_the_validator(self):
        self.write("leak.yaml", LEAK_CASE.replace(r"'\bleak\w*'", "'.*'"))
        self.assertTrue(self.errors_for("vacuous pattern: must_include[0] '.*' matches the empty string"))
        code, out = self.cli()
        self.assertEqual(code, 1)
        self.assertIn("vacuous pattern", out)

    def test_must_include_matching_every_fail_fixture_is_flagged(self):
        self.write("leak.fail.md", "Yes, leakage is negligible, scale first.\n")
        warnings = [w for w in self.suite().warnings if "non-discriminating pattern: must_include[0]" in w]
        self.assertEqual(len(warnings), 1, self.suite().warnings)
        self.assertEqual(self.cli()[0], 0)               # a warning by default...
        self.assertEqual(self.cli("--strict")[0], 1)     # ...an error under --strict
        # A second wrong answer that does not mention leakage clears the flag.
        self.write("leak.fail.plain.md", "Sure, normalize the full dataset.\n")
        self.assertFalse([w for w in self.suite().warnings if "non-discriminating" in w])

    def test_concept_free_fail_fixtures_are_flagged(self):
        # The base repo's only wrong answer ("Yes, scaling everything first is fine") uses none of
        # the concept vocabulary, so it fails every must_include trivially: discrimination is shown
        # only against an off-topic strawman.
        s = self.suite()
        self.assertEqual(len([w for w in s.warnings if "concept-free fail fixtures" in w]), 1, s.warnings)
        fail = next(r for r in s.fixture_results if r["fixture"] == "leak.fail.md")
        self.assertEqual((fail["must_include_matched"], fail["on_topic"]), (0, False))
        self.assertEqual(self.cli()[0], 0)               # a warning by default...
        self.assertEqual(self.cli("--strict")[0], 1)     # ...an error under --strict
        # A wrong answer that names the concept but reaches the wrong conclusion clears it,
        # and it must be caught by must_not_include, not by a missing concept.
        self.write("leak.fail.on_topic.md", "Yes - there is a little leakage, but it is negligible; no need to "
                                            "split first.\n")
        s = self.suite()
        self.assertEqual(s.errors, [])
        self.assertFalse([w for w in s.warnings if "concept-free" in w], s.warnings)
        on_topic = next(r for r in s.fixture_results if r["fixture"] == "leak.fail.on_topic.md")
        self.assertTrue(on_topic["on_topic"])

    def test_must_not_include_that_no_fail_fixture_triggers_is_an_advisory(self):
        # The base repo's must_not_include fires on its fail fixture ("Yes, ..."): exercised.
        self.assertEqual(self.suite().advisories, [])
        # A broken pattern (missing space, as in a real case) never fires; the fail fixture
        # still fails on the missing concepts, so the self-test alone would not notice.
        self.write("leak.yaml", LEAK_CASE + "  - '\\b(scaling) (isn''t|is un)(necessary)\\b'\n")
        s = self.suite()
        self.assertEqual(s.errors, [])
        [adv] = s.advisories
        self.assertIn("leak.yaml: unexercised pattern: must_not_include[1]", adv)
        # Kept out of suite.warnings (run_evals.py's validation), but promoted by the CLI.
        self.assertFalse([w for w in s.warnings if "unexercised" in w])
        self.assertEqual(self.cli()[0], 0)
        self.assertEqual(self.cli("--strict-advisories")[0], 1)
        code, out = self.cli("--strict")
        self.assertEqual(code, 1)
        self.assertIn("1 advisory warning(s) (--strict)", out)
        code, out = self.cli("--json")
        self.assertEqual((code, json.loads(out)["advisories"]), (0, [adv]))
        self.assertTrue(json.loads(out)["ok"])
        # Under --strict the JSON `ok` field follows the exit code, so it is false here too.
        code, out = self.cli("--strict", "--json")
        self.assertEqual((code, json.loads(out)["ok"]), (1, False))
        self.assertIn("EVAL WARNING (advisory):", self.cli()[1])
        # A wrong answer the pattern catches clears it (the broken pattern only ever
        # matches "is unnecessary"; the advisory is what prompts finding that out).
        self.write("leak.fail.scaling.md", "Scaling is unnecessary here.\n")
        self.assertEqual(self.suite().advisories, [])

    def test_on_topic_threshold_is_half_rounded_up(self):
        self.assertEqual([el.on_topic_threshold(n) for n in (1, 2, 3, 4, 5)], [1, 1, 2, 2, 3])
        self.assertFalse(el.is_on_topic(0, 0))           # must_not_include-only cases are exempt
        self.assertTrue(el.is_on_topic(2, 3))
        self.assertFalse(el.is_on_topic(1, 3))

    def test_fixtures_without_assertions_are_errors(self):
        self.write("plain.pass.md", "anything\n")
        self.assertTrue(self.errors_for("has .pass/.fail fixtures but no must_include"))

    def test_orphan_and_unexpected_files(self):
        self.write("ghost.pass.md", "x\n")
        self.write("notes.txt", "x\n")
        self.assertTrue(self.errors_for("ghost.pass.md: fixture has no matching case ghost.yaml"))
        self.assertTrue(self.errors_for("notes.txt: unexpected file"))

    def test_auxiliary_non_skill_directory_is_ignored(self):
        aux = self.root / "evals" / "routing"
        aux.mkdir()
        (aux / "benchmark.json").write_text("{}", encoding="utf-8")
        (aux / "README.md").write_text("x", encoding="utf-8")
        self.assertEqual(self.suite().errors, [])
        (aux / "case.yaml").write_text(RUBRIC_CASE, encoding="utf-8")  # a case there is still an error
        self.assertTrue(self.errors_for("evals/routing/case.yaml: lives under evals/routing/"))


class CaseValidationTests(RepoCase):
    def mutate(self, old: str, new: str) -> None:
        text = (self.ev / "leak.yaml").read_text(encoding="utf-8")
        self.assertIn(old, text)
        self.write("leak.yaml", text.replace(old, new, 1))

    def test_unknown_field(self):
        self.mutate("kind: response", "kind: response\nweight: '2'")
        self.assertTrue(self.errors_for("unknown field 'weight'"))

    def test_kind_is_required_and_enumerated(self):
        self.mutate("kind: response\n", "")
        self.assertTrue(self.errors_for("missing required field 'kind'"))
        self.mutate("category: misconception", "category: misconception\nkind: essay")
        self.assertTrue(self.errors_for("kind 'essay' not in"))

    def test_id_must_equal_stem(self):
        self.mutate("id: leak", "id: other")
        self.assertTrue(self.errors_for("id 'other' must equal the file stem 'leak'"))

    def test_bad_category_and_skill(self):
        self.mutate("category: misconception", "category: trick")
        self.assertTrue(self.errors_for("category 'trick' not in"))
        self.mutate("skill: skill-a", "skill: nope")
        self.assertTrue(self.errors_for("skill 'nope' does not exist"))

    def test_requires_is_a_closed_set(self):
        self.mutate("kind: response", "kind: response\nrequires:\n  - api_key")
        self.assertTrue(self.errors_for("requires value 'api_key' not in"))

    def test_parser_diagnostics_are_errors(self):
        self.mutate("  - 'identifies leakage'", "  - identifies leakage: concretely")
        self.assertTrue(self.errors_for("could not parse"))

    def test_executable_kind_needs_resolvable_tests(self):
        self.mutate("kind: response", "kind: executable")
        self.assertTrue(self.errors_for("kind: executable requires a non-empty executable_tests"))
        for test_id, msg in (
                ("test_fake.FakeTest.test_ok", "must be `examples.<module>.<Class>.<test_method>`"),
                ("examples.test_nope.FakeTest.test_ok", "no module tests/examples/test_nope.py"),
                ("examples.test_fake.Missing.test_ok", "no class Missing"),
                ("examples.test_fake.FakeTest.test_missing", "has no method test_missing"),
                ("examples.test_fake.FakeTest.helper", "is not a test method")):
            text = (self.ev / "leak.yaml").read_text(encoding="utf-8").split("executable_tests:")[0]
            self.write("leak.yaml", text.rstrip("\n") + f"\nexecutable_tests:\n  - {test_id}\n")
            self.assertTrue(self.errors_for(msg), (test_id, self.suite().errors))

    def test_inherited_test_method_resolves(self):
        self.mutate("kind: response", "kind: executable\nexecutable_tests:\n"
                                      "  - examples.test_fake.FakeTest.test_inherited")
        self.assertEqual(self.suite().errors, [])

    def test_response_kind_rejects_executable_tests(self):
        self.mutate("kind: response", "kind: response\nexecutable_tests:\n  - examples.test_fake.FakeTest.test_ok")
        self.assertTrue(self.errors_for("only meaningful for kind: executable"))

    def test_crlf_checkout_parses_and_hashes_like_lf(self):
        lf = el.load_case(self.ev / "leak.yaml")
        (self.ev / "leak.yaml").write_bytes(LEAK_CASE.replace("\n", "\r\n").encode("utf-8"))
        crlf = el.load_case(self.ev / "leak.yaml")
        self.assertEqual((crlf.data, crlf.diagnostics, crlf.sha256), (lf.data, [], lf.sha256))
        self.assertEqual(self.suite().errors, [])

    def test_rubric_only_case_is_a_warning(self):
        self.assertTrue([w for w in self.suite().warnings if "plain.yaml: rubric-only case" in w])

    def test_duplicate_uid_detected(self):
        other = self.root / "evals" / "skill-b"
        other.mkdir()
        text = RUBRIC_CASE.replace("skill: skill-a", "skill: skill-b").replace("id: plain", "id: x")
        (other / "x.yaml").write_text(text, encoding="utf-8")
        self.assertEqual(self.suite().errors, [])  # same stem in different skills is fine


class PlanValidationTests(RepoCase):
    def set_plan(self, **overrides) -> None:
        self.write("plan.json", json.dumps(plan_for(**overrides)))

    def test_valid_plan(self):
        self.assertIn("skill-a", self.suite().plans)

    def test_invalid_json(self):
        self.write("plan.json", "{not json")
        self.assertTrue(self.errors_for("plan.json: not valid JSON"))

    def test_type_and_skill_must_match(self):
        self.set_plan(type="reference")
        self.assertTrue(self.errors_for("does not match skill-a/SKILL.md type 'workflow'"))
        self.set_plan(skill="skill-b")
        self.assertTrue(self.errors_for("must equal the directory name 'skill-a'"))

    def test_unknown_and_missing_fields(self):
        plan = plan_for(extra=1)
        del plan["rubric"]
        self.write("plan.json", json.dumps(plan))
        self.assertTrue(self.errors_for("unknown field 'extra'"))
        self.assertTrue(self.errors_for("missing required field 'rubric'"))

    def test_representative_tasks_shape(self):
        self.set_plan(representative_tasks=[])
        self.assertTrue(self.errors_for("representative_tasks must be a non-empty list"))
        self.set_plan(representative_tasks=[{"task": "t"}])
        self.assertTrue(self.errors_for("representative_tasks[0] must be an object"))

    def test_negative_cases_must_reference_real_non_happy_path_cases(self):
        self.set_plan(negative_cases=["plain"])
        self.assertTrue(self.errors_for("'plain' is a happy_path case"))
        self.set_plan(negative_cases=["missing_case"])
        self.assertTrue(self.errors_for("'missing_case' looks like a case id"))
        self.set_plan(negative_cases=["A planned case about something else"])
        self.assertEqual(self.suite().errors, [])

    def test_assertion_strategy_consistency(self):
        self.set_plan(assertion_strategy=["executable"])
        self.assertTrue(self.errors_for("includes 'executable' but no case has kind: executable"))
        self.set_plan(assertion_strategy="rubric", rubric=[])
        self.assertTrue(self.errors_for("includes 'rubric' but rubric is empty"))
        self.set_plan(assertion_strategy=["vibes"])
        self.assertTrue(self.errors_for("assertion_strategy must be one or a list"))
        self.set_plan(assertion_strategy="hard-assertions")  # a single string is accepted
        self.assertFalse(self.errors_for("assertion_strategy"))


class CoverageTests(RepoCase):
    def test_coverage_rows_and_summary(self):
        cov = el.coverage(self.suite())
        rows = {r["skill"]: r for r in cov["skills"]}
        self.assertEqual(rows["skill-a"]["missing"], [])
        self.assertEqual((rows["skill-a"]["cases"], rows["skill-a"]["non_happy_path_cases"],
                          rows["skill-a"]["hard_assertion_cases"], rows["skill-a"]["fixture_backed_cases"]),
                         (2, 1, 1, 1))
        self.assertEqual(rows["skill-b"]["missing"], ["plan.json", "case", "non-happy-path case"])
        self.assertEqual(cov["summary"]["fully_covered"], 1)
        self.assertEqual(cov["summary"]["skills_total"], 2)

    def test_default_run_does_not_fail_on_coverage_but_required_mode_does(self):
        self.assertEqual(self.cli("--coverage")[0], 0)
        code, out = self.cli("--require-full-coverage")
        self.assertEqual(code, 1)
        self.assertIn("EVAL COVERAGE GAP: skill-b: missing plan.json, case, non-happy-path case", out)

    def test_non_happy_path_requirement_and_exemption(self):
        (self.ev / "leak.yaml").write_text(LEAK_CASE.replace("misconception", "happy_path"), encoding="utf-8")
        self.write("plan.json", json.dumps(plan_for(negative_cases=[])))
        rows = {r["skill"]: r for r in el.coverage(self.suite())["skills"]}
        self.assertEqual(rows["skill-a"]["missing"], ["non-happy-path case"])
        self.write("plan.json", json.dumps(plan_for(
            negative_cases=[], non_happy_path_exemption="Every request to this skill is a lookup with one answer.")))
        rows = {r["skill"]: r for r in el.coverage(self.suite())["skills"]}
        self.assertEqual(rows["skill-a"]["missing"], [])

    def test_json_output(self):
        code, out = self.cli("--json", "--coverage")
        doc = json.loads(out)
        self.assertEqual(code, 0)
        self.assertTrue(doc["ok"])
        self.assertEqual(doc["fixture_self_tests"], {"total": 2, "ok": 2})
        self.assertEqual(doc["coverage"]["summary"]["skills_total"], 2)


class ResponsesFileTests(unittest.TestCase):
    """run_evals.load_responses must read back what --save-responses writes. (Kept here
    rather than in test_run_evals.py only for file-ownership reasons during the audit fix.)"""

    def test_unicode_line_separators_inside_json_strings_round_trip(self):
        import run_evals
        uid = "skill-a/leak"
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "r.jsonl"
            # json.dumps(ensure_ascii=False) — the --save-responses format — leaves U+2028,
            # U+2029 and U+0085 raw; str.splitlines() treats them as line breaks.
            rows = [{"case_id": uid, "condition": f"c{i}", "response": f"a{ch}b"}
                    for i, ch in enumerate(("\u2028", "\u2029", "\x85", "\x0b", "\x1c"))]
            path.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows),
                            encoding="utf-8", newline="\n")
            loaded, errors = run_evals.load_responses(path, {uid})
            self.assertEqual(errors, [])
            self.assertEqual([r["response"] for r in loaded], [r["response"] for r in rows])
            # CRLF (a file edited on Windows) still reads as one record per line.
            path.write_bytes(path.read_bytes().replace(b"\n", b"\r\n"))
            loaded, errors = run_evals.load_responses(path, {uid})
            self.assertEqual((len(loaded), errors), (len(rows), []))


class RealRepoTests(unittest.TestCase):
    """The shipped evals/ tree must validate cleanly and stay a faithful YAML subset."""

    @classmethod
    def setUpClass(cls):
        cls.suite = el.load_suite(REPO_ROOT)

    def test_no_validation_errors(self):
        self.assertEqual(self.suite.errors, [])

    def test_every_case_has_kind_and_hard_assertions_have_both_fixture_types(self):
        for case in self.suite.cases:
            with self.subTest(case=case.uid):
                self.assertIn(case.kind, el.CASE_KINDS)
                if case.has_hard_assertions:
                    self.assertEqual({f.expect for f in case.fixtures}, {"pass", "fail"})

    def test_every_hard_assertion_case_has_an_on_topic_wrong_answer(self):
        # Guards the reviewer finding: fail fixtures must include a wrong answer that uses the
        # topic's vocabulary, so must_not_include is actually exercised (see concept-free lint).
        for case in self.suite.cases:
            if not case.must_include:
                continue
            with self.subTest(case=case.uid):
                fails = [r for r in self.suite.fixture_results if r["case_id"] == case.uid and r["expected"] == "fail"]
                self.assertTrue(any(r["on_topic"] for r in fails), [r["fixture"] for r in fails])
                # ...and at least one on-topic wrong answer is caught by must_not_include itself.
                self.assertTrue(any(r["on_topic"] and any(a["type"] == "must_not_include" and not a["passed"]
                                                          for a in r["assertions"]) for r in fails))

    def test_strict_mode_is_clean(self):
        self.assertEqual(self.suite.warnings, [])

    def test_every_must_not_include_pattern_is_exercised(self):
        # The 30 patterns that pre-dated the unexercised-pattern lint each have a fail fixture
        # now (or were fixed or removed), so the backlog ratchet is gone: any must_not_include
        # pattern that no fail fixture triggers fails here, whoever adds it.
        unexercised = [a for a in self.suite.advisories if "unexercised pattern: must_not_include[" in a]
        self.assertEqual(unexercised, [], "must_not_include pattern(s) that no fail fixture triggers; add a "
                                          "fail fixture each one catches, or fix or remove the pattern")
        self.assertEqual(self.suite.advisories, [])

    def test_every_fixture_grades_as_labelled(self):
        # Regression check per fixture: every pass fixture passes every hard assertion and every
        # fail fixture fails at least one. (Also reported as validation errors; listed here so a
        # failure names the fixture and the assertions involved.)
        wrong = [f"{r['case_id']}: {r['fixture']} expected {r['expected']}, observed {r['observed']}; "
                 + ("failed " + ", ".join(a["id"] for a in r["assertions"] if not a["passed"])
                    if r["expected"] == "pass" else "passed every hard assertion")
                 for r in self.suite.fixture_results if not r["ok"]]
        self.assertEqual(wrong, [])
        self.assertEqual({r["observed"] for r in self.suite.fixture_results if r["expected"] == "pass"}, {"pass"})
        self.assertEqual({r["observed"] for r in self.suite.fixture_results if r["expected"] == "fail"}, {"fail"})

    def test_dev_fixtures_use_known_labels_and_fail_on_must_not_include(self):
        fixtures = {(c.uid, f.name): f for c in self.suite.cases for f in c.fixtures}
        # Any label that starts with "dev" counts, so a misspelt one (dev_partial, devpartial,
        # dev-weird) fails the label check instead of skipping both checks.
        dev = [r for r in self.suite.fixture_results
               if (fixtures[(r["case_id"], r["fixture"])].label or "").startswith("dev")]
        self.assertGreaterEqual(len([r for r in dev if r["expected"] == "fail"]), 30)
        for r in dev:
            label = fixtures[(r["case_id"], r["fixture"])].label
            with self.subTest(case=r["case_id"], fixture=r["fixture"]):
                self.assertTrue(DEV_FIXTURE_LABELS[r["expected"]].fullmatch(label),
                                f"unknown {r['expected']}.dev fixture label {label!r}")
                if r["expected"] == "fail":
                    fired = [a["id"] for a in r["assertions"] if a["type"] == "must_not_include" and not a["passed"]]
                    self.assertTrue(fired, "a fail.dev-* fixture must be caught by a must_not_include pattern")

    def test_every_skill_with_cases_has_a_plan(self):
        self.assertEqual(sorted({c.skill_dir for c in self.suite.cases}), sorted(self.suite.plans))

    def test_case_files_parse_identically_with_pyyaml(self):
        try:
            import yaml
        except ImportError:
            self.skipTest("PyYAML not installed (optional cross-check)")
        for case in self.suite.cases:
            with self.subTest(case=case.uid):
                self.assertEqual(case.diagnostics, [])
                self.assertEqual(yaml.safe_load(case.path.read_text(encoding="utf-8")), case.data)

    def test_cli_default_mode_passes(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = validate_evals.main([])
        self.assertEqual(code, 0, out.getvalue())

    def test_cli_strict_modes_pass(self):
        # What CI runs (--strict --require-full-coverage), plus --strict-advisories. One call: each
        # run loads and self-tests the whole suite.
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = validate_evals.main(["--strict", "--strict-advisories", "--require-full-coverage"])
        self.assertEqual(code, 0, out.getvalue())
        self.assertIn("0 warning(s); 0 advisory warning(s)", out.getvalue())


if __name__ == "__main__":
    unittest.main()
