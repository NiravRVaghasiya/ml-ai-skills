"""Tests for scripts/run_evals.py: offline report schema and determinism,
recorded-response grading, regression comparison, model mode with the HTTP
layer mocked (no network, no API key needed), refusal to run model mode
without every explicit flag, retry/backoff, LLM-judge separation, and the
executable-test runner. Pure stdlib."""
from __future__ import annotations

import email.message
import hashlib
import io
import json
import sys
import tempfile
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from unittest import mock

from test_evals import LEAK_FAIL, LEAK_PASS, make_repo

import eval_lib as el  # scripts/ is on sys.path via testutil
import run_evals as re_

FIXED_ENV = {"python_version": "3.x", "python_implementation": "CPython", "platform": "test",
             "git_revision": None, "git_dirty": None}
API_KEY = "sk-test-not-a-real-key-123"


class FakeTransport:
    """Scripted stand-in for urllib: records every request, replays responses in order."""

    def __init__(self, responses):
        self.responses = list(responses)
        self.requests: list[dict] = []

    def __call__(self, url, body, headers, timeout):
        self.requests.append({"url": url, "body": json.loads(body), "headers": dict(headers), "timeout": timeout})
        item = self.responses.pop(0) if len(self.responses) > 1 else self.responses[0]
        if isinstance(item, Exception):
            raise item
        if callable(item):
            item = item(json.loads(body))
        status, payload, *rest = item
        return status, (rest[0] if rest else {}), json.dumps(payload).encode("utf-8")


def message(text: str, **extra) -> tuple[int, dict]:
    return 200, {"id": "msg_1", "type": "message", "role": "assistant", "model": "model-x-reported",
                 "content": [{"type": "thinking", "thinking": ""}, {"type": "text", "text": text}],
                 "stop_reason": "end_turn", "usage": {"input_tokens": 10, "output_tokens": 5}, **extra}


def by_condition(body: dict) -> tuple[int, dict]:
    """Model double: answers correctly only when the skill is in the system prompt."""
    return message(LEAK_PASS if "<skill name=" in body["system"] else LEAK_FAIL)


