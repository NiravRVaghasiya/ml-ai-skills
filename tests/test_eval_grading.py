"""Tests for scripts/eval_grading.py (layered verdicts, Wilson intervals, paired uplift,
labelled-set loading and scoring) and scripts/eval_backends.py (statuses, budget,
pricing, replay, redaction). Also guards the shipped evals/_grading/ manifests:
they must validate, sit in the partition their directory names, and never
duplicate a tuned evals/<slug>/ fixture. Pure stdlib, no network."""
from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path

from test_evals import LEAK_PASS, make_repo
from testutil import REPO_ROOT

import eval_backends as eb  # scripts/ is on sys.path via testutil
import eval_grading as eg
import eval_lib as el


def leak_case() -> el.Case:
    data = {"skill": "s", "category": "misconception", "kind": "response", "input": "q",
            "expected_behavior": ["names leakage", "says split first"], "must_not": ["says it is fine"],
            "must_include": [r"\bleak\w*", r"\bsplit first\b"], "must_not_include": [r"\A\W*yes\b"]}
    return el.Case(path=Path("evals/s/leak.yaml"), data=data, diagnostics=[], sha256="0")


def labels(case, text, expected, must_not, source="human"):
    return {(case.uid, el.sha256_bytes(text.encode("utf-8"))): {
        "expected_behavior": expected, "must_not": must_not, "label_source": source}}


class WilsonTests(unittest.TestCase):
    def test_known_values(self):
        # Reference values of the Wilson score interval at z = 1.96.
        self.assertIsNone(eg.wilson(0, 0))
        self.assertEqual(eg.wilson(5, 10), [0.236593, 0.763407])
        lo, hi = eg.wilson(0, 10)
        self.assertEqual((lo, round(hi, 4)), (0.0, 0.2775))
        lo, hi = eg.wilson(10, 10)
        self.assertEqual((round(lo, 4), hi), (0.7225, 1.0))

    def test_proportion(self):
        self.assertEqual(eg.proportion(0, 0), {"k": 0, "n": 0, "rate": None, "ci95": None})
        self.assertEqual(eg.proportion(1, 4)["rate"], 0.25)


class CombineTests(unittest.TestCase):
    """VERDICT_RULES, one row per rule branch."""

    def verdict(self, text, *, lab=None, judge=None, status="ok"):
        case = leak_case()
        hard = eg.structural(case, text)
        rubric = eg.rubric_layer(case, el.sha256_bytes(text.encode("utf-8")), labels=lab, judge=judge)
        return eg.combine(status, hard, rubric)

    def test_rule_1_non_ok_status_is_the_verdict(self):
        for status in eb.FAILURE_STATUSES + ("refusal",):
            with self.subTest(status=status):
                v = self.verdict(LEAK_PASS, status=status)
                self.assertEqual((v["final"], v["rule"], v["critical_error"]), (status, 1, False))

    def test_rule_2_any_must_not_violation_fails(self):
        v = self.verdict("Yes. Data leakage is real, split first.")
        self.assertEqual((v["final"], v["critical_sources"]), ("fail", ["structural"]))
        case = leak_case()
        text = "Leakage matters, split first. But honestly it is fine either way."
        v = self.verdict(text, lab=labels(case, text, [True, True], [True]))
        self.assertEqual((v["final"], v["rule"], v["critical_sources"]), ("fail", 2, ["rubric (labels:human)"]))

    def test_rule_3_rubric_graded(self):
        case = leak_case()
        good = "Leakage: split first."
        self.assertEqual(self.verdict(good, lab=labels(case, good, [True, True], [False]))["final"], "correct")
        self.assertEqual(self.verdict(good, lab=labels(case, good, [True, False], [False]))["final"], "incorrect")
        para = "Test statistics contaminate training, so partition before fitting."  # regexes miss it
        v = self.verdict(para, lab=labels(case, para, [True, True], [False]))
        self.assertEqual(v["final"], "disputed")

    def test_rule_4_structural_only_is_never_correct(self):
        self.assertEqual(self.verdict("Leakage: split first.")["final"], "structural-pass")
        self.assertEqual(self.verdict("Just normalize it.")["final"], "structural-fail")
        rubric_only = el.Case(path=Path("evals/s/p.yaml"), diagnostics=[], sha256="0",
                              data={"skill": "s", "input": "q", "expected_behavior": ["x"]})
        hard = eg.structural(rubric_only, "anything")
        self.assertEqual(eg.combine("ok", hard, eg.rubric_layer(rubric_only, "h"))["final"], "ungraded")

    def test_labels_take_precedence_over_the_judge_and_bad_shapes_abstain(self):
        case = leak_case()
        text = "Leakage: split first."
        judge = {"status": "graded", "judge_model": "j", "rubric_pass": False,
                 "expected_behavior": [{"criterion": "a", "met": False}, {"criterion": "b", "met": False}],
                 "must_not": [{"criterion": "c", "violated": False}]}
        r = eg.rubric_layer(case, el.sha256_bytes(text.encode("utf-8")),
                            labels=labels(case, text, [True, True], [False]), judge=judge)
        self.assertEqual(r["source"], "labels:human")
        r = eg.rubric_layer(case, el.sha256_bytes(text.encode("utf-8")), labels=labels(case, text, [True], [False]))
        self.assertEqual(r["status"], "abstained")
        r = eg.rubric_layer(case, "h", judge={"status": "unparseable", "judge_model": "j", "error": "x"})
        self.assertEqual(r["status"], "abstained")
        self.assertTrue(all(i["verdict"] == "not-graded" for i in r["items"]))


