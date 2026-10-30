"""Tests for the routing CI gate in scripts/eval_router.py (`--gate`, run_gate,
resolve_base): which base commit a push / pull_request is compared against, and
that the three checks (integrity, own baseline, no regression vs the base) catch
what they claim to - including a regression hidden in an earlier commit of a
multi-commit push, which a HEAD^1-only comparison misses.

Each test builds a throwaway git repository in a temp dir (copies of the real
skills plus a synthetic benchmark that meets every coverage rule) and calls the
gate in-process with a fake router, so no test depends on the real benchmark's
state or on the real router's quality. Needs git; no network.
"""
import contextlib
import io
import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

import testutil  # noqa: F401  (inserts scripts/ and schema/ onto sys.path as a side effect)
import eval_router as er
import skills_lib as sl
from test_eval_router import FakeHit, synthetic_benchmark

GIT_TIMEOUT = 60
GIT_ENV = {"GIT_AUTHOR_NAME": "gate-test", "GIT_AUTHOR_EMAIL": "gate-test@example.invalid",
           "GIT_COMMITTER_NAME": "gate-test", "GIT_COMMITTER_EMAIL": "gate-test@example.invalid",
           "GIT_CONFIG_NOSYSTEM": "1"}


def git(root: Path, *args: str) -> str:
    r = subprocess.run(["git", "-c", "commit.gpgsign=false", "-c", "init.defaultBranch=main", *args], cwd=root,
                       capture_output=True, text=True, timeout=GIT_TIMEOUT, env={**os.environ, **GIT_ENV})
    if r.returncode != 0:
        raise AssertionError(f"git {' '.join(args)} failed: {r.stderr}")
    return r.stdout.strip()


def make_router(bench: dict, drop_every: int = 0):
    """A fake router: returns each synthetic case's expected (+ prerequisite) skills,
    optionally dropping every `drop_every`-th case's hits (a worse router)."""
    by_query = {c["query"]: c for c in bench["cases"]}
    order = {c["query"]: i for i, c in enumerate(bench["cases"])}

    def route(task, top_k=5):
        c = by_query[task]
        if drop_every and order[task] % drop_every == 0:
            return "workflow", []
        return "workflow", [FakeHit(s) for s in c["acceptable"] + c["expected"]][:max(top_k, 1) + 2]
    return route


@unittest.skipUnless(shutil.which("git"), "git is not installed")
class GateRepoTest(unittest.TestCase):
    TREE = True  # copy the skills and write the synthetic benchmark (run_gate needs them)

    @classmethod
    def setUpClass(cls):
        cls.skills = sl.load_all_skills()
        cls.bench = synthetic_benchmark(cls.skills)
        cls.good = staticmethod(make_router(cls.bench))
        cls.worse = staticmethod(make_router(cls.bench, drop_every=3))

    def setUp(self):
        self._tmp = tempfile.mkdtemp(prefix="router-gate-")
        self.addCleanup(shutil.rmtree, self._tmp, ignore_errors=True)
        self.root = Path(self._tmp) / "repo"
        self.root.mkdir()
        git(self.root, "init", "-q")
        if self.TREE:
            for d in sl.iter_skill_dirs(testutil.REPO_ROOT):
                (self.root / d.name).mkdir()
                shutil.copyfile(d / "SKILL.md", self.root / d.name / "SKILL.md")
            (self.root / "evals" / "routing").mkdir(parents=True)
            self.write_benchmark(self.bench)

    # -- helpers
    @property
    def bench_path(self) -> Path:
        return self.root / "evals" / "routing" / "benchmark.json"

    def write_benchmark(self, bench: dict) -> None:
        self.bench_path.write_text(json.dumps(bench, indent=2) + "\n", encoding="utf-8", newline="\n")

    def write_baseline(self, route_fn) -> None:
        base = er.build_baseline(self.bench_path, route_fn, 5, "2026-10-29", "test", "test")
        (self.root / "evals" / "routing" / "baseline.json").write_text(json.dumps(base, indent=2) + "\n",
                                                                        encoding="utf-8", newline="\n")

    def commit(self, message: str) -> str:
        if self.TREE:
            git(self.root, "add", "-A")
        git(self.root, "commit", "-q", "--allow-empty", "-m", message)
        return git(self.root, "rev-parse", "HEAD")

    def gate(self, route_fn, base_sha=None, event_name="push") -> tuple[int, str, str]:
        summary = Path(self._tmp) / "summary.md"
        summary.unlink(missing_ok=True)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = er.run_gate(self.root, base_sha=base_sha, event_name=event_name, report_dir=Path(self._tmp),
                               summary_path=summary, route_fn=route_fn)
        return code, summary.read_text(encoding="utf-8") if summary.exists() else "", out.getvalue()