class Base(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.root = make_repo(self.tmp / "repo")

    def tearDown(self):
        self._tmp.cleanup()

    def run_cli(self, *args, env=None, transport=None, sleep=None, now=lambda: "2026-01-01T00:00:00+00:00",
                environment=FIXED_ENV):
        out, err = io.StringIO(), io.StringIO()
        sleeps: list[float] = []
        code = re_.main(["--root", str(self.root), *args], env=env if env is not None else {},
                        transport=transport, sleep=sleep or sleeps.append, now=now, environment=environment,
                        stdout=out, stderr=err)
        self.sleeps = sleeps
        return code, out.getvalue(), err.getvalue()

    def report(self, *args, **kw) -> tuple[int, dict]:
        code, out, err = self.run_cli(*args, **kw)
        return code, json.loads(out)

    def responses_file(self, rows, name="r.jsonl") -> Path:
        p = self.tmp / name
        p.write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")
        return p

    def model_args(self, *extra):
        return ("--provider", "anthropic", "--model", "model-x", "--allow-network", *extra)


class OfflineReportTests(Base):
    def test_schema_and_provenance(self):
        code, r = self.report()
        self.assertEqual(code, 0)
        for key in ("schema", "schema_version", "harness_version", "generated_at", "nondeterministic_fields", "mode",
                    "environment", "dependencies", "dataset", "validation", "input_errors", "fixture_self_tests",
                    "graded", "executable", "skipped", "summary", "skill_uplift", "baseline", "model_run"):
            self.assertIn(key, r)
        self.assertEqual((r["schema"], r["schema_version"], r["mode"]), (re_.REPORT_SCHEMA, 1, "offline"))
        self.assertEqual(r["harness_version"], el.HARNESS_VERSION)
        self.assertEqual(r["nondeterministic_fields"], ["generated_at"])
        self.assertEqual(r["baseline"]["status"], "unmeasured")
        self.assertEqual(r["skill_uplift"]["status"], "unmeasured")
        self.assertIsNone(r["model_run"])
        case = r["dataset"]["cases"]["skill-a/leak"]
        raw = (self.root / "evals" / "skill-a" / "leak.yaml").read_bytes()
        self.assertEqual(case["sha256"], hashlib.sha256(raw).hexdigest())
        skill_raw = (self.root / "skill-a" / "SKILL.md").read_text(encoding="utf-8")
        self.assertEqual(case["skill_sha256"], hashlib.sha256(skill_raw.encode("utf-8")).hexdigest())
        self.assertEqual(sorted(case["fixtures"]), ["leak.fail.md", "leak.pass.md"])
        self.assertEqual(r["fixture_self_tests"]["total"], 2)
        self.assertEqual(r["fixture_self_tests"]["failed"], 0)
        self.assertIn({"case_id": "skill-a/plain", "what": "hard assertions", "reason": "rubric-only case"},
                      r["skipped"])

    def test_identical_inputs_give_byte_identical_reports_except_timestamp(self):
        stamps = iter(["2026-01-01T00:00:00+00:00", "2027-06-30T12:34:56+00:00"])
        rows = self.responses_file([{"case_id": "skill-a/leak", "condition": "with_skill", "response": LEAK_PASS}])
        outs = []
        for _ in range(2):
            # real environment_info (temp dir is not a git repo -> stable None values)
            code, out, _ = self.run_cli("--responses", str(rows), now=lambda: next(stamps), environment=None)
            self.assertEqual(code, 0)
            outs.append(out)
        self.assertNotEqual(outs[0], outs[1])
        a, b = (json.loads(o) for o in outs)
        for doc in (a, b):
            for field in re_.NONDETERMINISTIC_FIELDS:
                doc.pop(field)
        self.assertEqual(re_.dump(a), re_.dump(b))

    def test_out_file_and_stdout_agree(self):
        out_path = self.tmp / "report.json"
        self.run_cli("--out", str(out_path))
        _, stdout, _ = self.run_cli()
        self.assertEqual(out_path.read_text(encoding="utf-8"), stdout)

    def test_invalid_suite_exits_1(self):
        (self.root / "evals" / "skill-a" / "leak.fail.md").write_text(LEAK_PASS, encoding="utf-8")
        code, r = self.report()
        self.assertEqual(code, 1)
        self.assertEqual(r["fixture_self_tests"]["failed"], 1)
        self.assertFalse(r["validation"]["ok"])

    def test_case_selection(self):
        _, r = self.report("--case", "plain")
        self.assertEqual(list(r["dataset"]["cases"]), ["skill-a/plain"])
        code, _, err = self.run_cli("--skill", "skill-b")
        self.assertEqual(code, 2)
        self.assertIn("selected no cases", err)


class RecordedResponseTests(Base):
    def test_grades_per_assertion_and_keeps_subjective_separate(self):
        rows = self.responses_file([
            {"case_id": "skill-a/leak", "condition": "with_skill", "response": LEAK_PASS},
            {"case_id": "skill-a/leak", "condition": "without_skill", "response": LEAK_FAIL},
            {"case_id": "skill-a/plain", "condition": "with_skill", "response": "steps"},
        ])
        code, r = self.report("--responses", str(rows))
        self.assertEqual(code, 0)
        g = {(x["case_id"], x["condition"]): x for x in r["graded"]}
        self.assertTrue(g[("skill-a/leak", "with_skill")]["hard"]["passed"])
        failed = g[("skill-a/leak", "without_skill")]["hard"]
        self.assertFalse(failed["passed"])
        self.assertEqual([a["id"] for a in failed["assertions"] if not a["passed"]],
                         ["must_include[0]", "must_include[1]", "must_not_include[0]"])
        plain = g[("skill-a/plain", "with_skill")]["hard"]
        self.assertEqual((plain["gradable"], plain["passed"]), (False, None))
        for x in r["graded"]:
            self.assertEqual(x["subjective"]["status"], "not-graded")
        self.assertEqual(r["summary"]["hard"]["with_skill"],
                         {"responses": 2, "gradable": 1, "passed": 1, "failed": 0, "errors": 0, "not_gradable": 1,
                          "pass_rate": 1.0})
        self.assertEqual(r["summary"]["hard"]["without_skill"]["pass_rate"], 0.0)
        self.assertIsNone(r["summary"]["subjective"]["with_skill"]["rubric_pass_rate"])
        up = r["skill_uplift"]
        self.assertEqual((up["status"], up["paired_cases"], up["delta"]), ("measured", 1, 1.0))
        self.assertEqual(up["only_with_skill_passed"], ["skill-a/leak"])

    def test_strict_fails_on_hard_failure(self):
        rows = self.responses_file([{"case_id": "skill-a/leak", "condition": "recorded", "response": LEAK_FAIL}])
        self.assertEqual(self.run_cli("--responses", str(rows))[0], 0)
        self.assertEqual(self.run_cli("--responses", str(rows), "--strict")[0], 1)

    def test_bad_input_lines_are_reported_not_guessed(self):
        p = self.tmp / "bad.jsonl"
        p.write_text("\n".join([
            "{not json",
            json.dumps({"case_id": "skill-a/nope", "condition": "with_skill", "response": "x"}),
            json.dumps({"case_id": "skill-a/leak", "condition": "With Skill", "response": "x"}),
            json.dumps({"case_id": "skill-a/leak", "response": "x"}),
            json.dumps({"case_id": "skill-a/leak", "condition": "c", "response": "x"}),
            json.dumps({"case_id": "skill-a/leak", "condition": "c", "response": "y"}),
        ]) + "\n", encoding="utf-8")
        code, r = self.report("--responses", str(p))
        self.assertEqual(code, 1)
        errs = "\n".join(r["input_errors"])
        for needle in ("bad.jsonl:1: not valid JSON", "unknown case_id 'skill-a/nope'", "condition 'With Skill'",
                       "bad.jsonl:4: each line needs", "bad.jsonl:6: duplicate response"):
            self.assertIn(needle, errs)
        self.assertEqual(len(r["graded"]), 1)


class CompareTests(Base):
    def make_report(self, name, rows):
        p = self.tmp / name
        args = ("--responses", str(self.responses_file(rows, name + ".jsonl"))) if rows else ()
        self.run_cli(*args, "--out", str(p))
        return p

    def test_regression_detected_with_assertion_detail(self):
        row = {"case_id": "skill-a/leak", "condition": "with_skill", "response": LEAK_PASS}
        base = self.make_report("base.json", [row])
        new = self.make_report("new.json", [{**row, "response": "No - split first, otherwise statistics differ."}])
        code, out, _ = self.run_cli("--compare", str(base), str(new))
        self.assertEqual(code, 1)
        c = json.loads(out)
        self.assertEqual(c["schema"], re_.COMPARE_SCHEMA)
        self.assertEqual([(x["case_id"], x["assertion"]) for x in c["regressions"]],
                         [("skill-a/leak", "must_include[0]")])
        self.assertEqual(c["baseline"]["status"], "measured")
        # the reverse direction is an improvement, not a regression
        code, out, _ = self.run_cli("--compare", str(new), str(base))
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out)["summary"]["improvements"], 1)

    def test_no_graded_base_is_reported_unmeasured(self):
        base = self.make_report("base.json", [])
        row = {"case_id": "skill-a/leak", "condition": "with_skill", "response": LEAK_PASS}
        new = self.make_report("new.json", [row])
        code, out, _ = self.run_cli("--compare", str(base), str(new))
        c = json.loads(out)
        self.assertEqual((code, c["baseline"]["status"]), (0, "unmeasured"))
        self.assertEqual(c["added_in_new"], ["skill-a/leak|with_skill"])

    def test_changed_case_and_fixture_regression_flagged(self):
        base = self.make_report("base.json", [{"case_id": "skill-a/leak", "condition": "c", "response": LEAK_PASS}])
        (self.root / "evals" / "skill-a" / "leak.fail.md").write_text(LEAK_PASS, encoding="utf-8")
        y = self.root / "evals" / "skill-a" / "leak.yaml"
        y.write_text(y.read_text(encoding="utf-8") + "notes: >\n  changed\n", encoding="utf-8")
        new = self.make_report("new.json", [{"case_id": "skill-a/leak", "condition": "c", "response": LEAK_PASS}])
        code, out, _ = self.run_cli("--compare", str(base), str(new))
        c = json.loads(out)
        self.assertEqual(code, 1)
        self.assertEqual(c["changed_cases"], ["skill-a/leak"])
        self.assertEqual(c["fixture_regressions"],
                         [{"case_id": "skill-a/leak", "fixture": "leak.fail.md", "new": "fail"}])

    # ---- the gate cannot be bypassed by dropping or weakening what used to pass ----
    def test_passing_response_missing_from_new_is_a_regression(self):
        row = {"case_id": "skill-a/leak", "condition": "with_skill", "response": LEAK_PASS}
        base = self.make_report("base.json", [row, {**row, "condition": "without_skill", "response": LEAK_FAIL}])
        new = self.make_report("new.json", [{"case_id": "skill-a/plain", "condition": "with_skill", "response": "x"}])
        code, out, _ = self.run_cli("--compare", str(base), str(new))
        c = json.loads(out)
        self.assertEqual(code, 1)
        # only the BASE-passing key regresses; the BASE-failing one is merely listed as missing
        self.assertEqual([(r["case_id"], r["condition"], r["new"]) for r in c["regressions"]],
                         [("skill-a/leak", "with_skill", "missing")])
        self.assertEqual(c["missing_in_new"], ["skill-a/leak|with_skill", "skill-a/leak|without_skill"])

    def test_removed_hard_assertions_are_a_regression(self):
        row = {"case_id": "skill-a/leak", "condition": "with_skill", "response": LEAK_PASS}
        base = self.make_report("base.json", [row])
        y = self.root / "evals" / "skill-a" / "leak.yaml"
        y.write_text(y.read_text(encoding="utf-8").split("must_include:")[0], encoding="utf-8")
        for f in ("leak.pass.md", "leak.fail.md"):
            (self.root / "evals" / "skill-a" / f).unlink()
        new = self.make_report("new.json", [{**row, "response": LEAK_FAIL}])
        code, out, _ = self.run_cli("--compare", str(base), str(new))
        c = json.loads(out)
        self.assertEqual(code, 1)
        self.assertEqual([(r["assertion"], r["base"], r["new"]) for r in c["regressions"]],
                         [(None, "pass", "not-gradable")])
        self.assertEqual(c["changed_cases"], ["skill-a/leak"])
        self.assertEqual(sorted((f["fixture"], f["new"]) for f in c["fixture_regressions"]),
                         [("leak.fail.md", "missing"), ("leak.pass.md", "missing")])

    def test_removed_passing_assertion_is_a_regression(self):
        row = {"case_id": "skill-a/leak", "condition": "with_skill", "response": LEAK_PASS}
        base = self.make_report("base.json", [row])
        y = self.root / "evals" / "skill-a" / "leak.yaml"
        y.write_text(y.read_text(encoding="utf-8").replace("  - '\\b(split|splitting) (it )?first\\b'\n", ""),
                     encoding="utf-8")
        new = self.make_report("new.json", [row])
        code, out, _ = self.run_cli("--compare", str(base), str(new))
        c = json.loads(out)
        self.assertEqual(code, 1)
        self.assertEqual([(r["assertion"], r["new"]) for r in c["regressions"]], [("must_include[1]", "missing")])

    def test_invalid_new_report_fails_the_gate(self):
        row = {"case_id": "skill-a/leak", "condition": "with_skill", "response": LEAK_PASS}
        base = self.make_report("base.json", [row])
        y = self.root / "evals" / "skill-a" / "leak.yaml"
        y.write_text(y.read_text(encoding="utf-8") + "bogus_field: 'x'\n", encoding="utf-8")
        new = self.make_report("new.json", [row])
        self.assertFalse(json.loads(new.read_text(encoding="utf-8"))["validation"]["ok"])
        code, out, _ = self.run_cli("--compare", str(base), str(new))
        c = json.loads(out)
        self.assertEqual((code, c["summary"]["regressions"]), (1, 0))
        self.assertEqual(len(c["new_report_problems"]), 1)
        self.assertIn("failed validation", c["new_report_problems"][0])

    def test_new_report_with_input_errors_fails_the_gate(self):
        row = {"case_id": "skill-a/leak", "condition": "with_skill", "response": LEAK_PASS}
        base = self.make_report("base.json", [row])
        new = self.make_report("new.json", [row, {"case_id": "skill-a/nope", "condition": "c", "response": "x"}])
        code, out, _ = self.run_cli("--compare", str(base), str(new))
        self.assertEqual(code, 1)
        self.assertIn("input error", json.loads(out)["new_report_problems"][0])

    def test_identical_reports_pass_the_gate(self):
        row = {"case_id": "skill-a/leak", "condition": "with_skill", "response": LEAK_PASS}
        base = self.make_report("base.json", [row])
        code, out, _ = self.run_cli("--compare", str(base), str(base))
        c = json.loads(out)
        self.assertEqual((code, c["regressions"], c["fixture_regressions"], c["new_report_problems"]), (0, [], [], []))

    def test_uplift_delta_change(self):
        rows = [{"case_id": "skill-a/leak", "condition": "with_skill", "response": LEAK_PASS},
                {"case_id": "skill-a/leak", "condition": "without_skill", "response": LEAK_FAIL}]
        base = self.make_report("base.json", rows)
        new = self.make_report("new.json", [rows[0], {**rows[1], "response": LEAK_PASS}])
        c = json.loads(self.run_cli("--compare", str(base), str(new))[1])
        self.assertEqual(c["skill_uplift"]["delta_change"], -1.0)
        self.assertEqual(c["regressions"], [])

    def test_rejects_non_reports(self):
        junk = self.tmp / "junk.json"
        junk.write_text("{}", encoding="utf-8")
        code, _, err = self.run_cli("--compare", str(junk), str(junk))
        self.assertEqual(code, 2)
        self.assertIn("is not a", err)