class JudgeParseTests(unittest.TestCase):
    def test_strict_parse(self):
        case = leak_case()
        ok = json.dumps({"expected_behavior": [{"index": 1, "met": True}, {"index": 0, "met": True}],
                         "must_not": [{"index": 0, "violated": False}]})
        res = eg.parse_judge(case, eb.Completion(status="ok", text="noise " + ok + " noise"), "j")
        self.assertEqual((res["status"], res["rubric_pass"]), ("graded", True))
        bad = json.dumps({"expected_behavior": [{"index": 0, "met": "true"}, {"index": 1, "met": True}],
                          "must_not": [{"index": 0, "violated": False}]})
        self.assertEqual(eg.parse_judge(case, eb.Completion(status="ok", text=bad), "j")["status"], "unparseable")
        err = eg.parse_judge(case, eb.Completion(status="rate-limited", error="429"), "j")
        self.assertEqual((err["status"], err["attempt_status"]), ("judge-error", "rate-limited"))

    def test_judge_prompt_hash_is_stable_and_sensitive(self):
        h = eg.judge_prompt_sha256()
        self.assertEqual(h, eg.judge_prompt_sha256())
        orig = eg.JUDGE_SYSTEM_PROMPT
        try:
            eg.JUDGE_SYSTEM_PROMPT = orig + " "
            self.assertNotEqual(h, eg.judge_prompt_sha256())
        finally:
            eg.JUDGE_SYSTEM_PROMPT = orig


def entry(case_id, cond, passed, *, sample=0, status="ok", final=None, critical=False, rubric=None,
          grader_configured=False):
    final = final or ("structural-pass" if passed else "structural-fail")
    if rubric is None:  # as graded_entry produces: these verdicts only come out of a graded rubric (rule 3)
        rubric = "graded" if final in ("correct", "incorrect", "disputed") else "not-graded"
    return {"case_id": case_id, "condition": cond, "sample": sample, "status": status,
            "hard": {"gradable": True, "passed": passed if status == "ok" else None},
            "rubric": {"status": rubric, "grader_configured": grader_configured},
            "verdict": {"final": final, "critical_error": critical}}


