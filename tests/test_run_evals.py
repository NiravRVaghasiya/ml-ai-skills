"""Tests for scripts/run_evals.py: offline report schema and determinism,
recorded-response grading, regression comparison, model mode with the HTTP
layer mocked (no network, no API key needed), refusal to run model mode
without every explicit flag, retry/backoff, LLM-judge separation, and the
executable-test runner. Pure stdlib."""
from __future__ import annotations

import email.message
import hashlib
import http.client
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from unittest import mock

from test_evals import LEAK_FAIL, LEAK_PASS, make_repo

import eval_backends as eb  # scripts/ is on sys.path via testutil
import eval_lib as el
import run_evals as re_

FIXED_ENV = {"python_version": "3.x", "python_implementation": "CPython", "platform": "test",
             "git_revision": None, "git_dirty": None}
API_KEY = "sk-test-not-a-real-key-123"
JUDGE_COST = ("--judge-price-input-per-mtok", "1", "--judge-price-output-per-mtok", "1")


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

    COST_ARGS = ("--max-cost-usd", "1000", "--price-input-per-mtok", "1", "--price-output-per-mtok", "1")

    def model_args(self, *extra):
        return ("--provider", "anthropic", "--model", "model-x", "--allow-network", *self.COST_ARGS, *extra)