class ModelModeRefusalTests(Base):
    def assert_refused(self, args, env, missing):
        t = FakeTransport([message("x")])
        code, out, err = self.run_cli(*args, env=env, transport=t)
        self.assertEqual(code, 2, err)
        self.assertEqual(out, "")
        self.assertEqual(t.requests, [])
        for m in missing:
            self.assertIn(m, err)

    def test_each_missing_requirement_refuses(self):
        env = {"ANTHROPIC_API_KEY": API_KEY}
        self.assert_refused(["--allow-network"], env, ["--provider anthropic", "--model <model id>"])
        self.assert_refused(["--provider", "anthropic", "--model", "m"], env, ["--allow-network"])
        self.assert_refused(["--provider", "anthropic", "--allow-network"], env, ["--model <model id>"])
        self.assert_refused(list(self.model_args()), {}, ["ANTHROPIC_API_KEY"])
        self.assert_refused(list(self.model_args()), {"ANTHROPIC_API_KEY": ""}, ["ANTHROPIC_API_KEY"])
        self.assert_refused(["--judge-model", "j"], env, ["--provider anthropic", "--allow-network"])

    def test_model_only_flags_without_model_mode_are_refused_not_ignored(self):
        env = {"ANTHROPIC_API_KEY": API_KEY}
        saved = self.tmp / "saved.jsonl"
        for args, named in ((["--save-responses", str(saved)], "--save-responses"),
                            (["--temperature", "0"], "--temperature"),
                            (["--conditions", "with_skill"], "--conditions"),
                            (["--max-tokens", "10"], "--max-tokens"),
                            (["--anthropic-version", "2099-01-01"], "--anthropic-version")):
            with self.subTest(args=args):
                self.assert_refused(args, env, [named, "--provider anthropic", "--allow-network"])
        self.assertFalse(saved.exists())

    def test_default_run_never_touches_the_network(self):
        t = FakeTransport([message("x")])
        code, _, _ = self.run_cli(env={"ANTHROPIC_API_KEY": API_KEY}, transport=t)
        self.assertEqual((code, t.requests), (0, []))

    def test_unknown_condition_refused(self):
        t = FakeTransport([message("x")])
        code, _, err = self.run_cli(*self.model_args("--conditions", "with_skill,zero_shot"),
                                    env={"ANTHROPIC_API_KEY": API_KEY}, transport=t)
        self.assertEqual((code, t.requests), (2, []))

    def test_invalid_suite_is_not_sent_to_the_api(self):
        (self.root / "evals" / "skill-a" / "leak.fail.md").write_text(LEAK_PASS, encoding="utf-8")
        t = FakeTransport([message("x")])
        code, _, err = self.run_cli(*self.model_args(), env={"ANTHROPIC_API_KEY": API_KEY}, transport=t)
        self.assertEqual((code, t.requests), (1, []))
        self.assertIn("not spending API calls", err)