class PairedUpliftTests(unittest.TestCase):
    def test_complete_pairs_only(self):
        graded = [entry("a", "with_skill", True), entry("a", "without_skill", False),
                  entry("b", "with_skill", True),  # no without_skill arm
                  entry("c", "with_skill", True), entry("c", "without_skill", True, status="timeout")]
        u = eg.paired_uplift(graded, "structural")
        self.assertEqual((u["status"], u["paired_cases"], u["delta"]), ("measured", 1, 1.0))
        reasons = {i["case_id"]: i["reasons"] for i in u["incomplete_cases"]}
        self.assertEqual(reasons["b"], ["no without_skill attempt"])
        self.assertEqual(reasons["c"], ["without_skill: 1 of 1 attempt(s) have no structural outcome"])

    def test_one_arm_is_never_an_uplift(self):
        u = eg.paired_uplift([entry("a", "with_skill", True)], "structural")
        self.assertEqual(u["status"], "unmeasured")

    def test_unequal_samples_and_correct_layer(self):
        graded = [entry("a", "with_skill", True), entry("a", "with_skill", True, sample=1),
                  entry("a", "without_skill", True)]
        u = eg.paired_uplift(graded, "structural")
        self.assertIn("unequal sample counts (2 vs 1)", u["incomplete_cases"][0]["reasons"])
        graded = [entry("a", "with_skill", True, final="correct"), entry("a", "without_skill", True, final="incorrect"),
                  entry("b", "with_skill", True), entry("b", "without_skill", True)]
        u = eg.paired_uplift(graded, "correct")
        self.assertEqual((u["paired_cases"], u["delta"], u["cases_with_better"]), (1, 1.0, ["a"]))
        self.assertEqual(u["incomplete_cases"][0]["case_id"], "b")  # structural-only: no correctness outcome
        self.assertEqual(u["rubric_graded_attempts"], 2)

    def test_regex_must_not_fail_alone_is_no_correctness_outcome(self):
        # Both arms hit a must_not_include regex and nothing graded the rubric: the correct layer has no
        # outcome (it used to report a measured 0 vs 0), while critical_error still counts the regex hit.
        graded = [entry("a", c, False, final="fail", critical=True) for c in ("with_skill", "without_skill")]
        self.assertEqual(eg.paired_uplift(graded, "correct")["status"], "unmeasured")
        crit = eg.paired_uplift(graded, "critical_error")
        self.assertEqual((crit["status"], crit["rubric_graded_attempts"]), ("measured", 0))
        self.assertEqual(eg.summarize_final(graded)["with_skill"]["correct"]["n"], 0)
        # With a graded rubric the same 'fail' is a correctness outcome (not correct).
        graded = [entry("a", c, False, final="fail", critical=True, rubric="graded")
                  for c in ("with_skill", "without_skill")]
        self.assertEqual(eg.paired_uplift(graded, "correct")["with_skill_rate"]["n"], 1)

    def test_refusal_is_not_correct_only_when_a_rubric_grader_was_configured(self):
        refusal = entry("a", "with_skill", None, status="refusal", final="refusal")
        self.assertIsNone(eg.outcome_correct(refusal))
        refusal["rubric"]["grader_configured"] = True
        self.assertIs(eg.outcome_correct(refusal), False)
        # summary.final.correct uses the same outcome, so the two denominators agree
        self.assertEqual(eg.summarize_final([refusal])["with_skill"]["correct"]["n"], 1)


class LabelledSetTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.root = make_repo(self.tmp / "repo")
        self.suite = el.load_suite(self.root)

    def tearDown(self):
        self._tmp.cleanup()

    def manifest(self, rows) -> Path:
        p = self.tmp / "m" / "manifest.jsonl"
        p.parent.mkdir(exist_ok=True)
        p.write_text("".join((r if isinstance(r, str) else json.dumps(r)) + "\n" for r in rows), encoding="utf-8")
        return p

    def row(self, **kw):
        base = {"item_id": "i1", "case_id": "skill-a/leak", "skill": "skill-a", "partition": "calibration",
                "intended_label": "pass", "final_label": "pass", "label_source": "human", "answer_text": "t"}
        base.update(kw)
        return {k: v for k, v in base.items() if v is not None}

    def test_valid_items_and_answer_files(self):
        (self.tmp / "m").mkdir()
        (self.tmp / "m" / "answers").mkdir()
        (self.tmp / "m" / "answers" / "a.md").write_bytes(b"from a file\r\n")  # CRLF is normalized
        p = self.manifest([self.row(), self.row(item_id="i2", answer_text=None, answer_file="answers/a.md")])
        items, errors, warnings = eg.load_labelled(p)
        self.assertEqual((errors, warnings), ([], []))
        self.assertEqual(items[1]["answer_text"], "from a file\n")

    def test_every_validation_error(self):
        rows = [
            "{oops",
            self.row(item_id="dup"), self.row(item_id="dup"),
            self.row(item_id="x1", extra=1),
            self.row(item_id="x2", partition="test"),
            self.row(item_id="x3", final_label="maybe"),
            self.row(item_id="x4", label_source="crowd"),
            self.row(item_id="x5", label_source="fixture-author"),
            self.row(item_id="x6", case_id="other/leak"),
            self.row(item_id="x7", answer_file="a.md"),
            self.row(item_id="x8", answer_text=None, answer_file="/etc/passwd"),
            self.row(item_id="x9", label_source="agent-adjudicated"),
            self.row(item_id="x10", adjudicated_labels=[{"labeler": "a", "label": "fail"}]),
        ]
        _, errors, _ = eg.load_labelled(self.manifest(rows))
        joined = "\n".join(errors)
        for needle in ("not valid JSON", "duplicate item_id 'dup'", "unknown field 'extra'", "partition 'test'",
                       "final_label must be one of", "label_source must be one of", "fixture-author labels are dev",
                       "must be '<skill>/<case id>'", "exactly one of answer_text or answer_file",
                       "answer_file must be a relative POSIX path", "agent-adjudicated items need",
                       "contradicts unanimous adjudicated label"):
            self.assertIn(needle, joined)

    def test_symlink_out_of_the_manifest_directory_is_rejected(self):
        (self.tmp / "m").mkdir()
        outside = self.tmp / "outside.md"
        outside.write_text("secret", encoding="utf-8")
        try:
            os.symlink(outside, self.tmp / "m" / "link.md")
        except (OSError, NotImplementedError) as e:
            self.skipTest(f"cannot create a symlink here ({type(e).__name__}); Windows needs Developer Mode or admin")
        _, errors, _ = eg.load_labelled(self.manifest([self.row(answer_text=None, answer_file="link.md")]))
        self.assertIn("answer_file must be a relative POSIX path", errors[0])

    def test_split_labels_resolved_without_notes_warn_outside_strict_partitions(self):
        # dev items, and a human final label over split model labels, only warn
        adj = [{"labeler": "a", "label": "pass"}, {"labeler": "b", "label": "fail"}]
        for kw in ({"partition": "dev", "label_source": "agent-adjudicated"}, {"label_source": "human"}):
            with self.subTest(**kw):
                _, errors, warnings = eg.load_labelled(self.manifest([self.row(adjudicated_labels=adj, **kw)]))
                self.assertEqual(errors, [])
                self.assertIn("labelers disagree", warnings[0])

    def test_agent_adjudicated_measurement_items_need_two_labelers_and_a_majority(self):
        def adj(*labels, names=None):
            return [{"labeler": n, "label": lab} for n, lab in zip(names or "abcdef", labels)]
        bad = {
            "single": dict(adjudicated_labels=adj("pass")),
            "same-labeler": dict(adjudicated_labels=adj("pass", "pass", names=["x", "X "])),
            "tie-resolved": dict(adjudicated_labels=adj("pass", "fail")),
            "minority": dict(final_label="fail", adjudicated_labels=adj("pass", "pass", "fail")),
            "abstention-tie": dict(adjudicated_labels=adj("pass", "unresolved")),
        }
        good = {
            "unanimous": dict(adjudicated_labels=adj("pass", "pass")),
            "tie-unresolved": dict(final_label="unresolved", adjudicated_labels=adj("pass", "fail")),
            "majority": dict(adjudicated_labels=adj("pass", "pass", "fail")),
        }
        for part in ("calibration", "heldout"):
            for name, kw in {**bad, **good}.items():
                with self.subTest(partition=part, item=name):
                    _, errors, _ = eg.load_labelled(self.manifest([self.row(
                        partition=part, label_source="agent-adjudicated", **kw)]))
                    self.assertEqual(bool(errors), name in bad, errors)
        _, errors, _ = eg.load_labelled(self.manifest([self.row(label_source="agent-adjudicated", **bad["single"])]))
        self.assertIn("at least two adjudicated_labels from distinct labelers", errors[0])
        _, errors, _ = eg.load_labelled(self.manifest([self.row(label_source="agent-adjudicated",
                                                                **bad["tie-resolved"])]))
        self.assertIn("final_label must be the strict majority ('unresolved')", errors[0])

    def test_scoring_and_exclusions(self):
        items = [
            self.row(item_id="tp", answer_text="Split first: leak otherwise."),
            self.row(item_id="fn", answer_text="Partition before fitting to avoid contamination."),
            self.row(item_id="tn", final_label="fail", intended_label="fail", answer_text="Yes, normalize it all."),
            self.row(item_id="fp", final_label="fail", intended_label="fail",
                     answer_text="It won't leak much; split first only if you care."),
            self.row(item_id="un", final_label="unresolved"),
            self.row(item_id="dev", answer_text=LEAK_PASS),
            self.row(item_id="rubric-only", case_id="skill-a/plain"),
        ]
        res = eg.score_labelled(items, self.suite, {"regex": eg.regex_grader}, exclude_texts=eg.dev_texts(self.suite))
        self.assertEqual(res["counts"]["excluded"], {"unresolved": 1, "unknown_case": 0, "dev_overlap": 1})
        r = res["graders"]["regex"]
        self.assertEqual(r["not_gradable"], 1)
        a = r["aggregate"]
        self.assertEqual((a["tp"], a["fn"], a["tn"], a["fp"]), (1, 1, 1, 1))
        self.assertEqual((a["tpr"]["rate"], a["fnr"]["rate"], a["tnr"]["rate"], a["fpr"]["rate"]),
                         (0.5, 0.5, 0.5, 0.5))
        self.assertEqual(r["by_label_source"]["human"]["tp"], 1)
        rows = {x["item_id"]: x for x in res["items"]}
        self.assertEqual(rows["fn"]["graders"]["regex"]["failed_assertions"], ["must_include[0]", "must_include[1]"])
        self.assertIn("dev fixture", rows["dev"]["excluded"])

    def test_abstentions_are_counted_not_scored(self):
        items = [self.row(item_id="a", answer_text="x")]
        res = eg.score_labelled(items, self.suite, {"g": lambda case, text: ("abstain", {})})
        a = res["graders"]["g"]["aggregate"]
        self.assertEqual((a["abstained"], a["tp"] + a["fn"], a["tpr"]["rate"]), (1, 0, None))

    def test_calibration_status_flags_missing_human_labels(self):
        items = [self.row(label_source="agent-adjudicated", adjudicated_labels=[{"labeler": "a", "label": "pass"}])]
        st = eg.calibration_status(items, eg.score_labelled(items, self.suite, {"regex": eg.regex_grader}))
        self.assertEqual((st["status"], st["human_labelled_items"]), ("measured", 0))
        self.assertIn("not measured", st["against_humans"])


