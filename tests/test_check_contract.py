"""scripts/check_contract.py: the public-contract lock and its versioning rules."""
import contextlib
import copy
import io
import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

import testutil as tu
import check_contract as cc


def run(*args):
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        code = cc.main(list(args))
    return code, out.getvalue()


class TestRepositoryLock(unittest.TestCase):
    def test_committed_lock_matches_repository(self):
        code, out = run("--check")
        self.assertEqual(code, 0, out)

    def test_lock_lists_every_skill_and_plugin(self):
        lock = json.loads(cc.LOCK_PATH.read_text(encoding="utf-8"))
        slugs = {d.name for d in tu.sl.iter_skill_dirs()}
        self.assertEqual(set(lock["skills"]), slugs)
        self.assertEqual(set(lock["plugins"][cc.gp.ALL_PLUGIN_NAME]), slugs)
        self.assertEqual(lock["version"], (tu.REPO_ROOT / "VERSION").read_text(encoding="utf-8").strip())


class TestDiffClassification(unittest.TestCase):
    def setUp(self):
        self.old = cc.compute_contract()

    def diff(self, mutate):
        new = copy.deepcopy(self.old)
        mutate(new)
        return cc.diff_contract(self.old, new)

    def test_additions_are_not_breaking(self):
        def add(c):
            c["skills"].append("zz-new-skill")
            c["plugins"]["zz-plugin"] = []
            c["enums"]["domain"].append("robotics")
            c["frontmatter"]["optional"].append("license")
        breaking, other = self.diff(add)
        self.assertEqual(breaking, [])
        self.assertEqual(len(other), 4)

    def test_removals_and_new_required_keys_are_breaking(self):
        cases = {
            "skill removed": lambda c: c["skills"].pop(0),
            "plugin removed": lambda c: c["plugins"].pop(sorted(c["plugins"])[0]),
            "removed from plugin": lambda c: c["plugins"][cc.gp.ALL_PLUGIN_NAME].pop(0),
            "enum value removed": lambda c: c["enums"]["risk_level"].remove("critical"),
            "optional frontmatter key removed": lambda c: c["frontmatter"]["optional"].clear(),
            "new required frontmatter key": lambda c: c["frontmatter"]["required"].append("license"),
            "demoted to optional": lambda c: (c["frontmatter"]["required"].remove("inputs"),
                                              c["frontmatter"]["optional"].append("inputs")),
            "marketplace renamed": lambda c: c.__setitem__("marketplace", "other"),
        }
        for label, mutate in cases.items():
            with self.subTest(change=label):
                breaking, _ = self.diff(mutate)
                self.assertTrue(any(label in b for b in breaking), breaking)

    def test_optional_promoted_to_required_is_breaking_once(self):
        def promote(c):
            c["frontmatter"]["optional"].remove("version_constraints")
            c["frontmatter"]["required"].append("version_constraints")
        breaking, _ = self.diff(promote)
        self.assertEqual(len(breaking), 1, breaking)
        self.assertIn("new required frontmatter key: version_constraints", breaking[0])


class TestLockWorkflow(unittest.TestCase):
    """End-to-end --check/--write on a temp copy of the repo."""

    def setUp(self):
        self._tmp = tempfile.mkdtemp(prefix="contract-")
        self.root = tu.copy_skill_tree(Path(self._tmp) / "repo")
        shutil.copy(tu.REPO_ROOT / "VERSION", self.root / "VERSION")
        self.set_version("1.0.0", changelog=True)
        self.lock = self.root / "schema" / "public-contract.json"
        code, out = run("--write", "--root", str(self.root))
        self.assertEqual(code, 0, out)

    def tearDown(self):
        shutil.rmtree(self._tmp, ignore_errors=True)

    def set_version(self, version, changelog):
        (self.root / "VERSION").write_text(version + "\n", encoding="utf-8")
        headings = "".join(f"## [{v}]\n\nnotes\n\n" for v in ["1.0.0"] + ([version] if changelog else []))
        (self.root / "CHANGELOG.md").write_text("# Changelog\n\n" + headings, encoding="utf-8")

    def check(self):
        return run("--check", "--root", str(self.root))

    def write(self):
        return run("--write", "--root", str(self.root))

    def test_fresh_lock_passes(self):
        self.assertEqual(self.check()[0], 0)
        lock = json.loads(self.lock.read_text(encoding="utf-8"))
        self.assertEqual(lock["version"], "1.0.0")

    def test_breaking_change_needs_major_bump_and_changelog(self):
        shutil.rmtree(self.root / "attention-mechanisms")
        code, out = self.check()
        self.assertEqual(code, 1)
        self.assertIn("BREAKING: skill removed: attention-mechanisms", out)
        self.assertIn("bump VERSION's major", out)

        code, out = self.write()  # same major -> refused, lock untouched
        self.assertEqual(code, 1)
        self.assertIn("refusing to lock BREAKING", out)
        self.assertIn("attention-mechanisms", json.loads(self.lock.read_text(encoding="utf-8"))["skills"])

        self.set_version("1.1.0", changelog=True)  # minor bump is not enough
        self.assertEqual(self.write()[0], 1)

        self.set_version("2.0.0", changelog=False)  # major bump but no CHANGELOG entry
        code, out = self.write()
        self.assertEqual(code, 1)
        self.assertIn("## [2.0.0]", out)

        self.set_version("2.0.0", changelog=True)
        self.assertEqual(self.write()[0], 0)
        self.assertEqual(self.check()[0], 0)

    def test_additive_change_relocks_without_bump(self):
        shutil.copytree(self.root / "data-preprocessing", self.root / "data-preprocessing-two")
        code, out = self.check()
        self.assertEqual(code, 1)
        self.assertIn("skill added: data-preprocessing-two", out)
        self.assertNotIn("BREAKING", out)
        self.assertEqual(self.write()[0], 0)
        self.assertEqual(self.check()[0], 0)

    def test_version_change_requires_relock_and_changelog(self):
        self.set_version("1.0.1", changelog=False)
        code, out = self.check()
        self.assertEqual(code, 1)
        self.assertIn("VERSION 1.0.0 -> 1.0.1", out)
        self.assertIn("## [1.0.1]", out)

    def test_version_may_not_go_backwards(self):
        self.set_version("2.0.0", changelog=True)
        self.assertEqual(self.write()[0], 0)
        self.set_version("1.5.0", changelog=True)
        code, out = self.write()
        self.assertEqual(code, 1)
        self.assertIn("lower than the locked", out)

    def test_missing_lock_fails_check(self):
        self.lock.unlink()
        code, out = self.check()
        self.assertEqual(code, 1)
        self.assertIn("--write", out)