class ModelModeTests(Base):
    env = {"ANTHROPIC_API_KEY": API_KEY}

    def test_requests_report_and_conditions(self):
        t = FakeTransport([by_condition])
        saved = self.tmp / "saved.jsonl"
        code, out, err = self.run_cli(*self.model_args("--save-responses", str(saved)), env=self.env, transport=t)
        self.assertEqual(code, 0, err)
        r = json.loads(out)
        self.assertEqual(len(t.requests), 4)  # 2 cases x 2 conditions
        for req in t.requests:
            self.assertEqual(req["url"], "https://api.anthropic.com/v1/messages")
            self.assertEqual(req["headers"], {"x-api-key": API_KEY, "anthropic-version": "2023-06-01",
                                              "content-type": "application/json"})
            self.assertEqual(req["body"]["model"], "model-x")
            self.assertNotIn("temperature", req["body"])  # not sent unless asked for
            self.assertEqual(req["body"]["messages"][0]["role"], "user")
        skill_md = (self.root / "skill-a" / "SKILL.md").read_text(encoding="utf-8")
        systems = {(req["body"]["messages"][0]["content"].strip(), "<skill name=" in req["body"]["system"])
                   for req in t.requests}
        self.assertEqual(len(systems), 4)
        with_skill = [q for q in t.requests if "<skill name=" in q["body"]["system"]]
        self.assertTrue(all(skill_md in q["body"]["system"] for q in with_skill))
        self.assertEqual(r["mode"], "model")
        mr = r["model_run"]
        self.assertEqual((mr["provider"], mr["model"], mr["anthropic_version"], mr["temperature"]),
                         ("anthropic", "model-x", "2023-06-01", None))
        self.assertEqual(mr["prompt_template_sha256"], re_.prompt_template_sha256())
        self.assertEqual(mr["http_attempts"], 4)
        g = {(x["case_id"], x["condition"]): x for x in r["graded"]}
        self.assertTrue(g[("skill-a/leak", "with_skill")]["hard"]["passed"])
        self.assertFalse(g[("skill-a/leak", "without_skill")]["hard"]["passed"])
        self.assertEqual(g[("skill-a/leak", "with_skill")]["model_reported"], "model-x-reported")
        self.assertEqual(g[("skill-a/leak", "with_skill")]["response"], LEAK_PASS)  # thinking block excluded
        self.assertEqual(r["skill_uplift"]["delta"], 1.0)
        self.assertEqual(r["baseline"]["status"], "unmeasured")
        self.assertNotIn(API_KEY, out + err)
        # saved responses re-grade offline to the same hard verdicts
        code, r2 = self.report("--responses", str(saved))
        self.assertEqual(code, 0)
        hard = lambda rep: {(x["case_id"], x["condition"]): x["hard"] for x in rep["graded"]}  # noqa: E731
        self.assertEqual(hard(r), hard(r2))

    def test_temperature_sent_only_when_given(self):
        t = FakeTransport([by_condition])
        code, out, _ = self.run_cli(*self.model_args("--temperature", "0", "--conditions", "with_skill"),
                                    env=self.env, transport=t)
        self.assertEqual(code, 0)
        self.assertTrue(all(q["body"]["temperature"] == 0.0 for q in t.requests))
        self.assertEqual(json.loads(out)["model_run"]["temperature"], 0.0)

    def test_judge_is_recorded_separately_from_hard_results(self):
        def judge_or_answer(body):
            if body["system"] == re_.JUDGE_SYSTEM_PROMPT:
                n = body["messages"][0]["content"].count("\n0. ")
                verdict = {"expected_behavior": [{"index": 0, "met": False, "reason": "r"}],
                           "must_not": [{"index": 0, "violated": False, "reason": "r"}] if n > 1 else []}
                return message(json.dumps(verdict))
            return by_condition(body)
        t = FakeTransport([judge_or_answer])
        code, out, _ = self.run_cli(*self.model_args("--judge-model", "judge-x", "--conditions", "with_skill"),
                                    env=self.env, transport=t)
        self.assertEqual(code, 0)
        r = json.loads(out)
        leak = next(x for x in r["graded"] if x["case_id"] == "skill-a/leak")
        self.assertTrue(leak["hard"]["passed"])                                  # hard: pass
        self.assertEqual((leak["subjective"]["status"], leak["subjective"]["rubric_pass"]), ("graded", False))
        self.assertEqual(leak["subjective"]["judge_model"], "judge-x")
        self.assertEqual(r["summary"]["hard"]["with_skill"]["pass_rate"], 1.0)   # unaffected by the judge
        self.assertEqual(r["summary"]["subjective"]["with_skill"]["rubric_pass_rate"], 0.0)
        self.assertEqual(r["model_run"]["judge_model"], "judge-x")

    def test_unparseable_judge_output_is_not_a_verdict(self):
        def judge_or_answer(body):
            return message("I think it is fine.") if body["system"] == re_.JUDGE_SYSTEM_PROMPT else by_condition(body)
        t = FakeTransport([judge_or_answer])
        _, out, _ = self.run_cli(*self.model_args("--judge-model", "j", "--conditions", "with_skill"),
                                 env=self.env, transport=t)
        r = json.loads(out)
        self.assertEqual({x["subjective"]["status"] for x in r["graded"]}, {"unparseable"})
        self.assertEqual(r["summary"]["subjective"]["with_skill"]["judge_errors"], 2)

    def test_off_contract_judge_verdicts_are_unparseable_not_coerced(self):
        # bool("false") is True: string booleans must never become a (inverted) verdict.
        bad_verdicts = {
            "string booleans": lambda n: {"expected_behavior": [{"index": 0, "met": "false"}],
                                          "must_not": [{"index": 0, "violated": "false"}][:n]},
            "duplicate index": lambda n: {"expected_behavior": [{"index": 0, "met": True}, {"index": 0, "met": True}],
                                          "must_not": [{"index": 0, "violated": False}][:n]},
            "out-of-range index": lambda n: {"expected_behavior": [{"index": 1, "met": True}],
                                             "must_not": [{"index": 0, "violated": False}][:n]},
            "bool index": lambda n: {"expected_behavior": [{"index": False, "met": True}],
                                     "must_not": [{"index": 0, "violated": False}][:n]},
            "integer verdict": lambda n: {"expected_behavior": [{"index": 0, "met": 1}],
                                          "must_not": [{"index": 0, "violated": 0}][:n]},
            "not an object": lambda n: [1, 2],
        }
        for label, make in bad_verdicts.items():
            def judge_or_answer(body, make=make):
                if body["system"] == re_.JUDGE_SYSTEM_PROMPT:
                    n = 1 if "says it is fine" in body["messages"][0]["content"] else 0
                    return message(json.dumps(make(n)))
                return by_condition(body)
            with self.subTest(label):
                t = FakeTransport([judge_or_answer])
                _, out, _ = self.run_cli(*self.model_args("--judge-model", "j", "--conditions", "with_skill"),
                                         env=self.env, transport=t)
                r = json.loads(out)
                self.assertEqual({x["subjective"]["status"] for x in r["graded"]}, {"unparseable"})
                self.assertIsNone(r["summary"]["subjective"]["with_skill"]["rubric_pass_rate"])

    def test_api_error_becomes_an_error_entry_without_the_key(self):
        t = FakeTransport([(401, {"type": "error", "error": {"type": "authentication_error", "message": "bad key"}})])
        code, out, err = self.run_cli(*self.model_args("--conditions", "with_skill"), env=self.env, transport=t)
        r = json.loads(out)
        self.assertEqual(len(t.requests), 2)  # 401 is not retried
        self.assertEqual({x["status"] for x in r["graded"]}, {"error"})
        self.assertIn("HTTP 401: authentication_error: bad key", r["graded"][0]["error"])
        self.assertEqual(r["summary"]["hard"]["with_skill"]["errors"], 2)
        self.assertNotIn(API_KEY, out + err)
        self.assertEqual(code, 0)
        self.assertEqual(self.run_cli(*self.model_args("--conditions", "with_skill", "--strict"),
                                      env=self.env, transport=t)[0], 1)