class ShippedGradingSetTests(unittest.TestCase):
    """The repository's evals/_grading/ manifests (empty until a labelled set is added)."""

    GRADING = REPO_ROOT / "evals" / "_grading"

    def test_layout(self):
        self.assertTrue((self.GRADING / "README.md").is_file())
        for part in eg.PARTITIONS:
            self.assertTrue((self.GRADING / part / "manifest.jsonl").is_file(), part)

    def test_manifests_validate_and_stay_in_their_partition(self):
        suite = el.load_suite(REPO_ROOT)
        dev = eg.dev_texts(suite)
        uids = {c.uid for c in suite.cases}
        for part in eg.PARTITIONS:
            path = self.GRADING / part / "manifest.jsonl"
            items, errors, _ = eg.load_labelled(path)
            with self.subTest(partition=part):
                self.assertEqual(errors, [])
                self.assertEqual([i["item_id"] for i in items if i["partition"] != part], [])
                self.assertEqual([i["item_id"] for i in items if i["case_id"] not in uids], [])
                if part != "dev":
                    # Tuned fixtures are dev data; a copy in calibration/heldout would measure nothing held out.
                    self.assertEqual([i["item_id"] for i in items if el.normalize_response(i["answer_text"]) in dev],
                                     [])
                    self.assertEqual([i["item_id"] for i in items if i["label_source"] == "fixture-author"], [])

    def test_grading_dir_is_not_mistaken_for_a_skill(self):
        self.assertEqual(list(self.GRADING.glob("*.yaml")) + list(self.GRADING.glob("plan.json")), [])