class OfflineReportTests(Base):
    def test_schema_and_provenance(self):
        code, r = self.report()
        self.assertEqual(code, 0)
        for key in ("schema", "schema_version", "harness_version", "generated_at", "nondeterministic_fields", "mode",
                    "environment", "dependencies", "dataset", "validation", "input_errors", "fixture_self_tests",
                    "graded", "executable", "skipped", "summary", "skill_uplift", "baseline", "model_run",
                    "grader_version", "provenance", "uplift", "verdict_rules", "model_evaluation"):
            self.assertIn(key, r)
        self.assertEqual((r["schema"], r["schema_version"], r["mode"]), (re_.REPORT_SCHEMA, 2, "offline"))
        self.assertEqual(r["model_evaluation"]["status"], "not-run")
        self.assertIn("no model configured", r["model_evaluation"]["reason"])
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
                         {"responses": 2, "gradable": 1, "passed": 1, "failed": 0, "errors": 0, "refusals": 0,
                          "not_gradable": 1, "pass_rate": 1.0})
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
        no_model = "not-run: no model configured"
        self.assert_refused(["--allow-network"], env, [no_model, "--provider (anthropic", "--model <model id>"])
        self.assert_refused(["--provider", "anthropic", "--model", "m", *self.COST_ARGS], env, ["--allow-network"])
        self.assert_refused(["--provider", "anthropic", "--allow-network", *self.COST_ARGS], env,
                            [no_model, "--model <model id>"])
        self.assert_refused(list(self.model_args()), {}, ["ANTHROPIC_API_KEY"])
        self.assert_refused(list(self.model_args()), {"ANTHROPIC_API_KEY": ""}, ["ANTHROPIC_API_KEY"])
        self.assert_refused(["--judge-model", "j"], env, ["judge options need responses to grade"])
        self.assert_refused(["--provider", "replay"], env, ["--replay-file"])

    def test_api_key_with_whitespace_or_control_characters_is_refused_without_echoing_it(self):
        # http.client rejects such a header value with a ValueError that quotes the key
        # (security review 2026-10-30); refuse up front and never print the value.
        for bad in (API_KEY + "\r", API_KEY + "\n", " " + API_KEY, API_KEY[:5] + "\x00" + API_KEY[5:]):
            with self.subTest(bad=repr(bad[-2:])):
                t = FakeTransport([message("x")])
                code, out, err = self.run_cli(*self.model_args(), env={"ANTHROPIC_API_KEY": bad}, transport=t)
                self.assertEqual(code, 2, err)
                self.assertEqual(t.requests, [])
                self.assertIn("without surrounding whitespace or control", err)
                self.assertNotIn(API_KEY.strip()[4:], err)

    def test_paid_providers_refuse_without_a_cost_ceiling_or_prices(self):
        env = {"ANTHROPIC_API_KEY": API_KEY}
        base = ["--provider", "anthropic", "--model", "m", "--allow-network"]
        self.assert_refused(base + ["--price-input-per-mtok", "1", "--price-output-per-mtok", "1"], env,
                            ["model mode refused", "--max-cost-usd"])
        self.assert_refused(base + ["--max-cost-usd", "5"], env, ["--price-input-per-mtok and --price-output-per-mtok"])
        self.assert_refused(["--provider", "claude-cli", "--model", "m", "--allow-network"], env, ["--max-cost-usd"])
        # Without the CLI's own --max-budget-usd nothing caps a single call, so prices become mandatory.
        self.assert_refused(["--provider", "claude-cli", "--model", "m", "--allow-network", "--max-cost-usd", "1",
                             "--no-cli-budget-flag"], env, ["required with --no-cli-budget-flag"])
        self.assert_refused(list(self.model_args("--max-cost-usd", "0")), env, ["--max-cost-usd must be > 0"])
        self.assert_refused(list(self.model_args("--endpoint", "http://api.example/v1/messages")), env,
                            ["--endpoint must be an https:// URL"])

    def test_worst_case_above_the_ceiling_is_refused_before_any_request(self):
        t = FakeTransport([message("x")])
        args = ["--provider", "anthropic", "--model", "m", "--allow-network", "--max-cost-usd", "0.001",
                "--price-input-per-mtok", "3", "--price-output-per-mtok", "15"]
        code, out, err = self.run_cli(*args, env={"ANTHROPIC_API_KEY": API_KEY}, transport=t)
        self.assertEqual((code, out, t.requests), (2, "", []))
        self.assertIn("not-run: the worst-case estimate", err)

    def test_model_only_flags_without_model_mode_are_refused_not_ignored(self):
        env = {"ANTHROPIC_API_KEY": API_KEY}
        saved = self.tmp / "saved.jsonl"
        for args, named in ((["--save-responses", str(saved)], "--save-responses"),
                            (["--temperature", "0"], "--temperature"),
                            (["--conditions", "with_skill"], "--conditions"),
                            (["--max-tokens", "10"], "--max-tokens"),
                            (["--anthropic-version", "2099-01-01"], "--anthropic-version")):
            with self.subTest(args=args):
                self.assert_refused(args, env, [named, "not-run: no model configured", "--provider (anthropic"])
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
        code, out, _ = self.run_cli(*self.model_args(*JUDGE_COST, "--judge-model", "judge-x", "--conditions", "with_skill"),
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
        _, out, _ = self.run_cli(*self.model_args(*JUDGE_COST, "--judge-model", "j", "--conditions", "with_skill"),
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
                _, out, _ = self.run_cli(*self.model_args(*JUDGE_COST, "--judge-model", "j", "--conditions", "with_skill"),
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

    def test_rejected_header_value_never_appears_in_the_error(self):
        # The real http.client path: putheader() rejects a trailing CR before any connection
        # is opened (port 1 on loopback is never contacted), quoting the value in its ValueError.
        secret = "sk-ant-NOT-REAL-0123456789"
        with self.assertRaises(eb.TransportError) as ctx:
            eb.urllib_transport("https://127.0.0.1:1/v1/messages", b"{}", {"x-api-key": secret + "\r"}, 1.0)
        self.assertNotIn(secret, str(ctx.exception))
        self.assertIsNone(ctx.exception.__cause__)
        self.assertTrue(ctx.exception.__suppress_context__)

    def test_persistent_connection_failure_gives_up_cleanly(self):
        _, calls, sleeps, err = self.run_client([urllib.error.URLError("dns")] * 3, max_retries=2)
        self.assertEqual((len(calls), len(sleeps)), (3, 2))
        self.assertIn("gave up after 3 attempt(s); last error: connection error: URLError: dns", str(err))

    class TruncatedResp(Resp):
        def read(self):
            raise http.client.IncompleteRead(b"partial", 100)

    def test_truncated_and_malformed_http_responses_are_retried_not_crashes(self):
        # http.client.HTTPException subclasses are not OSErrors; they used to escape and kill the run.
        ok = self.Resp({"content": [{"type": "text", "text": "ok"}], "model": "m"})
        outcomes = [self.TruncatedResp({}), http.client.BadStatusLine("garbage"), ok]
        msg, calls, sleeps, err = self.run_client(outcomes)
        self.assertIsNone(err)
        self.assertEqual((re_.response_text(msg), len(calls)), ("ok", 3))
        _, calls, _, err = self.run_client([self.TruncatedResp({})] * 2, max_retries=1)
        self.assertIn("connection error: IncompleteRead", str(err))
        # an HTTP error whose body is cut off keeps its status
        broken = urllib.error.HTTPError(re_.API_URL, 400, "err", self.headers(), None)
        broken.read = mock.Mock(side_effect=http.client.IncompleteRead(b"", 10))
        _, calls, _, err = self.run_client([broken])
        self.assertEqual((len(calls), err.status), (1, 400))

    def test_backend_maps_a_truncated_body_to_a_status_and_counts_unconfirmed_attempts(self):
        with mock.patch.object(re_._OPENER, "open", lambda req, timeout: self.TruncatedResp({})):
            client = re_.MessagesClient(API_KEY, sleep=lambda s: None, max_retries=2)
            c = eb.AnthropicBackend(client, "m").complete(eb.Request(system="s", user="u", max_tokens=10))
        self.assertEqual((c.status, c.attempts, c.meta["unconfirmed_attempts"]), ("error", 3, 3))
        self.assertIn("IncompleteRead", c.error)


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
        failed = next(x for x in ex["results"] if x["status"] == "failed")
        self.assertIn("Traceback", failed["output_tail"])  # the evidence for the failure is kept
        self.assertNotIn("output_tail", next(x for x in ex["results"] if x["status"] == "passed"))

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


# --------------------------------------------------------------------------
# Executable cases: skipped is reported as not evidence, opt-in env, --require-executed (#10)
# --------------------------------------------------------------------------

ENV_GATED_TESTS = """import os
import unittest


class Gated(unittest.TestCase):
    def test_slow(self):
        if os.environ.get("ML_AI_SKILLS_SLOW_EXAMPLES") != "1":
            self.skipTest("slow; set ML_AI_SKILLS_SLOW_EXAMPLES=1 to run")
"""


class ExecutableOptInTests(Base):
    TEST_ID = "examples.test_gated.Gated.test_slow"

    def setUp(self):
        super().setUp()
        (self.root / "tests" / "examples" / "test_gated.py").write_text(ENV_GATED_TESTS, encoding="utf-8")
        y = self.root / "evals" / "skill-a" / "leak.yaml"
        text = y.read_text(encoding="utf-8").replace("kind: response", "kind: executable")
        y.write_text(text + f"executable_tests:\n  - {self.TEST_ID}\n", encoding="utf-8")
        # The developer's shell (or CI's examples job) may set these; the tests decide them explicitly.
        cleaned = {k: v for k, v in os.environ.items()
                   if k not in (re_.SLOW_EXAMPLES_ENV, re_.REQUIRE_EXAMPLE_DEPS_ENV)}
        patcher = mock.patch.dict(os.environ, cleaned, clear=True)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_default_run_reports_skipped_as_not_evidence(self):
        code, out, err = self.run_cli("--run-executable")
        r = json.loads(out)
        [res] = r["executable"]["results"]
        self.assertEqual((res["status"], res["executed"], res["status_label"]),
                         ("skipped", False, "skipped (not evidence)"))
        self.assertIn("set ML_AI_SKILLS_SLOW_EXAMPLES=1", res["detail"])
        self.assertEqual(r["executable"]["counts"], {"passed": 0, "failed": 0, "skipped": 1})
        self.assertEqual(code, 0)  # a skip is not a failure by default ...
        self.assertIn("skipped (not evidence)", err)

    def test_executable_flags_without_run_executable_are_a_usage_error(self):
        # Without --run-executable nothing runs, so --require-executed would silently pass.
        for flag in ("--require-executed", "--slow-examples", "--require-example-deps"):
            with self.subTest(flag=flag):
                code, out, err = self.run_cli(flag)
                self.assertEqual((code, out), (2, ""))
                self.assertIn(f"{flag} need(s) --run-executable", err)

    def test_require_executed_fails_on_a_skip(self):
        code, _, err = self.run_cli("--run-executable", "--require-executed")
        self.assertEqual(code, 1)  # ... but it is never evidence
        self.assertIn("--require-executed: 1 executable test(s) were skipped", err)

    def test_slow_examples_flag_runs_the_opt_in_test(self):
        code, out, _ = self.run_cli("--run-executable", "--slow-examples", "--require-executed")
        r = json.loads(out)
        self.assertEqual(code, 0)
        self.assertEqual(r["executable"]["results"][0]["status"], "passed")
        self.assertEqual(r["executable"]["env_overrides"], {"ML_AI_SKILLS_SLOW_EXAMPLES": "1"})

    def test_requires_blocked_case_counts_as_skipped_for_require_executed(self):
        y = self.root / "evals" / "skill-a" / "leak.yaml"
        y.write_text(y.read_text(encoding="utf-8") + "requires:\n  - gpu\n", encoding="utf-8")
        code, out, _ = self.run_cli("--run-executable", "--slow-examples", "--require-executed")
        [res] = json.loads(out)["executable"]["results"]
        self.assertEqual((code, res["status"], res["executed"]), (1, "skipped", False))


# --------------------------------------------------------------------------
# Backends, statuses, budget, resume, provenance, redaction
# --------------------------------------------------------------------------

def leak_answer(req: eb.Request) -> str:
    return LEAK_PASS if "<skill name=" in req.system else LEAK_FAIL


class FakeFactory:
    """backend_factory for main(): an in-process answering backend (and judge) per role."""

    def __init__(self, answer=leak_answer, judge=None, *, paid=False, pricing=None):
        self.backends = {"answer": eb.CallableBackend(answer, paid=paid, pricing=pricing)}
        if judge:
            self.backends["judge"] = eb.CallableBackend(judge, provider="fake-judge", model="judge-m")

    def __call__(self, role, args):
        return self.backends[role]


REPLAY = ("--provider", "replay", "--replay-file")


def run_main(root: Path, *args, env=None, **kw) -> tuple[int, str, str]:
    out, err = io.StringIO(), io.StringIO()
    code = re_.main(["--root", str(root), *args], env=env or {}, now=lambda: "2026-01-01T00:00:00+00:00",
                    environment=FIXED_ENV, stdout=out, stderr=err, **kw)
    return code, out.getvalue(), err.getvalue()


class ReplayAndStatusTests(Base):
    def replay_file(self, rows):
        return str(self.responses_file(rows, "replay.jsonl"))

    def test_replay_runs_the_full_pipeline_offline_and_reports_uplift(self):
        f = self.replay_file([
            {"case_id": "skill-a/leak", "condition": "with_skill", "response": LEAK_PASS},
            {"case_id": "skill-a/leak", "condition": "without_skill", "response": LEAK_FAIL},
            {"case_id": "skill-a/plain", "condition": "with_skill", "response": "steps"},
            {"case_id": "skill-a/plain", "condition": "without_skill", "response": "steps"}])
        code, r = self.report(*REPLAY, f)
        self.assertEqual(code, 0)
        self.assertEqual(r["mode"], "model")
        self.assertEqual(r["model_run"]["backend"]["provider"], "replay")
        self.assertEqual(r["model_evaluation"], {"status": "run", "complete": True, "attempts": 4,
                                                 "by_status": {"ok": 4}, "provider": "replay",
                                                 "model": "replay:replay.jsonl"})
        g = {(x["case_id"], x["condition"]): x for x in r["graded"]}
        self.assertEqual(g[("skill-a/leak", "with_skill")]["verdict"]["final"], "structural-pass")
        self.assertEqual(g[("skill-a/leak", "without_skill")]["verdict"]["final"], "fail")  # must_not_include hit
        self.assertTrue(g[("skill-a/leak", "without_skill")]["verdict"]["critical_error"])
        self.assertEqual(g[("skill-a/plain", "with_skill")]["verdict"]["final"], "ungraded")
        up = r["uplift"]
        self.assertEqual((up["structural"]["status"], up["structural"]["delta"]), ("measured", 1.0))
        self.assertEqual(up["critical_error"]["delta"], -0.5)  # leak: 0 vs 1 critical errors; plain: 0 vs 0
        self.assertEqual(up["correct"]["status"], "unmeasured")  # no rubric verdict: never "correct"
        self.assertEqual(r["summary"]["final"]["with_skill"]["correct"]["n"], 0)
        self.assertEqual(r["provenance"]["replay_file_sha256"], el.sha256_file(Path(f)))

    def test_failed_attempts_are_recorded_never_passes_and_break_pairs(self):
        f = self.replay_file([
            {"case_id": "skill-a/leak", "condition": "with_skill", "status": "rate-limited", "error": "429"},
            {"case_id": "skill-a/leak", "condition": "without_skill", "response": LEAK_FAIL},
            {"case_id": "skill-a/plain", "condition": "with_skill", "status": "timeout"},
            {"case_id": "skill-a/plain", "condition": "without_skill", "status": "refusal", "response": ""}])
        code, r = self.report(*REPLAY, f)
        self.assertEqual(code, 0)
        g = {(x["case_id"], x["condition"]): x for x in r["graded"]}
        self.assertEqual(g[("skill-a/leak", "with_skill")]["verdict"]["final"], "rate-limited")
        self.assertIsNone(g[("skill-a/leak", "with_skill")]["hard"]["passed"])
        self.assertEqual(g[("skill-a/plain", "with_skill")]["verdict"]["final"], "timeout")
        self.assertEqual(g[("skill-a/plain", "without_skill")]["verdict"]["final"], "refusal")
        self.assertFalse(r["model_evaluation"]["complete"])
        self.assertEqual(r["summary"]["hard"]["with_skill"]["errors"], 2)
        self.assertEqual(r["summary"]["hard"]["without_skill"]["refusals"], 1)
        su = r["skill_uplift"]
        self.assertEqual(su["status"], "unmeasured")
        self.assertEqual(su["incomplete_cases"][0]["case_id"], "skill-a/leak")
        self.assertIn("with_skill: 1 of 1 attempt(s) have no structural outcome", su["incomplete_cases"][0]["reasons"])
        self.assertEqual(self.run_cli(*REPLAY, f, "--strict")[0], 1)

    def test_samples_average_per_case_and_an_errored_sample_makes_the_pair_incomplete(self):
        rows = [{"case_id": "skill-a/leak", "condition": "with_skill", "sample": s, "response": LEAK_PASS}
                for s in (0, 1)]
        rows += [{"case_id": "skill-a/leak", "condition": "without_skill", "sample": 0, "response": LEAK_PASS},
                 {"case_id": "skill-a/leak", "condition": "without_skill", "sample": 1, "response": LEAK_FAIL}]
        code, r = self.report(*REPLAY, self.replay_file(rows), "--samples", "2", "--case", "leak")
        self.assertEqual(code, 0)
        st = r["uplift"]["structural"]
        self.assertEqual((st["paired_cases"], st["samples_per_arm"], st["delta"]), (1, [2], 0.5))
        self.assertEqual((st["with_skill_rate"]["k"], st["with_skill_rate"]["n"]), (2, 2))
        self.assertEqual([g["sample"] for g in r["graded"]], [0, 1, 0, 1])
        rows[3] = {**rows[3], "status": "server-error"}
        _, r = self.report(*REPLAY, self.replay_file(rows), "--samples", "2", "--case", "leak")
        self.assertEqual(r["uplift"]["structural"]["status"], "unmeasured")

    def test_dry_run_sends_nothing_and_needs_no_key_or_network(self):
        t = FakeTransport([message("x")])
        code, out, err = self.run_cli("--provider", "anthropic", "--model", "m", "--dry-run", "--max-tokens", "100",
                                      "--price-input-per-mtok", "3", "--price-output-per-mtok", "15",
                                      transport=t)
        self.assertEqual((code, t.requests), (0, []), err)
        plan = json.loads(out)
        self.assertEqual((plan["schema"], plan["status"]), (re_.PLAN_SCHEMA, "not-run"))
        self.assertEqual(plan["preflight"]["answer_requests"], 4)
        self.assertGreater(plan["preflight"]["worst_case_usd"], 0)
        self.assertIn("worst-case cost $", err)
        p = plan["provenance"]
        self.assertEqual((p["report_schema"], p["git_revision"], p["git_dirty"]), (re_.PLAN_SCHEMA, None, None))
        self.assertEqual(p["config"]["model"], "m")
        self.assertEqual(p["config_sha256"], re_._json_sha(p["config"]))

    def test_regex_only_critical_fail_in_both_arms_leaves_correct_unmeasured(self):
        # Regression: a must_not_include hit in both arms with no rubric used to make the correct layer
        # "measured" at 0 vs 0 while summary.final.correct had n=0.
        f = self.replay_file([{"case_id": "skill-a/leak", "condition": c, "response": LEAK_FAIL}
                              for c in ("with_skill", "without_skill")])
        _, r = self.report(*REPLAY, f, "--case", "leak")
        self.assertEqual([g["verdict"]["final"] for g in r["graded"]], ["fail", "fail"])
        self.assertEqual(r["uplift"]["correct"]["status"], "unmeasured")
        self.assertEqual(r["summary"]["final"]["with_skill"]["correct"]["n"], 0)
        self.assertEqual((r["uplift"]["critical_error"]["status"], r["uplift"]["critical_error"]["delta"]),
                         ("measured", 0.0))


class BudgetTests(Base):
    env = {"ANTHROPIC_API_KEY": API_KEY}

    def test_ceiling_stops_calls_before_it_is_exceeded(self):
        # Each call uses all 100 max_tokens at $1000/Mtok output = $0.10, exactly its worst case.
        t = FakeTransport([message(LEAK_PASS, usage={"input_tokens": 0, "output_tokens": 100})])
        args = ("--provider", "anthropic", "--model", "m", "--allow-network", "--max-tokens", "100",
                "--price-input-per-mtok", "0", "--price-output-per-mtok", "1000", "--max-cost-usd", "0.25")
        code, out, err = self.run_cli(*args, env=self.env, transport=t)
        self.assertEqual((code, t.requests), (2, []))  # worst case $0.40 > $0.25: refused up front
        code, out, err = self.run_cli(*args, "--allow-partial-budget", env=self.env, transport=t)
        r = json.loads(out)
        self.assertEqual(len(t.requests), 2)
        self.assertEqual(sorted(g["status"] for g in r["graded"]),
                         ["budget-exhausted", "budget-exhausted", "ok", "ok"])
        b = r["model_run"]["budget"]
        self.assertLessEqual(b["spent_usd_estimate"], 0.25)
        self.assertTrue(b["exhausted"])
        self.assertGreater(r["model_run"]["preflight"]["worst_case_usd"], 0.25)
        self.assertIn("INCOMPLETE", err)

    def test_unpriced_paid_backend_stops_once_the_ceiling_is_reached(self):
        backend = eb.CallableBackend(lambda req: eb.Completion(status="ok", text=LEAK_PASS, cost_usd=0.6), paid=True)
        code, out, err = run_main(self.root, "--provider", "claude-cli", "--model", "m", "--allow-network",
                                  "--max-cost-usd", "1", backend_factory=lambda role, args: backend)
        r = json.loads(out)
        self.assertEqual(len(backend.calls), 2)  # $0 < $1 -> call; $0.6 < $1 -> call; $1.2 -> stop
        self.assertEqual(sorted(g["status"] for g in r["graded"]),
                         ["budget-exhausted", "budget-exhausted", "ok", "ok"])

    def test_unpriced_paid_call_reporting_no_cost_stops_the_run(self):
        # Regression: a claude CLI result without total_cost_usd (and no --price-*) used to be charged
        # nothing, so the ceiling never bound and every call went out (20 calls at a $0.01 ceiling).
        calls = []

        def run(argv, **kw):
            if argv[-1] == "--version":
                return subprocess.CompletedProcess(argv, 0, "x\n", "")
            calls.append(argv)
            return subprocess.CompletedProcess(argv, 0, json.dumps(
                {"type": "result", "subtype": "success", "is_error": False, "result": LEAK_PASS}) + "\n", "")
        code, out, err = run_main(self.root, "--provider", "claude-cli", "--model", "m", "--allow-network",
                                  "--max-cost-usd", "0.01", "--samples", "5", cli_runner=run)
        r = json.loads(out)
        self.assertEqual(len(calls), 1)
        statuses = [g["status"] for g in r["graded"]]
        self.assertEqual((statuses.count("ok"), statuses.count("budget-exhausted")), (1, 19))
        b = r["model_run"]["budget"]
        self.assertEqual((b["exhausted"], b["uncosted_calls"]), (True, 1))
        self.assertIn("spend can no longer be bounded", b["stop_reason"])
        self.assertIn("spend can no longer be bounded",
                      next(g["error"] for g in r["graded"] if g["status"] == "budget-exhausted"))
        self.assertFalse(r["model_evaluation"]["complete"])
        self.assertIn("INCOMPLETE", err)

    def test_unconfirmed_http_attempts_are_charged_a_worst_case_each(self):
        # One timeout, then success: the timed-out request may have been billed.
        t = FakeTransport([re_.TransportError("TimeoutError: timed out"),
                           message(LEAK_PASS, usage={"input_tokens": 0, "output_tokens": 10})])
        args = ("--provider", "anthropic", "--model", "m", "--allow-network", "--max-tokens", "100",
                "--price-input-per-mtok", "0", "--price-output-per-mtok", "1000", "--max-cost-usd", "5",
                "--case", "leak", "--conditions", "with_skill", "--backoff-base", "0")
        code, out, err = self.run_cli(*args, env=self.env, transport=t)
        self.assertEqual(code, 0, err)
        [g] = json.loads(out)["graded"]
        self.assertEqual((g["status"], g["attempts"]), ("ok", 2))
        # observed $0.01 + one worst case ($0.10 output; input priced at 0)
        self.assertAlmostEqual(json.loads(out)["model_run"]["budget"]["spent_usd_estimate"], 0.11)

    def test_cli_that_cannot_start_is_charged_nothing(self):
        def run(argv, **kw):
            if argv[-1] == "--version":
                return subprocess.CompletedProcess(argv, 0, "x\n", "")
            raise FileNotFoundError("claude")
        _, out, _ = run_main(self.root, "--provider", "claude-cli", "--model", "m", "--allow-network",
                             "--max-cost-usd", "1", cli_runner=run)
        r = json.loads(out)
        self.assertEqual({g["status"] for g in r["graded"]}, {"error"})  # every case tried, none charged
        self.assertEqual((r["model_run"]["budget"]["spent_usd_estimate"], r["model_run"]["budget"]["exhausted"]),
                         (0.0, False))


class RunDirResumeTests(Base):
    def setUp(self):
        super().setUp()
        self.dummy = self.responses_file([], "dummy.jsonl")  # satisfies --replay-file; the factory answers
        self.run_dir = self.tmp / "run"

    def run_fake(self, factory, *args):
        code, out, err = run_main(self.root, *REPLAY, str(self.dummy), *args, backend_factory=factory)
        return code, (json.loads(out) if out else None), err

    def test_resume_reuses_only_completed_attempts_with_matching_provenance(self):
        fail_once = {"n": 0}

        def flaky(req):
            if req.case_id == "skill-a/leak" and req.condition == "with_skill" and fail_once["n"] == 0:
                fail_once["n"] += 1
                return eb.Completion(status="timeout", error="timed out")
            return leak_answer(req)
        f1 = FakeFactory(flaky)
        code, r, _ = self.run_fake(f1, "--run-dir", str(self.run_dir))
        self.assertEqual((code, len(f1.backends["answer"].calls)), (0, 4))
        self.assertFalse(r["model_evaluation"]["complete"])
        self.assertTrue((self.run_dir / "manifest.json").is_file())
        self.assertTrue((self.run_dir / "report.json").is_file())
        recs = [json.loads(x) for x in (self.run_dir / "records.jsonl").read_text(encoding="utf-8").splitlines()]
        self.assertEqual(len(recs), 4)
        self.assertEqual({x["completion"]["status"] for x in recs}, {"ok", "timeout"})
        # without --resume a used run dir is refused
        code, _, err = self.run_fake(FakeFactory(), "--run-dir", str(self.run_dir))
        self.assertEqual(code, 2)
        self.assertIn("already has records", err)
        # --resume re-attempts only the timed-out key
        f2 = FakeFactory()
        code, r, _ = self.run_fake(f2, "--run-dir", str(self.run_dir), "--resume")
        calls = [(q.case_id, q.condition) for q in f2.backends["answer"].calls]
        self.assertEqual(calls, [("skill-a/leak", "with_skill")])
        self.assertTrue(r["model_evaluation"]["complete"])
        self.assertEqual(r["model_run"]["resume"]["reused"], 3)
        self.assertEqual(r["model_run"]["resume"]["reattempted"], 1)
        # editing SKILL.md changes the with_skill prompt: those answers are stale, without_skill ones are not
        sk = self.root / "skill-a" / "SKILL.md"
        sk.write_text(sk.read_text(encoding="utf-8") + "\nMore guidance.\n", encoding="utf-8")
        f3 = FakeFactory()
        _, r, _ = self.run_fake(f3, "--run-dir", str(self.run_dir), "--resume")
        self.assertEqual(sorted((q.case_id, q.condition) for q in f3.backends["answer"].calls),
                         [("skill-a/leak", "with_skill"), ("skill-a/plain", "with_skill")])
        self.assertEqual(r["model_run"]["resume"]["stale_ignored"], 2)

    def test_truncated_last_line_is_skipped_and_counted(self):
        self.run_fake(FakeFactory(), "--run-dir", str(self.run_dir))
        with (self.run_dir / "records.jsonl").open("a", encoding="utf-8") as f:
            f.write('{"type": "answer", "key": "skill-a/leak|with')  # crash mid-write
        f2 = FakeFactory()
        code, r, _ = self.run_fake(f2, "--run-dir", str(self.run_dir), "--resume")
        self.assertEqual((code, len(f2.backends["answer"].calls)), (0, 0))
        self.assertEqual(r["model_run"]["resume"]["corrupt_lines"], 1)

    def test_resume_without_records_is_a_usage_error(self):
        code, _, err = self.run_fake(FakeFactory(), "--run-dir", str(self.run_dir), "--resume")
        self.assertEqual(code, 2)
        self.assertIn("does not exist or is empty", err)
        code, _, err = self.run_fake(FakeFactory(), "--resume")
        self.assertEqual(code, 2)
        self.assertIn("--resume needs --run-dir", err)

    def test_judge_results_are_recorded_and_reused(self):
        def judge(req):
            n_mn = req.user.split("must_not (")[1].count("\n0. ")
            return json.dumps({"expected_behavior": [{"index": 0, "met": True, "reason": "r"}],
                               "must_not": [{"index": 0, "violated": False, "reason": "r"}] if n_mn else []})
        jargs = ("--judge-provider", "replay", "--judge-replay-file", str(self.dummy))
        f1 = FakeFactory(judge=judge)
        code, r, err = self.run_fake(f1, "--run-dir", str(self.run_dir), *jargs)
        self.assertEqual(code, 0, err)
        self.assertEqual(len(f1.backends["judge"].calls), 4)
        g = {(x["case_id"], x["condition"]): x for x in r["graded"]}
        self.assertEqual(g[("skill-a/leak", "with_skill")]["verdict"]["final"], "correct")
        self.assertEqual(g[("skill-a/leak", "without_skill")]["verdict"]["final"], "fail")  # regex must_not_include
        self.assertEqual(g[("skill-a/plain", "with_skill")]["verdict"]["final"], "correct")
        self.assertEqual(r["uplift"]["correct"]["delta"], 0.5)
        self.assertEqual(r["provenance"]["judge_prompt_sha256"], re_.eg.judge_prompt_sha256())
        f2 = FakeFactory(judge=judge)
        _, r2, _ = self.run_fake(f2, "--run-dir", str(self.run_dir), "--resume", *jargs)
        self.assertEqual((len(f2.backends["answer"].calls), len(f2.backends["judge"].calls)), (0, 0))
        self.assertEqual(r2["uplift"], r["uplift"])
        # Editing only the judge prompt re-buys the judge calls, never the answers.
        f3 = FakeFactory(judge=judge)
        with mock.patch.object(re_.eg, "JUDGE_SYSTEM_PROMPT", re_.eg.JUDGE_SYSTEM_PROMPT + " Be strict."):
            _, r3, _ = self.run_fake(f3, "--run-dir", str(self.run_dir), "--resume", *jargs)
        self.assertEqual((len(f3.backends["answer"].calls), len(f3.backends["judge"].calls)), (0, 4))
        self.assertEqual(r3["provenance"]["prompt_template_sha256"], r["provenance"]["prompt_template_sha256"])
        self.assertNotEqual(r3["provenance"]["judge_prompt_sha256"], r["provenance"]["judge_prompt_sha256"])


class ProvenanceTests(Base):
    def test_report_provenance_block(self):
        rows = self.responses_file([{"case_id": "skill-a/leak", "condition": "with_skill", "response": LEAK_PASS}])
        _, r = self.report("--responses", str(rows))
        p = r["provenance"]
        for key in ("git_revision", "git_dirty", "dataset_sha256", "skill_sha256", "case_sha256",
                    "prompt_template_sha256", "grader_version", "harness_version", "config", "config_sha256",
                    "responses_sha256", "schema_version"):
            self.assertIn(key, p)
        self.assertEqual(p["dataset_sha256"], r["dataset"]["dataset_sha256"])
        self.assertEqual(p["responses_sha256"], el.sha256_file(rows))
        self.assertEqual(p["config"]["responses"], str(rows))
        self.assertNotIn("root", p["config"])
        self.assertEqual(r["verdict_rules"], list(re_.eg.VERDICT_RULES))

    def test_compare_reads_schema_v1_reports_and_rejects_unknown_versions(self):
        row = {"case_id": "skill-a/leak", "condition": "with_skill", "response": LEAK_PASS}
        p = self.tmp / "new.json"
        self.run_cli("--responses", str(self.responses_file([row])), "--out", str(p))
        v1 = json.loads(p.read_text(encoding="utf-8"))
        v1["schema_version"] = 1
        for k in ("provenance", "uplift", "verdict_rules", "model_evaluation", "grader_version"):
            v1.pop(k)
        for g in v1["graded"]:
            for k in ("sample", "rubric", "verdict"):
                g.pop(k)
        old = self.tmp / "v1.json"
        old.write_text(json.dumps(v1), encoding="utf-8")
        code, out, _ = self.run_cli("--compare", str(old), str(p))
        self.assertEqual((code, json.loads(out)["regressions"]), (0, []))
        v1["schema_version"] = 99
        old.write_text(json.dumps(v1), encoding="utf-8")
        code, _, err = self.run_cli("--compare", str(old), str(p))
        self.assertEqual(code, 2)
        self.assertIn("schema_version 99", err)


class RubricLabelTests(Base):
    def test_labels_drive_the_combined_verdict(self):
        rows = self.responses_file([
            {"case_id": "skill-a/leak", "condition": "with_skill", "response": LEAK_PASS},
            {"case_id": "skill-a/leak", "condition": "without_skill", "response": LEAK_PASS + " Extra."},
            {"case_id": "skill-a/plain", "condition": "with_skill", "response": "steps"}])
        sha = lambda t: hashlib.sha256(t.encode("utf-8")).hexdigest()  # noqa: E731
        labels = self.tmp / "labels.jsonl"
        labels.write_text("\n".join(json.dumps(x) for x in [
            {"case_id": "skill-a/leak", "response_sha256": sha(LEAK_PASS), "expected_behavior": [True],
             "must_not": [False], "label_source": "human"},
            {"case_id": "skill-a/leak", "response_sha256": sha(LEAK_PASS + " Extra."), "expected_behavior": [True],
             "must_not": [True], "label_source": "human"},
            {"case_id": "skill-a/plain", "response_sha256": sha("steps"), "expected_behavior": [False],
             "must_not": [], "label_source": "agent-adjudicated"}]) + "\n", encoding="utf-8")
        code, r = self.report("--responses", str(rows), "--rubric-labels", str(labels))
        self.assertEqual(code, 0)
        g = {(x["case_id"], x["condition"]): x for x in r["graded"]}
        self.assertEqual(g[("skill-a/leak", "with_skill")]["verdict"]["final"], "correct")
        bad = g[("skill-a/leak", "without_skill")]["verdict"]
        self.assertEqual((bad["final"], bad["critical_sources"]), ("fail", ["rubric (labels:human)"]))
        self.assertEqual(g[("skill-a/plain", "with_skill")]["verdict"]["final"], "incorrect")
        self.assertEqual(g[("skill-a/plain", "with_skill")]["rubric"]["source"], "labels:agent-adjudicated")
        fin = r["summary"]["final"]["with_skill"]
        self.assertEqual((fin["correct"]["k"], fin["correct"]["n"]), (1, 2))
        self.assertIsNotNone(fin["correct"]["ci95"])

    def test_bad_label_file_is_an_input_error(self):
        labels = self.tmp / "labels.jsonl"
        labels.write_text('{"case_id": "x"}\n', encoding="utf-8")
        code, r = self.report("--rubric-labels", str(labels))
        self.assertEqual(code, 1)
        self.assertIn("needs case_id, response_sha256", r["input_errors"][0])


class RedactionTests(Base):
    SECRET = "sk-ant-api03-super-secret-value-XYZ"
    OTHER = "ghp_other_token_value_123456"

    def test_secrets_never_reach_reports_records_logs_or_saved_responses(self):
        env = {"ANTHROPIC_API_KEY": self.SECRET, "SOME_SERVICE_TOKEN": self.OTHER}
        # A misbehaving proxy that echoes the key in an error body, and a "model" that echoes another secret.
        echo = (400, {"error": {"type": "invalid_request_error", "message": f"bad header {self.SECRET}"}})

        def respond(body):
            if body["messages"][0]["content"].startswith("Walk"):
                return echo
            return message(f"{LEAK_PASS} {self.OTHER}")
        t = FakeTransport([respond])
        run_dir, saved, out_file = self.tmp / "run", self.tmp / "saved.jsonl", self.tmp / "report.json"
        code, out, err = self.run_cli(*self.model_args("--run-dir", str(run_dir), "--save-responses", str(saved),
                                                       "--out", str(out_file)), env=env, transport=t)
        self.assertEqual(code, 0, err)
        self.assertTrue(any(q["headers"]["x-api-key"] == self.SECRET for q in t.requests))  # the key was used
        texts = {"stderr": err, "report": out_file.read_text(encoding="utf-8"),
                 "saved": saved.read_text(encoding="utf-8")}
        texts.update({p.name: p.read_text(encoding="utf-8") for p in run_dir.iterdir()})
        self.assertIn("records.jsonl", texts)
        for name, text in texts.items():
            with self.subTest(file=name):
                self.assertNotIn(self.SECRET, text)
                self.assertNotIn(self.OTHER, text)
        self.assertIn(eb.REDACTED, texts["report"])
        self.assertIn(eb.REDACTED, texts["records.jsonl"])
        self.assertEqual(json.loads(texts["report"])["provenance"]["config"]["api_key_env"], "ANTHROPIC_API_KEY")


class ClaudeCliBackendTests(Base):
    CLI_OK = {"type": "result", "subtype": "success", "is_error": False, "result": LEAK_PASS, "num_turns": 1,
              "total_cost_usd": 0.01, "usage": {"input_tokens": 5, "output_tokens": 7},
              "modelUsage": {"model-cli-1": {}}}

    def fake_runner(self, payload=None, *, exc=None, returncode=0, stdout=None):
        calls = []

        def run(argv, **kw):
            if argv[-1] == "--version":
                return subprocess.CompletedProcess(argv, 0, "9.9.9 (fake)\n", "")
            sp = argv[argv.index("--system-prompt-file") + 1]
            calls.append({"argv": argv, "system": Path(sp).read_text(encoding="utf-8"), "cwd": kw["cwd"],
                          "input": kw["input"], "timeout": kw["timeout"]})
            if exc:
                raise exc
            body = stdout if stdout is not None else json.dumps(payload or self.CLI_OK) + "\n"
            return subprocess.CompletedProcess(argv, returncode, body, "boom")
        return run, calls

    def cli_args(self, *extra):
        return ("--provider", "claude-cli", "--model", "cli-model", "--allow-network", "--max-cost-usd", "2",
                "--conditions", "with_skill", "--case", "leak", *extra)

    def test_priced_cli_backend_claims_no_per_call_worst_case(self):
        # The CLI ignores max_tokens, so a price x max_tokens "worst case" would understate a
        # call and let --no-cli-budget-flag runs overshoot the ceiling (security review 2026-10-30).
        backend = eb.ClaudeCliBackend("m", pricing=eb.Pricing(3.0, 15.0), budget_flag=False)
        req = eb.Request(system="s", user="u", max_tokens=16)
        self.assertIsNone(backend.worst_case_cost(req))

    def test_argv_stdin_isolation_and_parsing(self):
        run, calls = self.fake_runner()
        code, out, err = run_main(self.root, *self.cli_args("--timeout", "33", "--claude-cli-arg=--bare"),
                                  cli_runner=run)
        self.assertEqual(code, 0, err)
        [c] = calls
        argv = c["argv"]
        self.assertEqual(argv[:4], ["claude", "-p", "--output-format", "json"])
        for flag in ("--disable-slash-commands", "--strict-mcp-config", "--no-session-persistence"):
            self.assertIn(flag, argv)
        self.assertEqual(argv[argv.index("--tools") + 1], "")
        self.assertEqual(argv[argv.index("--model") + 1], "cli-model")
        self.assertEqual(argv[argv.index("--max-budget-usd") + 1], "2.0000")
        self.assertEqual(argv[-1], "--bare")
        self.assertIn('<skill name="skill-a">', c["system"])  # SKILL.md is the system prompt, not the user turn
        self.assertEqual(c["input"].strip(), "Can I scale the whole dataset before splitting?")
        self.assertEqual(c["timeout"], 33.0)
        self.assertFalse(Path(c["cwd"]).exists())  # temporary working directory removed
        self.assertNotIn(str(self.root), c["cwd"])
        r = json.loads(out)
        [g] = r["graded"]
        self.assertEqual((g["status"], g["model_reported"], g["cost_usd"]), ("ok", "model-cli-1", 0.01))
        self.assertTrue(g["hard"]["passed"])
        self.assertEqual(r["model_run"]["backend"]["cli_version"], "9.9.9 (fake)")
        self.assertEqual(r["model_run"]["budget"]["spent_usd_estimate"], 0.01)

    def test_cli_failures_map_to_explicit_statuses(self):
        cases = {
            "timeout": dict(exc=subprocess.TimeoutExpired("claude", 1)),
            "error": dict(returncode=1, stdout="not json"),
            "budget-exhausted": dict(payload={**self.CLI_OK, "is_error": True, "subtype": "error_max_budget_usd"}),
        }
        for status, kw in cases.items():
            with self.subTest(status=status):
                run, _ = self.fake_runner(**kw)
                _, out, _ = run_main(self.root, *self.cli_args(), cli_runner=run)
                [g] = json.loads(out)["graded"]
                self.assertEqual((g["status"], g["verdict"]["final"], g["hard"]["passed"]), (status, status, None))

    def test_real_subprocess_with_a_fake_cli_and_a_bounded_timeout(self):
        script = self.tmp / "fake_claude.py"
        script.write_text(
            "import json, sys, time\n"
            "argv = sys.argv[1:]\n"
            "if '--sleep' in argv:\n"
            "    time.sleep(30)\n"
            "system = open(argv[argv.index('--system-prompt-file') + 1], encoding='utf-8').read()\n"
            "user = sys.stdin.read()\n"
            "print(json.dumps({'type': 'result', 'subtype': 'success', 'is_error': False,\n"
            "                  'result': 'echo:' + str(len(system)) + ':' + user.strip(), 'total_cost_usd': 0.0}))\n",
            encoding="utf-8")
        backend = eb.ClaudeCliBackend("m", command=[sys.executable, str(script)], timeout=60)
        c = backend.complete(eb.Request(system="SYS", user="hello", max_tokens=10))
        self.assertEqual((c.status, c.text), ("ok", "echo:3:hello"))
        slow = eb.ClaudeCliBackend("m", command=[sys.executable, str(script)], timeout=2, extra_args=("--sleep",))
        c = slow.complete(eb.Request(system="SYS", user="hello", max_tokens=10))
        self.assertEqual(c.status, "timeout")


# --------------------------------------------------------------------------
# Labelled-set grading CLI (--grade-labelled / --grade-heldout)
# --------------------------------------------------------------------------

def item(item_id, text, label, **kw):
    base = {"item_id": item_id, "case_id": "skill-a/leak", "skill": "skill-a", "partition": "heldout",
            "intended_label": label if label != "unresolved" else "pass", "final_label": label,
            "label_source": "agent-adjudicated", "answer_text": text,
            "adjudicated_labels": [{"labeler": "labeler-a", "label": label}, {"labeler": "labeler-b", "label": label}]}
    base.update(kw)
    return {k: v for k, v in base.items() if v is not None}


class GradeLabelledCliTests(Base):
    def write_manifest(self, items, partition="heldout") -> Path:
        d = self.root / "evals" / "_grading" / partition
        d.mkdir(parents=True, exist_ok=True)
        p = d / "manifest.jsonl"
        p.write_text("".join(json.dumps(i) + "\n" for i in items), encoding="utf-8")
        return p

    def test_heldout_scoring_with_exclusions(self):
        self.write_manifest([
            item("ok-1", "Split first; otherwise statistics leak.", "pass"),
            item("ok-missed", "Fit the scaler on the training portion only to avoid contamination.", "pass"),
            item("bad-caught", "Yes, scale it all.", "fail"),
            item("bad-missed", "Leakage is a myth; split first if you like, it changes nothing.", "fail"),
            item("unsure", "Maybe.", "unresolved", adjudicated_labels=[{"labeler": "a", "label": "pass"},
                                                                       {"labeler": "b", "label": "fail"}]),
            item("overlap", LEAK_PASS, "pass"),
            item("unknown", "x", "pass", case_id="skill-a/nope"),
        ])
        code, out, err = self.run_cli("--grade-heldout")
        self.assertEqual(code, 0, err)
        r = json.loads(out)
        self.assertEqual((r["schema"], r["partition"], r["status"]["status"]),
                         ("ml-ai-skills/grader-calibration", "heldout", "measured"))
        self.assertEqual(r["counts"], {"items": 7, "usable": 4,
                                       "excluded": {"unresolved": 1, "unknown_case": 1, "dev_overlap": 1}})
        a = r["graders"]["regex"]["aggregate"]
        self.assertEqual((a["tp"], a["fn"], a["tn"], a["fp"]), (1, 1, 1, 1))
        self.assertEqual((a["fpr"]["rate"], a["fpr"]["n"]), (0.5, 2))
        self.assertEqual(a["fpr"]["ci95"], re_.eg.wilson(1, 2))
        self.assertIn("skill-a", r["graders"]["regex"]["by_skill"])
        self.assertIn("not measured: no item is human-labelled", r["status"]["against_humans"])
        self.assertIn("FPR 0.500", err)
        self.assertEqual(r["manifest"]["path"], "evals/_grading/heldout/manifest.jsonl")

    def test_missing_or_empty_manifest_is_unmeasured_not_a_pass(self):
        code, out, _ = self.run_cli("--grade-heldout")
        r = json.loads(out)
        self.assertEqual((code, r["status"]["status"]), (0, "unmeasured"))
        self.assertIn("no labelled set at", r["warnings"][0])
        self.write_manifest([])
        _, out, _ = self.run_cli("--grade-labelled", "heldout")
        self.assertEqual(json.loads(out)["status"]["reason"], "the labelled set is empty")

    def test_manifest_errors_exit_1(self):
        self.write_manifest([
            item("dev-labelled", "x", "pass", label_source="fixture-author", adjudicated_labels=[]),
            item("wrong-partition", "Split first; leak.", "pass", partition="calibration"),
            item("contradiction", "y", "pass", adjudicated_labels=[{"labeler": "a", "label": "fail"}]),
            item("traversal", None, "pass", answer_file="../../secret.md"),
        ])
        code, out, _ = self.run_cli("--grade-heldout")
        r = json.loads(out)
        self.assertEqual(code, 1)
        joined = "\n".join(r["errors"])
        for needle in ("fixture-author labels are dev data", "not partition 'heldout': wrong-partition",
                       "contradicts unanimous adjudicated label", "answer_file must be a relative POSIX path"):
            self.assertIn(needle, joined)

    def test_dev_fixtures_score_perfectly_by_construction(self):
        code, out, _ = self.run_cli("--grade-labelled", "dev-fixtures")
        a = json.loads(out)["graders"]["regex"]["aggregate"]
        self.assertEqual(code, 0)
        self.assertEqual((a["tp"], a["tn"], a["fp"], a["fn"]), (1, 1, 0, 0))

    def test_judge_and_layered_graders_are_scored_when_a_judge_is_configured(self):
        self.write_manifest([item("ok", "Split first; otherwise statistics leak.", "pass"),
                             item("bad", "Leakage is a myth; split first if you like, it changes nothing.", "fail")])

        def judge(req):
            bad = "myth" in req.user
            return json.dumps({"expected_behavior": [{"index": 0, "met": not bad, "reason": "r"}],
                               "must_not": [{"index": 0, "violated": bad, "reason": "r"}]})
        dummy = self.responses_file([], "dummy.jsonl")
        factory = FakeFactory(judge=judge)
        code, out, err = run_main(self.root, "--grade-heldout", "--judge-provider", "replay", "--judge-replay-file",
                                  str(dummy), backend_factory=factory)
        self.assertEqual(code, 0, err)
        self.assertEqual(len(factory.backends["judge"].calls), 2)  # one judge call per item, shared by both graders
        r = json.loads(out)
        self.assertEqual(r["graders"]["regex"]["aggregate"]["fp"], 1)
        self.assertEqual(r["graders"]["judge"]["aggregate"]["fp"], 0)
        self.assertEqual(r["graders"]["layered"]["aggregate"]["tn"], 1)
        self.assertEqual(r["judge"]["calibration"], "uncalibrated against human labels")

    def test_calibration_report_carries_provenance(self):
        p = self.write_manifest([item("ok-1", "Split first; otherwise statistics leak.", "pass")])
        env = {**FIXED_ENV, "git_revision": "abc123", "git_dirty": True}
        code, out, err = self.run_cli("--grade-heldout", environment=env)
        self.assertEqual(code, 0, err)
        prov = json.loads(out)["provenance"]
        self.assertEqual((prov["report_schema"], prov["git_revision"], prov["git_dirty"]),
                         ("ml-ai-skills/grader-calibration", "abc123", True))
        self.assertEqual(prov["manifest_sha256"], el.sha256_file(p))
        self.assertEqual(prov["config"]["grade_heldout"], True)
        self.assertEqual(prov["config_sha256"], re_._json_sha(prov["config"]))
        for key in ("grader_version", "harness_version", "dataset_sha256", "case_sha256", "skill_sha256"):
            self.assertIn(key, prov)
        self.assertIsNone(prov["judge_prompt_sha256"])

    def test_manifest_errors_mean_no_judge_calls(self):
        self.write_manifest([item("ok", "Split first; leak.", "pass"),
                             item("bad", "y", "pass", adjudicated_labels=[{"labeler": "a", "label": "fail"}])])
        dummy = self.responses_file([], "dummy.jsonl")
        factory = FakeFactory(judge=lambda req: "{}")
        code, out, _ = run_main(self.root, "--grade-heldout", "--judge-provider", "replay", "--judge-replay-file",
                                str(dummy), backend_factory=factory)
        r = json.loads(out)
        self.assertEqual((code, len(factory.backends["judge"].calls)), (1, 0))
        self.assertIn("judge not run: the manifest has errors", "\n".join(r["warnings"]))
        self.assertNotIn("judge", r["graders"])


class JudgeOnlyPreflightTests(Base):
    """--responses / --grade-* with a paid judge: same worst-case refusal and --dry-run as model mode."""

    write_manifest = GradeLabelledCliTests.write_manifest

    def paid_judge(self):
        verdict = json.dumps({"expected_behavior": [{"index": 0, "met": True}], "must_not": [{"index": 0,
                                                                                             "violated": False}]})
        self.judge = eb.CallableBackend(lambda req: verdict, provider="claude-cli", model="judge-m", paid=True,
                                        pricing=eb.Pricing(1000.0, 1000.0))
        return lambda role, args: self.judge

    JARGS = ("--judge-provider", "claude-cli", "--judge-model", "judge-m", "--allow-network")

    def test_labelled_set(self):
        self.write_manifest([item("ok", "Split first; otherwise statistics leak.", "pass"),
                             item("bad", "Yes, scale it all.", "fail")])
        code, out, err = run_main(self.root, "--grade-heldout", *self.JARGS, "--max-cost-usd", "0.001",
                                  backend_factory=self.paid_judge())
        self.assertEqual((code, out, self.judge.calls), (2, "", []))
        self.assertIn("not-run: the worst-case estimate", err)
        code, out, err = run_main(self.root, "--grade-heldout", *self.JARGS, "--max-cost-usd", "0.001", "--dry-run",
                                  backend_factory=self.paid_judge())
        plan = json.loads(out)
        self.assertEqual((code, self.judge.calls, plan["schema"]), (0, [], re_.PLAN_SCHEMA))
        self.assertEqual((plan["preflight"]["judge_requests_max"], plan["preflight"]["answer_requests"]), (2, 0))
        self.assertGreater(plan["preflight"]["worst_case_usd"], 0.001)
        self.assertEqual(plan["provenance"]["config"]["judge_model"], "judge-m")
        code, out, err = run_main(self.root, "--grade-heldout", *self.JARGS, "--max-cost-usd", "100",
                                  backend_factory=self.paid_judge())
        self.assertEqual((code, len(self.judge.calls)), (0, 2), err)
        self.assertIn("judge-only - up to 2 request(s)", err)

    def test_recorded_responses(self):
        rows = self.responses_file([{"case_id": "skill-a/leak", "condition": "with_skill", "response": LEAK_PASS}])
        code, out, err = run_main(self.root, "--responses", str(rows), *self.JARGS, "--max-cost-usd", "0.001",
                                  backend_factory=self.paid_judge())
        self.assertEqual((code, out, self.judge.calls), (2, "", []))
        code, out, _ = run_main(self.root, "--responses", str(rows), *self.JARGS, "--max-cost-usd", "0.001",
                                "--allow-partial-budget", backend_factory=self.paid_judge())
        [g] = json.loads(out)["graded"]
        self.assertEqual((code, self.judge.calls, g["subjective"]["status"]), (0, [], "judge-error"))
        self.assertEqual(g["subjective"]["attempt_status"], "budget-exhausted")


class ReplayJudgeLabelledTests(Base):
    write_manifest = GradeLabelledCliTests.write_manifest

    def test_a_replay_judge_is_keyed_per_answer(self):
        good, bad = "Split first; otherwise statistics leak.", "Leakage is a myth; split first if you like."
        self.write_manifest([item("ok", good, "pass"), item("bad", bad, "fail")])
        verdict = lambda met: json.dumps({"expected_behavior": [{"index": 0, "met": met}],  # noqa: E731
                                          "must_not": [{"index": 0, "violated": not met}]})
        judge_rows = self.responses_file([
            {"case_id": "skill-a/leak", "condition": re_.labelled_condition(t), "response": verdict(t == good)}
            for t in (good, bad)], "judge.jsonl")
        code, out, err = self.run_cli("--grade-heldout", "--judge-provider", "replay", "--judge-replay-file",
                                      str(judge_rows))
        self.assertEqual(code, 0, err)
        a = json.loads(out)["graders"]["judge"]["aggregate"]
        self.assertEqual((a["tp"], a["tn"], a["fp"], a["fn"], a["abstained"]), (1, 1, 0, 0, 0))


if __name__ == "__main__":
    unittest.main()