@unittest.skipUnless(shutil.which("git"), "git is not installed")
class TestBaseRef(TestLockWorkflow):
    """--check --base-ref: the VERSION rules also hold against the base commit's lock, so a
    hand-edited same-commit lock cannot wave a breaking change through at the same major."""

    def setUp(self):
        super().setUp()
        self.git("init", "-q")
        self.git("add", "-A")
        self.git("commit", "-q", "-m", "base")

    def git(self, *args):
        return subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@example.invalid",
                               "-c", "commit.gpgsign=false", "-C", str(self.root), *args],
                              check=True, capture_output=True)

    def check_base(self, ref="HEAD"):
        return run("--check", "--root", str(self.root), "--base-ref", ref)

    def hand_edit_lock_removing(self, slug):
        lock = json.loads(self.lock.read_text(encoding="utf-8"))
        lock["skills"].remove(slug)
        for members in lock["plugins"].values():
            if slug in members:
                members.remove(slug)
        self.write_lock(lock)

    def write_lock(self, lock):
        self.lock.write_text(json.dumps(lock, indent=2) + "\n", encoding="utf-8", newline="\n")

    def test_hand_edited_lock_cannot_hide_a_breaking_change(self):
        shutil.rmtree(self.root / "attention-mechanisms")
        self.hand_edit_lock_removing("attention-mechanisms")
        self.assertEqual(self.check()[0], 0)  # the same-commit comparison alone is fooled
        code, out = self.check_base()
        self.assertEqual(code, 1, out)
        self.assertIn("BREAKING: skill removed: attention-mechanisms", out)
        self.assertIn("the lock at HEAD 1.0.0", out)
        self.assertIn("set VERSION to 2.0.0", out)
        # The failing output must not also claim the base rules hold.
        self.assertNotIn("rules hold", out)
        self.assertIn("VERSION rules violated vs the lock at HEAD (version 1.0.0)", out)

    def test_major_bump_satisfies_the_base_rule(self):
        shutil.rmtree(self.root / "attention-mechanisms")
        self.set_version("2.0.0", changelog=True)
        self.assertEqual(self.write()[0], 0)
        code, out = self.check_base()
        self.assertEqual(code, 0, out)
        self.assertIn("VERSION rules hold vs the lock at HEAD (version 1.0.0)", out)

    def test_additive_change_passes_and_version_may_not_go_below_base(self):
        shutil.copytree(self.root / "data-preprocessing", self.root / "data-preprocessing-two")
        self.assertEqual(self.write()[0], 0)
        self.assertEqual(self.check_base()[0], 0)
        self.set_version("0.9.0", changelog=True)
        lock = json.loads(self.lock.read_text(encoding="utf-8"))
        lock["version"] = "0.9.0"  # hand-edited so the same-commit check passes
        self.write_lock(lock)
        self.assertEqual(self.check()[0], 0)
        code, out = self.check_base()
        self.assertEqual(code, 1, out)
        self.assertIn("lower than the lock at HEAD 1.0.0", out)

    def test_base_without_a_lock_is_skipped_and_said_so(self):
        self.git("rm", "-q", "--cached", "schema/public-contract.json")
        self.git("commit", "-q", "-m", "no lock")
        code, out = self.check_base()
        self.assertEqual(code, 0, out)
        self.assertIn("base-lock comparison skipped", out)

    def test_unresolvable_base_ref_is_an_error(self):
        code, out = self.check_base("no-such-ref")
        self.assertEqual(code, 1, out)
        self.assertIn("does not resolve", out)

    def test_base_ref_requires_check(self):
        with self.assertRaises(SystemExit) as ctx, contextlib.redirect_stderr(io.StringIO()):
            cc.main(["--write", "--root", str(self.root), "--base-ref", "HEAD"])
        self.assertEqual(ctx.exception.code, 2)


class TestWiredIntoWorkflows(unittest.TestCase):
    def test_ci_and_release_compare_against_the_base_lock(self):
        # ci.yml passes the base commit its "Resolve the base commit" step resolved (a push of
        # several commits is compared against the push's `before`, not just the last parent);
        # release.yml runs on a tag push and compares against the tagged commit's parent.
        expected = {"ci.yml": 'scripts/check_contract.py --check --base-ref "$BASE_SHA"',
                    "release.yml": "scripts/check_contract.py --check --base-ref HEAD^1"}
        for wf, call in expected.items():
            text = (tu.REPO_ROOT / ".github" / "workflows" / wf).read_text(encoding="utf-8")
            with self.subTest(workflow=wf):
                self.assertIn(call, text)
                self.assertNotRegex(text, r"(?m)check_contract\.py --check\s*$")


if __name__ == "__main__":
    unittest.main()