class TestRunGate(GateRepoTest):
    def test_unchanged_tree_passes_all_three_checks(self):
        self.write_baseline(self.good)
        self.commit("base")
        self.commit("change")
        code, summary, log = self.gate(self.good)
        self.assertEqual(code, 0, log)
        self.assertIn("- integrity: pass", summary)
        self.assertIn("this commit's router vs this commit's baseline: pass", summary)
        self.assertRegex(summary, r"vs parent \w+'s benchmark \+ baseline: pass")
        self.assertIn("first parent of HEAD", summary)
        report = json.loads((Path(self._tmp) / "router-report-parent.json").read_text(encoding="utf-8"))
        self.assertIn("negative_false_activation_rate", report["splits"]["test"])

    def test_regression_with_lowered_baseline_fails_against_the_parent(self):
        self.write_baseline(self.good)
        self.commit("base")
        self.write_baseline(self.worse)  # lower the bar in the same change as the regression
        self.commit("regress and re-baseline")
        code, summary, log = self.gate(self.worse)
        self.assertEqual(code, 1, log)
        self.assertIn("this commit's router vs this commit's baseline: pass", summary)
        self.assertRegex(log, r"::error::routing regression \[parent \w+\]: recall = [0-9.]+ < required minimum")

    def _regress_with_acceptance(self, tweak=None) -> tuple[int, str, str]:
        """Base commit with a good router; the change regresses and records an
        accepted_regressions entry for the exact measured values (optionally tweaked)."""
        self.write_baseline(self.good)
        self.commit("base")
        base_sha = er.benchmark_sha256(self.bench_path)
        report = er.evaluate(self.bench["cases"], self.worse, top_k=5, split="test")["splits"]["test"]
        floors = er._floors(json.loads((self.root / "evals" / "routing" / "baseline.json").read_text(encoding="utf-8")))
        failing = [(er.THRESHOLD_FLAGS[f][0], f) for f in floors if er.check_thresholds(report, {f: floors[f]})]
        self.assertTrue(failing, "the worse router must fail some base floor")
        self.write_baseline(self.worse)
        path = self.root / "evals" / "routing" / "baseline.json"
        baseline = json.loads(path.read_text(encoding="utf-8"))
        baseline["accepted_regressions"] = [
            {"base_benchmark_sha256": base_sha, "metric": m, "floor": floors[f], "accepted_value": report[m],
             "cases": ["synthetic-case"], "reason": "deliberate trade-off recorded for this test"} for m, f in failing]
        if tweak:
            tweak(baseline["accepted_regressions"])
        path.write_text(json.dumps(baseline, indent=2) + "\n", encoding="utf-8", newline="\n")
        self.commit("regress with an explicit acceptance")
        return self.gate(self.worse)

    def test_explicit_acceptance_of_the_exact_values_passes_with_warnings(self):
        code, summary, log = self._regress_with_acceptance()
        self.assertEqual(code, 0, log)
        self.assertIn("::warning::routing trade-off [parent", log)
        self.assertNotIn("::error::", log)
        report = json.loads((Path(self._tmp) / "router-report-parent.json").read_text(encoding="utf-8"))
        self.assertTrue(report["accepted_regressions"])

    def test_acceptance_does_not_cover_a_further_drop(self):
        def worse_than_accepted(entries):
            for e in entries:
                e["accepted_value"] = round(e["accepted_value"] + 0.05, 4)  # accepts a smaller drop than measured
        code, _summary, log = self._regress_with_acceptance(worse_than_accepted)
        self.assertEqual(code, 1, log)
        self.assertRegex(log, r"::error::routing regression \[parent \w+\]")

    def test_acceptance_for_another_base_benchmark_is_ignored(self):
        def other_base(entries):
            for e in entries:
                e["base_benchmark_sha256"] = "0" * 64
        code, _summary, log = self._regress_with_acceptance(other_base)
        self.assertEqual(code, 1, log)

    def test_malformed_acceptance_fails_integrity(self):
        def no_reason(entries):
            entries[0]["reason"] = ""
        code, summary, log = self._regress_with_acceptance(no_reason)
        self.assertEqual(code, 1, log)
        self.assertIn("reason must explain the trade-off", log)

    def test_multi_commit_push_is_checked_against_event_before(self):
        # A push of two commits: the first regresses and lowers baseline.json, the second
        # is unrelated. HEAD^1 is the already-lowered commit, so only the explicit base
        # (github.event.before) catches the regression.
        self.write_baseline(self.good)
        before = self.commit("base")
        self.write_baseline(self.worse)
        self.commit("regress and re-baseline")
        self.commit("unrelated follow-up")
        code, summary, log = self.gate(self.worse)  # no explicit base: HEAD^1
        self.assertEqual(code, 0, log)
        code, summary, log = self.gate(self.worse, base_sha=before, event_name="push")
        self.assertEqual(code, 1, log)
        self.assertIn(f"explicit base {before[:12]}", summary)
        self.assertRegex(log, r"::error::routing regression \[parent \w+\]")

    def test_new_branch_push_falls_back_to_first_parent(self):
        self.write_baseline(self.good)
        self.commit("base")
        self.commit("change")
        code, summary, log = self.gate(self.good, base_sha=er.ZERO_SHA)
        self.assertEqual(code, 0, log)
        self.assertIn("all-zero SHA of a new branch", summary)

    def test_unknown_explicit_base_fails_loudly(self):
        self.write_baseline(self.good)
        self.commit("base")
        code, summary, log = self.gate(self.good, base_sha="1234567890abcdef1234567890abcdef12345678")
        self.assertEqual(code, 1, log)
        self.assertIn("is not in this clone", log)
        self.assertIn("vs base: FAIL", summary)

    def test_root_commit_reports_not_run(self):
        self.write_baseline(self.good)
        self.commit("only commit")
        code, summary, log = self.gate(self.good)
        self.assertEqual(code, 0, log)
        self.assertIn("NOT RUN", summary)
        self.assertIn("::warning::no base commit with a routing baseline", log)

    def test_family_spanning_splits_fails_own_check(self):
        bench = json.loads(json.dumps(self.bench))
        dev = next(c for c in bench["cases"] if c["split"] == "dev")
        next(c for c in bench["cases"] if c["split"] == "test")["family"] = dev["family"]
        self.write_benchmark(bench)
        self.write_baseline(self.good)
        self.commit("base")
        code, summary, log = self.gate(self.good)
        self.assertEqual(code, 1, log)
        self.assertIn("spans splits", log)
        self.assertIn("benchmark did not validate", summary)

    def test_benchmark_edit_without_rebaseline_fails_integrity(self):
        self.write_baseline(self.good)
        self.commit("base")
        bench = json.loads(json.dumps(self.bench))
        bench["cases"][0]["notes"] = "edited"
        self.write_benchmark(bench)
        self.commit("edit benchmark only")
        code, summary, _log = self.gate(self.good)
        self.assertEqual(code, 1)
        self.assertIn("integrity: FAIL", summary)


