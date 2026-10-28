"""Executes agent-evaluation/SKILL.md's python blocks (stdlib-only, no stubs needed).

All six Workflow blocks run verbatim; the regression tests below pin the
defects found in the audit (each failed against the pre-fix skill code):

* detect_loops hashed ``tuple(sorted(args.items()))`` -> TypeError on any
  list/dict-valued argument, which real tool calls routinely have.
* check_ordering counted an *errored* prerequisite call as satisfied (and,
  once fixed, must say the prerequisite was attempted-but-failed rather than
  "called before its prerequisite", which would send triage hunting for a
  missing call that isn't missing).
* check_unsafe_actions treated ``{"confirmed": False}`` as confirmed (key
  presence, not value).
"""
from __future__ import annotations

import contextlib
import io
import unittest

from examples import harness

SLUG = "agent-evaluation"


# harness.run_blocks compiles the blocks without dont_inherit=True, so they
# inherit harness.py's `from __future__ import annotations`; @dataclass then
# resolves string annotations via sys.modules[cls.__module__], which doesn't
# exist for the exec namespace. Registering an empty module of that name is
# enough (dataclasses only uses it to recognise ClassVar/InitVar strings).
_NS_MODULE = f"skill_example_{SLUG.replace('-', '_')}"


def run_skill():
    with contextlib.redirect_stdout(io.StringIO()):
        return harness.run_blocks(SLUG, stubs={_NS_MODULE: harness.make_module(_NS_MODULE)})


class AgentEvaluationExampleTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.ns = run_skill()

    def call(self, name, args, is_error=False, result=None):
        return self.ns["ToolCall"](name=name, args=args, is_error=is_error, result=result)

    def traj(self, *calls):
        return self.ns["Trajectory"](task="t", calls=list(calls))

    def test_all_blocks_execute_and_report_matches_sample_trajectory(self):
        report = self.ns["report"]
        self.assertEqual(len(report["argument_errors"]), 2)  # steps 1 and 2 lack `amount`
        self.assertTrue(all("missing required args ['amount']" in f for f in report["argument_errors"]))
        self.assertEqual(len(report["loop_findings"]), 1)  # strict policy flags the identical retry
        self.assertIn("step 2", report["loop_findings"][0])
        self.assertEqual(report["ordering_findings"], [])
        self.assertEqual([f.split(":")[0] for f in report["unsafe_action_findings"]],
                         ["step 1", "step 2", "step 3"])
        # default (lenient) loop policy tolerates one identical retry, as the block's comment claims
        self.assertEqual(self.ns["detect_loops"](self.ns["trajectory"]), [])

    def test_unknown_tool_is_reported(self):
        findings = self.ns["check_arguments"](self.traj(self.call("drop_table", {})))
        self.assertEqual(len(findings), 1)
        self.assertIn("unknown tool", findings[0])

    def test_empty_trajectory_has_no_findings(self):
        t = self.traj()
        for fn in ("check_arguments", "detect_loops", "check_ordering", "check_unsafe_actions"):
            self.assertEqual(self.ns[fn](t), [], fn)

    # --- regressions -------------------------------------------------------
    def test_loop_detection_handles_unhashable_argument_values(self):
        # Pre-fix: TypeError: unhashable type: 'list'
        args = {"to": ["a@example.com", "b@example.com"], "template": {"id": "refund"}}
        t = self.traj(self.call("send_email", args, is_error=True),
                      self.call("send_email", dict(args), is_error=True))
        findings = self.ns["detect_loops"](t, max_identical_repeats=0)
        self.assertEqual(len(findings), 1)
        self.assertIn("step 1", findings[0])

    def test_loop_detection_distinguishes_changed_arguments(self):
        t = self.traj(self.call("issue_refund", {"order_id": "1"}, is_error=True),
                      self.call("issue_refund", {"order_id": "1", "amount": 5.0}))
        self.assertEqual(self.ns["detect_loops"](t, max_identical_repeats=0), [])

    def test_errored_prerequisite_does_not_satisfy_ordering(self):
        # Pre-fix: [] — a refund that only ever FAILED still "satisfied" send_email's prerequisite.
        t = self.traj(self.call("lookup_order", {"order_id": "1"}),
                      self.call("issue_refund", {"order_id": "1", "amount": 5.0}, is_error=True),
                      self.call("send_email", {"to": "c", "template": "refund_confirmation"}))
        findings = self.ns["check_ordering"](t)
        self.assertEqual(len(findings), 1)
        self.assertIn("'send_email'", findings[0])
        self.assertIn("prerequisite 'issue_refund' was attempted but never succeeded", findings[0])
        self.assertNotIn("called before", findings[0])

    def test_never_called_prerequisite_message(self):
        t = self.traj(self.call("issue_refund", {"order_id": "1", "amount": 5.0}))
        self.assertEqual(self.ns["check_ordering"](t),
                         ["step 0: 'issue_refund' called before its prerequisite 'lookup_order'"])

    def test_prerequisite_that_succeeds_after_failing_is_satisfied(self):
        t = self.traj(self.call("lookup_order", {"order_id": "1"}),
                      self.call("issue_refund", {"order_id": "1"}, is_error=True),
                      self.call("issue_refund", {"order_id": "1", "amount": 5.0}),
                      self.call("send_email", {"to": "c", "template": "refund_confirmation"}))
        self.assertEqual(self.ns["check_ordering"](t), [])

    def test_confirmed_false_is_not_confirmation(self):
        # Pre-fix: [] — only key presence was checked.
        t = self.traj(self.call("issue_refund", {"order_id": "1", "amount": 5.0, "confirmed": False}))
        self.assertEqual(len(self.ns["check_unsafe_actions"](t)), 1)
        ok = self.traj(self.call("issue_refund", {"order_id": "1", "amount": 5.0, "confirmed": True}))
        self.assertEqual(self.ns["check_unsafe_actions"](ok), [])

    def test_unregistered_tool_defaults_to_medium_risk_and_is_flagged(self):
        t = self.traj(self.call("delete_account", {"user": "x"}))
        findings = self.ns["check_unsafe_actions"](t)
        self.assertEqual(len(findings), 1)
        self.assertIn("medium", findings[0])


if __name__ == "__main__":
    unittest.main()