class RetryTests(unittest.TestCase):
    def client(self, responses, **kw):
        t = FakeTransport(responses)
        sleeps: list[float] = []
        return re_.MessagesClient(API_KEY, transport=t, sleep=sleeps.append, rng=lambda: 1.0, **kw), t, sleeps

    def test_retry_after_is_honoured_then_succeeds(self):
        c, t, sleeps = self.client([(429, {"error": {"type": "rate_limit_error"}}, {"retry-after": "7"}),
                                    (529, {"error": {"type": "overloaded_error"}}), message("ok")])
        self.assertEqual(re_.response_text(c.create({"model": "m"})), "ok")
        self.assertEqual(len(t.requests), 3)
        self.assertEqual(sleeps, [7.0, 2.0])  # header value, then backoff_base * 2**1 (rng=1.0 -> no jitter cut)

    def test_backoff_is_capped_and_retries_are_bounded(self):
        c, t, sleeps = self.client([(503, {})], max_retries=3, backoff_cap=3.0)
        with self.assertRaises(re_.ApiError) as cm:
            c.create({"model": "m"})
        self.assertEqual(len(t.requests), 4)
        self.assertEqual(sleeps, [1.0, 2.0, 3.0])
        self.assertIn("gave up after 4 attempt(s)", str(cm.exception))
        self.assertEqual(cm.exception.status, 503)

    def test_connection_errors_are_retried(self):
        c, t, sleeps = self.client([re_.TransportError("reset"), message("ok")])
        self.assertEqual(re_.response_text(c.create({})), "ok")
        self.assertEqual(len(sleeps), 1)

    def test_client_errors_are_not_retried(self):
        c, t, sleeps = self.client([(400, {"error": {"type": "invalid_request_error", "message": "temperature"}})])
        with self.assertRaises(re_.ApiError) as cm:
            c.create({})
        self.assertEqual((len(t.requests), sleeps), (1, []))
        self.assertNotIn(API_KEY, str(cm.exception))

    def test_response_text_ignores_non_text_blocks(self):
        msg = {"content": [{"type": "thinking", "thinking": "secret"}, {"type": "text", "text": "a"},
                           {"type": "tool_use", "name": "x"}, {"type": "text", "text": "b"}]}
        self.assertEqual(re_.response_text(msg), "ab")