class TestResolveBase(GateRepoTest):
    TREE = False  # commit topology only
    def linear(self, n: int) -> list[str]:
        return [self.commit(f"c{i}") for i in range(n)]

    def test_push_without_explicit_base_uses_first_parent(self):
        c = self.linear(3)
        self.assertEqual(er.resolve_base(self.root)[0], c[1])
        self.assertEqual(er.resolve_base(self.root, "", "push")[0], c[1])

    def test_explicit_ancestor_is_used(self):
        c = self.linear(4)
        base, how = er.resolve_base(self.root, c[0], "push")
        self.assertEqual(base, c[0])
        self.assertIn("explicit base", how)
        self.assertEqual(er.resolve_base(self.root, c[0][:10], "push")[0], c[0])  # abbreviated SHA

    def test_explicit_base_equal_to_head(self):
        c = self.linear(2)
        self.assertEqual(er.resolve_base(self.root, c[1], "push"), (c[1], f"explicit base {c[1][:12]} (same as HEAD)"))

    def test_force_push_uses_merge_base(self):
        c = self.linear(2)
        git(self.root, "checkout", "-q", "-b", "old", c[1])
        old_tip = self.commit("rewritten away")
        git(self.root, "checkout", "-q", "main")
        new_tip = self.commit("replacement")
        self.assertNotEqual(old_tip, new_tip)
        base, how = er.resolve_base(self.root, old_tip, "push")
        self.assertEqual(base, c[1])
        self.assertIn("merge base", how)

    def test_root_commit_has_no_base(self):
        self.linear(1)
        self.assertEqual(er.resolve_base(self.root), (None, "no parent commit"))

    def test_unrelated_explicit_base_raises(self):
        self.linear(1)
        git(self.root, "checkout", "-q", "--orphan", "other")
        other = self.commit("unrelated root")
        git(self.root, "checkout", "-q", "main")
        with self.assertRaises(er.GateError):
            er.resolve_base(self.root, other, "push")
        with self.assertRaises(er.GateError):
            er.resolve_base(self.root, "0123456789abcdef0123456789abcdef01234567", "push")

    def pr_merge(self) -> tuple[str, str, str]:
        """main: c0 - c1 - m (m = merge of feature, first parent c1); feature: c0 - f1.
        Returns (c0, c1, f1) with HEAD at the merge commit, like refs/pull/N/merge."""
        c0 = self.commit("c0")
        git(self.root, "checkout", "-q", "-b", "feature")
        f1 = self.commit("feature work")
        git(self.root, "checkout", "-q", "main")
        c1 = self.commit("base moved")
        git(self.root, "merge", "-q", "--no-ff", "-m", "test merge", "feature")
        return c0, c1, f1

    def test_pull_request_merge_commit_uses_its_base_side(self):
        _c0, c1, _f1 = self.pr_merge()
        base, how = er.resolve_base(self.root, None, "pull_request")
        self.assertEqual(base, c1)
        self.assertIn("pull-request merge commit", how)
        base, how = er.resolve_base(self.root, c1, "pull_request")  # base.sha == first parent
        self.assertEqual(base, c1)
        self.assertIn(f"which is the explicit base {c1[:12]}", how)

    def test_pull_request_with_stale_base_sha_uses_the_newer_base_side(self):
        # github.event.pull_request.base.sha older than the merge commit's base side:
        # the first parent contains it, so the newer base tip is the comparison point.
        c0, c1, _f1 = self.pr_merge()
        base, how = er.resolve_base(self.root, c0, "pull_request")
        self.assertEqual(base, c1)
        self.assertIn(f"contains the explicit base {c0[:12]}", how)

    def test_pull_request_whose_first_parent_is_not_the_base_side(self):
        # If the first parent does not contain base.sha, the assumption is wrong and the
        # explicit base wins (here: the feature commit's side).
        _c0, c1, f1 = self.pr_merge()
        base, _how = er.resolve_base(self.root, f1, "pull_request")
        self.assertEqual(base, f1)
        self.assertNotEqual(base, c1)

    def test_head_only_pull_request_checkout_uses_merge_base(self):
        # actions/checkout with ref: head.sha (no merge commit) after the base moved on.
        c0 = self.commit("c0")
        git(self.root, "checkout", "-q", "-b", "feature")
        self.commit("feature work")
        git(self.root, "checkout", "-q", "main")
        base_tip = self.commit("base moved")
        git(self.root, "checkout", "-q", "feature")
        base, how = er.resolve_base(self.root, base_tip, "pull_request")
        self.assertEqual(base, c0)
        self.assertIn("merge base", how)

    def test_head_sha_confirms_the_synthetic_merge(self):
        c0, c1, f1 = self.pr_merge()
        self.assertEqual(er.resolve_base(self.root, c1, "pull_request", head_sha=f1)[0], c1)
        self.assertEqual(er.resolve_base(self.root, c0, "pull_request", head_sha=f1)[0], c1)  # stale base.sha

    def author_merge_head(self) -> tuple[str, str, str]:
        """A head-only checkout whose PR head is the author's own merge commit, and
        whose first parent already contains base.sha (review finding, 2026-10-29):
        main: c0 - c1; feature: c0 - f1 - fm1 (merge of main) - f2 - fm2 (merge of side).
        Returns (c1, f2, fm2) with HEAD at fm2."""
        c0 = self.commit("c0")
        git(self.root, "checkout", "-q", "-b", "side")
        self.commit("side work")
        git(self.root, "checkout", "-q", "-b", "feature", c0)
        self.commit("feature work")
        git(self.root, "checkout", "-q", "main")
        c1 = self.commit("base moved")
        git(self.root, "checkout", "-q", "feature")
        git(self.root, "merge", "-q", "--no-ff", "-m", "merge main into feature", "main")
        f2 = self.commit("more feature work")
        git(self.root, "merge", "-q", "--no-ff", "-m", "merge side into feature", "side")
        return c1, f2, git(self.root, "rev-parse", "HEAD")

    def test_head_only_checkout_of_an_author_merge_commit(self):
        c1, f2, fm2 = self.author_merge_head()
        # Without head_sha the merge commit is taken for GitHub's test merge (documented limit) ...
        self.assertEqual(er.resolve_base(self.root, c1, "pull_request")[0], f2)
        # ... with it, HEAD is recognised as the PR head and the base itself is used.
        base, how = er.resolve_base(self.root, c1, "pull_request", head_sha=fm2)
        self.assertEqual(base, c1)
        self.assertIn("explicit base", how)

    def test_head_sha_must_match_the_checkout(self):
        _c0, c1, _f1 = self.pr_merge()
        with self.assertRaises(er.GateError):  # not in the clone
            er.resolve_base(self.root, c1, "pull_request", head_sha="0123456789abcdef0123456789abcdef01234567")
        with self.assertRaises(er.GateError):  # HEAD is neither the PR head nor a merge of it
            er.resolve_base(self.root, c1, "pull_request", head_sha=c1)

    def test_already_resolved_base_without_event_is_used_as_is(self):
        # ci.yml resolves the base in a shared step; passed with no event name, an
        # ancestor of HEAD is used verbatim even when HEAD is a merge commit.
        c0, _c1, _f1 = self.pr_merge()
        self.assertEqual(er.resolve_base(self.root, c0, None)[0], c0)

    def test_push_of_a_merge_commit_uses_event_before(self):
        c0, c1, _f1 = self.pr_merge()  # pushing the merge to main: before = c1
        self.assertEqual(er.resolve_base(self.root, c1, "push")[0], c1)
        self.assertEqual(er.resolve_base(self.root, None, "push")[0], c1)  # first parent of the merge
        self.assertEqual(er.resolve_base(self.root, c0, "push")[0], c0)


class TestGateCli(unittest.TestCase):
    def test_gate_flag_dispatches_to_run_gate(self):
        seen = {}

        def fake_run_gate(root, **kw):
            seen.update(kw, root=root)
            return 0

        original = er.run_gate
        er.run_gate = fake_run_gate
        try:
            code = er.main(["--gate", "--base-sha", "abc", "--event-name", "pull_request", "--report-dir", "out",
                            "--head-sha", "def"])
        finally:
            er.run_gate = original
        self.assertEqual(code, 0)
        self.assertEqual((seen["base_sha"], seen["event_name"], seen["report_dir"], seen["head_sha"]),
                         ("abc", "pull_request", Path("out"), "def"))
        self.assertEqual(seen["root"], sl.REPO_ROOT)


if __name__ == "__main__":
    unittest.main()