class BackendTests(unittest.TestCase):
    def test_budget(self):
        b = eb.Budget(1.0)
        self.assertTrue(b.can_afford(0.6))
        b.charge(0.6)
        self.assertFalse(b.can_afford(0.6))
        self.assertTrue(b.exhausted)
        self.assertFalse(b.can_afford(0.0))  # once exhausted, stays exhausted (no cherry-picking cheap calls)
        u = eb.Budget(1.0)
        u.charge(None)
        self.assertEqual(u.to_dict()["uncosted_calls"], 1)
        self.assertTrue(u.can_afford(None))  # a free backend's uncosted call does not stop the run
        self.assertTrue(eb.Budget(None).can_afford(None))

    def test_a_paid_call_of_unknown_cost_stops_the_budget(self):
        b = eb.Budget(1.0)
        b.charge(None, paid=True)
        self.assertFalse(b.can_afford(None))
        d = b.to_dict()
        self.assertEqual((d["exhausted"], d["uncosted_calls"], d["spent_usd_estimate"]), (True, 1, 0.0))
        self.assertIn("spend can no longer be bounded", d["stop_reason"])
        self.assertIn("this invocation only", d["scope"])

    def test_pricing(self):
        p = eb.Pricing(3.0, 15.0)
        self.assertAlmostEqual(p.actual({"input_tokens": 1000, "output_tokens": 100}), 0.0045)
        self.assertIsNone(p.actual(None))
        req = eb.Request(system="s" * 1000, user="u", max_tokens=100)
        self.assertGreater(p.worst_case(req), p.actual({"input_tokens": 250, "output_tokens": 100}))

    def test_redact_plain_and_json_escaped(self):
        secret = 'abc"def\\ghi-123'
        env = {"MY_API_KEY": secret, "SHORT_TOKEN": "x", "PATH": "/usr/bin:/bin"}
        secrets = eb.secret_values(env)
        self.assertEqual(secrets, [secret])
        text = json.dumps({"v": secret}) + " " + secret
        out = eb.redact(text, secrets)
        self.assertNotIn(secret, out)
        self.assertNotIn(json.dumps(secret)[1:-1], out)
        self.assertEqual(out.count(eb.REDACTED), 2)

    def test_messages_client_repr_hides_the_key(self):
        c = eb.MessagesClient("sk-secret-123456", transport=lambda *a: (200, {}, b"{}"))
        self.assertNotIn("sk-secret", repr(c))

    def test_api_error_kinds(self):
        self.assertEqual(eb.ApiError("x", 429).kind, "rate-limited")
        self.assertEqual(eb.ApiError("x", 529).kind, "server-error")
        self.assertEqual(eb.ApiError("connection error: TimeoutError: timed out", None).kind, "timeout")
        self.assertEqual(eb.ApiError("x", 401, "error").kind, "error")

    def test_anthropic_backend_maps_refusal_and_errors(self):
        def transport(url, body, headers, timeout):
            msg = {"content": [{"type": "text", "text": "I can't help"}], "stop_reason": "refusal", "model": "m-1",
                   "usage": {"input_tokens": 10, "output_tokens": 2}}
            return 200, {}, json.dumps(msg).encode("utf-8")
        client = eb.MessagesClient("k" * 12, transport=transport, sleep=lambda s: None)
        c = eb.AnthropicBackend(client, "m", eb.Pricing(1, 1)).complete(eb.Request("s", "u", 10))
        self.assertEqual((c.status, c.text, c.attempts), ("refusal", "I can't help", 1))
        self.assertAlmostEqual(c.cost_usd, 12 / 1e6)

        def limited(url, body, headers, timeout):
            return 429, {"retry-after": "0"}, b'{"error": {"type": "rate_limit_error", "message": "slow"}}'
        client = eb.MessagesClient("k" * 12, transport=limited, sleep=lambda s: None, max_retries=2)
        c = eb.AnthropicBackend(client, "m").complete(eb.Request("s", "u", 10))
        self.assertEqual((c.status, c.attempts, c.text), ("rate-limited", 3, ""))

    def test_replay(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "r.jsonl"
            p.write_text("\n".join([
                json.dumps({"case_id": "a/b", "condition": "with_skill", "response": "hi"}),
                json.dumps({"case_id": "a/b", "condition": "with_skill", "response": "dup"}),
                json.dumps({"case_id": "a/b", "condition": "without_skill", "status": "timeout"}),
                json.dumps({"case_id": "a/b", "condition": "x", "status": "weird", "response": ""}),
                json.dumps({"case_id": "a/b", "condition": "y", "sample": -1, "response": ""}),
                json.dumps({"case_id": "a/b", "condition": "z"}),
            ]) + "\n", encoding="utf-8")
            rows, errors = eb.load_replay(p)
        self.assertEqual(len(rows), 2)
        self.assertEqual(len(errors), 4)
        rb = eb.ReplayBackend(rows)
        self.assertEqual(rb.complete(eb.Request("s", "u", 1, case_id="a/b", condition="with_skill")).text, "hi")
        self.assertEqual(rb.complete(eb.Request("s", "u", 1, case_id="a/b", condition="without_skill")).status,
                         "timeout")
        self.assertEqual(rb.complete(eb.Request("s", "u", 1, case_id="a/b", condition="with_skill", sample=1)).status,
                         "error")

    def test_completion_round_trip(self):
        c = eb.Completion(status="ok", text="t", usage={"input_tokens": 1}, meta={"x": 1})
        self.assertEqual(eb.Completion.from_dict({**c.to_dict(), "unknown": 1}), c)


if __name__ == "__main__":
    unittest.main()
