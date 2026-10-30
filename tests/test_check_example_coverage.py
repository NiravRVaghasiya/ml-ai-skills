"""scripts/check_example_coverage.py: every runnable SKILL.md python block was executed."""
import contextlib
import io
import json
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import testutil as tu
import check_example_coverage as cec
from examples import harness

RUNNABLE_SLUG = "model-deployment"   # 2 runnable python blocks
NO_CODE_SLUG = "attention-mechanisms"  # reference skill, no python blocks


def run(*args):
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = cec.main([str(a) for a in args])
    return code, out.getvalue() + err.getvalue()


class TestParityWithHarness(unittest.TestCase):
    def test_inventory_matches_the_harness_view_of_every_skill(self):
        inv = cec.inventory(tu.REPO_ROOT)
        self.assertEqual(set(inv), {d.name for d in tu.sl.iter_skill_dirs()})
        for slug, cur in inv.items():
            blocks = harness.python_blocks(slug)
            with self.subTest(skill=slug):
                self.assertEqual(cur["total_blocks"], len(blocks))
                self.assertEqual(cur["runnable"], [i for i, b in enumerate(blocks) if not harness.is_illustrative(b)])

    def test_harness_records_are_accepted(self):
        with tempfile.TemporaryDirectory() as tmp:
            cov = Path(tmp) / "cov.jsonl"
            with mock.patch.dict("os.environ", {harness.COVERAGE_ENV: str(cov)}):
                harness._record_coverage(RUNNABLE_SLUG, [0, 1], len(harness.python_blocks(RUNNABLE_SLUG)))
            result = cec.evaluate(cov.read_text(encoding="utf-8").splitlines(), cec.inventory(tu.REPO_ROOT))
        self.assertEqual(result["skills"][RUNNABLE_SLUG]["missing"], [])
        self.assertNotIn(RUNNABLE_SLUG, " ".join(result["errors"]))
        self.assertEqual((result["provenance"]["records"], result["provenance"]["legacy_records"]), (1, 0))
        self.assertEqual(result["provenance"]["python"], [harness.run_provenance()["python"]])

    def test_schema_constants_match_the_harness(self):
        self.assertEqual(cec.COVERAGE_SCHEMA, harness.COVERAGE_SCHEMA)
        self.assertIn(harness.COVERAGE_SCHEMA_VERSION, cec.COVERAGE_SCHEMA_VERSIONS)
        self.assertTrue(set(cec.PROVENANCE_TYPES) <= set(harness.coverage_record(RUNNABLE_SLUG, [0], 2)))


class TestGate(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.mkdtemp(prefix="excov-")
        self.root = Path(self._tmp) / "repo"
        for slug in (RUNNABLE_SLUG, NO_CODE_SLUG):
            shutil.copytree(tu.REPO_ROOT / slug, self.root / slug)
        self.cov = Path(self._tmp) / "cov.jsonl"

    def tearDown(self):
        shutil.rmtree(self._tmp, ignore_errors=True)

    def record(self, blocks, slug=RUNNABLE_SLUG, **override):
        inv = cec.inventory(self.root)[slug]
        rec = {"skill": slug, "blocks": blocks, "total_blocks": inv["total_blocks"], "skill_sha256": inv["sha256"]}
        rec.update(override)
        return json.dumps(rec)

    def gate(self, *lines):
        self.cov.write_text("".join(line + "\n" for line in lines), encoding="utf-8")
        return run(self.cov, "--root", self.root)

    def add_block(self, code):
        text = tu.read_skill(self.root, RUNNABLE_SLUG)
        text = tu.sub_once(text, r"^## Gotchas", f"### 99. Extra step\n\n```python\n{code}\n```\n\n## Gotchas")
        tu.write_skill(self.root, RUNNABLE_SLUG, text)

    def test_all_runnable_blocks_executed_passes(self):
        code, out = self.gate(self.record([0]), self.record([1]))
        self.assertEqual(code, 0, out)
        self.assertIn("2 block(s) across 1 skill(s)", out)

    def test_new_unexecuted_block_fails(self):
        # The audit repro: a broken block appended to a skill whose test runs explicit indices.
        self.add_block("raise RuntimeError('this runnable block is never executed by CI')")
        code, out = self.gate(self.record([0]), self.record([1]))
        self.assertEqual(code, 1, out)
        self.assertIn(f"{RUNNABLE_SLUG}: runnable python block(s) [2] were never executed", out)

    def test_illustrative_block_is_not_required(self):
        self.add_block("# illustrative - not run\nraise RuntimeError('doc only')")
        code, out = self.gate(self.record([0, 1]))
        self.assertEqual(code, 0, out)

    def test_stale_records_count_for_nothing(self):
        cases = {"sha": {"skill_sha256": "0" * 64}, "total_blocks": {"total_blocks": 7}}
        for label, override in cases.items():
            with self.subTest(stale=label):
                code, out = self.gate(self.record([0, 1], **override))
                self.assertEqual(code, 1, out)
                self.assertIn("stale record", out)
                self.assertIn("[0, 1] were never executed", out)

    def test_malformed_unknown_and_unhashed_records_fail(self):
        for line in ("not json", self.record([0, 1], skill="no-such-skill"),
                     json.dumps({"skill": RUNNABLE_SLUG, "blocks": [0, 1], "total_blocks": 2})):
            with self.subTest(line=line[:40]):
                code, out = self.gate(self.record([0, 1]), line)
                self.assertEqual(code, 1, out)

    def test_v2_records_with_provenance_pass_and_bad_provenance_fails(self):
        v2 = {"schema": cec.COVERAGE_SCHEMA, "schema_version": 2, "git_revision": "abc", "git_dirty": True,
              "git_untracked_files": 2, "python": "3.13.2", "platform": "Windows", "recorded_at": "2026-10-29T00:00:00Z", "env": {},
              "test_id": "examples.test_x.T.test_y", "test_module": "examples.test_x"}
        code, out = self.gate(self.record([0, 1], **v2))
        self.assertEqual(code, 0, out)
        self.assertIn("git revision(s) abc (0 without)", out)
        for label, override in {"dirty not bool": {"git_dirty": "yes"}, "python not str": {"python": 3},
                                "untracked count not int": {"git_untracked_files": "2"},
                                "env not dict": {"env": []}, "unknown version": {"schema_version": 3},
                                "unknown schema": {"schema": "other"}}.items():
            with self.subTest(label):
                code, out = self.gate(self.record([0, 1], **{**v2, **override}))
                self.assertEqual(code, 1, out)
                self.assertIn("malformed coverage record", out)

    def test_json_output_reports_provenance(self):
        self.cov.write_text(self.record([0, 1]) + "\n", encoding="utf-8")
        code, out = run(self.cov, "--root", self.root, "--json")
        self.assertEqual(code, 0, out)
        prov = json.loads(out)["provenance"]
        self.assertEqual((prov["records"], prov["legacy_records"], prov["records_without_revision"]), (1, 1, 1))

    def test_empty_or_missing_file_is_a_usage_error(self):
        self.assertEqual(self.gate("")[0], 2)
        self.assertEqual(run(Path(self._tmp) / "absent.jsonl", "--root", self.root)[0], 2)


class TestWiredIntoCi(unittest.TestCase):
    def test_examples_job_records_coverage_and_runs_the_gate(self):
        text = (tu.REPO_ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
        job = text.split("\n  examples:\n", 1)[1].split("\n  evals-offline:\n", 1)[0]
        self.assertIn(f"{cec.COVERAGE_ENV}:", job)
        self.assertIn("python scripts/check_example_coverage.py", job)


if __name__ == "__main__":
    unittest.main()