class UrllibTransportTests(unittest.TestCase):
    """The real stdlib HTTP layer, with only the opener's open() replaced (no network)."""

    @staticmethod
    def headers(**kv) -> email.message.Message:
        m = email.message.Message()
        for k, v in kv.items():
            m[k.replace("_", "-")] = v
        return m

    class Resp:
        def __init__(self, payload: dict):
            self.status = 200
            self.headers = UrllibTransportTests.headers(Content_Type="application/json")
            self._body = json.dumps(payload).encode("utf-8")

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def read(self):
            return self._body

    def run_client(self, outcomes, **kw):
        calls = []

        def fake_urlopen(req, timeout):
            calls.append((req, timeout))
            o = outcomes.pop(0)
            if isinstance(o, BaseException):
                raise o
            return o
        sleeps: list[float] = []
        with mock.patch.object(re_._OPENER, "open", fake_urlopen):
            client = re_.MessagesClient(API_KEY, sleep=sleeps.append, rng=lambda: 1.0, **kw)  # default transport
            try:
                return client.create({"model": "m", "max_tokens": 1, "messages": []}), calls, sleeps, None
            except re_.ApiError as e:
                return None, calls, sleeps, e

    def http_error(self, code: int, body: dict, **hdrs) -> urllib.error.HTTPError:
        return urllib.error.HTTPError(re_.API_URL, code, "err", self.headers(**hdrs),
                                      io.BytesIO(json.dumps(body).encode("utf-8")))

    def test_retry_after_timeout_and_dns_errors_then_success(self):
        outcomes = [self.http_error(429, {"error": {"type": "rate_limit_error", "message": "slow"}}, Retry_After="3"),
                    TimeoutError("timed out"), urllib.error.URLError("getaddrinfo failed"),
                    self.Resp({"content": [{"type": "text", "text": "ok"}], "model": "m"})]
        msg, calls, sleeps, err = self.run_client(outcomes, timeout=7.5)
        self.assertIsNone(err)
        self.assertEqual(re_.response_text(msg), "ok")
        self.assertEqual(len(calls), 4)
        self.assertEqual(sleeps, [3.0, 2.0, 4.0])  # Retry-After read via the real header mapping, then backoff
        req, timeout = calls[0]
        self.assertEqual((req.full_url, req.get_method(), timeout), (re_.API_URL, "POST", 7.5))
        self.assertEqual((req.get_header("X-api-key"), req.get_header("Anthropic-version"),
                          req.get_header("Content-type")), (API_KEY, "2023-06-01", "application/json"))
        self.assertEqual(json.loads(req.data)["model"], "m")

    def test_client_error_is_mapped_and_not_retried(self):
        _, calls, sleeps, err = self.run_client(
            [self.http_error(401, {"error": {"type": "authentication_error", "message": "bad key"}})])
        self.assertEqual((len(calls), sleeps, err.status), (1, [], 401))
        self.assertIn("HTTP 401: authentication_error: bad key", str(err))
        self.assertNotIn(API_KEY, str(err))

    @staticmethod
    def api_request() -> urllib.request.Request:
        # Never opened: handed straight to the redirect machinery.
        return urllib.request.Request(re_.API_URL, data=b"{}", headers={"x-api-key": API_KEY},  # noqa: S310
                                      method="POST")

    def test_redirect_is_refused_and_the_api_key_is_not_forwarded(self):
        # The stock HTTPRedirectHandler would turn this POST into a GET to the Location,
        # copying x-api-key along, even to another host over plain http.
        req = self.api_request()
        req.timeout = 5.0  # set by OpenerDirector.open; the stock handler re-opens with it
        for code in (301, 302, 303, 307, 308):
            with self.subTest(code=code):
                hdrs = self.headers(Location="http://attacker.example/collect")
                fp = io.BytesIO(b"moved")
                with mock.patch.object(re_._OPENER, "open", side_effect=AssertionError("followed the redirect")):
                    with self.assertRaises(urllib.error.HTTPError) as ctx:
                        re_._OPENER.error("https", req, fp, code, "Found", hdrs)
                self.assertEqual(ctx.exception.code, code)

        def redirected(req, timeout):
            raise urllib.error.HTTPError(req.full_url, 302, "Found", self.headers(Location="http://evil/"),
                                         io.BytesIO(b""))
        calls = []
        with mock.patch.object(re_._OPENER, "open", lambda req, timeout: calls.append(req) or redirected(req, timeout)):
            client = re_.MessagesClient(API_KEY, sleep=lambda s: None, rng=lambda: 1.0)
            with self.assertRaises(re_.ApiError) as ctx:
                client.create({"model": "m", "max_tokens": 1, "messages": []})
        self.assertEqual((len(calls), ctx.exception.status), (1, 302))  # not retried, not followed
        self.assertNotIn(API_KEY, str(ctx.exception))

    def test_opener_has_no_redirect_following_handler(self):
        redirect = [h for h in re_._OPENER.handlers if isinstance(h, urllib.request.HTTPRedirectHandler)]
        self.assertEqual([type(h) for h in redirect], [re_._NoRedirectHandler])
        req = self.api_request()
        self.assertIsNone(redirect[0].redirect_request(req, None, 302, "Found", {}, "http://evil/"))

    def test_persistent_connection_failure_gives_up_cleanly(self):
        _, calls, sleeps, err = self.run_client([urllib.error.URLError("dns")] * 3, max_retries=2)
        self.assertEqual((len(calls), len(sleeps)), (3, 2))
        self.assertIn("gave up after 3 attempt(s); last error: connection error: URLError: dns", str(err))


class ExecutableRunnerTests(Base):
    def set_tests(self, *ids):
        y = self.root / "evals" / "skill-a" / "leak.yaml"
        text = y.read_text(encoding="utf-8").replace("kind: response", "kind: executable")
        y.write_text(text + "executable_tests:\n" + "".join(f"  - {i}\n" for i in ids), encoding="utf-8")

    def test_passed_failed_skipped_are_distinguished(self):
        base = "examples.test_fake.FakeTest."
        self.set_tests(base + "test_ok", base + "test_fail", base + "test_skip")
        code, r = self.report("--run-executable", "--python", sys.executable)
        ex = r["executable"]
        self.assertEqual(ex["status"], "run")
        self.assertEqual({x["test_id"].rsplit(".", 1)[1]: x["status"] for x in ex["results"]},
                         {"test_ok": "passed", "test_fail": "failed", "test_skip": "skipped"})
        self.assertIn("needs a gpu", next(x for x in ex["results"] if x["status"] == "skipped")["detail"])
        self.assertEqual(ex["counts"], {"passed": 1, "failed": 1, "skipped": 1})
        self.assertEqual(code, 1)  # a failing executable test fails the run

    def test_not_run_by_default_and_reported(self):
        self.set_tests("examples.test_fake.FakeTest.test_ok")
        _, r = self.report()
        self.assertEqual(r["executable"]["status"], "not-run")

    def test_cases_requiring_unavailable_resources_are_skipped_with_reason(self):
        self.set_tests("examples.test_fake.FakeTest.test_ok")
        y = self.root / "evals" / "skill-a" / "leak.yaml"
        y.write_text(y.read_text(encoding="utf-8") + "requires:\n  - gpu\n", encoding="utf-8")
        code, r = self.report("--run-executable")
        self.assertEqual(code, 0)
        res = r["executable"]["results"]
        self.assertEqual([(x["status"], x["detail"]) for x in res],
                         [("skipped", "case requires gpu; not run by the offline harness")])


if __name__ == "__main__":
    unittest.main()
