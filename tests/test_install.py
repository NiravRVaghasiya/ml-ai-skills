"""Installer tests: drive install.sh/uninstall.sh via subprocess against a
temp directory and assert on the filesystem. Requires `bash` on PATH (present
on Linux/macOS by default; Windows via Git Bash/WSL) — skipped otherwise."""
from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from testutil import REPO_ROOT
sys.path.insert(0, str(REPO_ROOT / "scripts"))
import skills_lib as sl  # noqa: E402

BASH = shutil.which("bash")
INSTALL_SH = REPO_ROOT / "install.sh"
UNINSTALL_SH = REPO_ROOT / "uninstall.sh"
MANIFEST_NAME = ".ml-ai-skills-manifest"

ALL_SLUGS = {d.name for d in sl.iter_skill_dirs()}


@unittest.skipUnless(BASH, "bash not found on PATH")
class InstallerTestBase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.target = self.tmp / "skills"

    def tearDown(self):
        self._tmp.cleanup()

    def run_install(self, *args: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            [BASH, str(INSTALL_SH), "--target", str(self.target), *args],
            capture_output=True, text=True,
        )

    def run_uninstall(self, *args: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            [BASH, str(UNINSTALL_SH), "--target", str(self.target), *args],
            capture_output=True, text=True,
        )

    def manifest_slugs(self) -> set[str]:
        text = (self.target / MANIFEST_NAME).read_text(encoding="utf-8")
        return {ln.strip() for ln in text.splitlines() if ln.strip() and not ln.startswith("#")}


class TestHelp(InstallerTestBase):
    def test_install_help_exits_zero_and_does_not_touch_target(self):
        result = subprocess.run([BASH, str(INSTALL_SH), "--help"], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0)
        self.assertIn("Usage", result.stdout)
        self.assertFalse(self.target.exists())

    def test_uninstall_help_exits_zero(self):
        result = subprocess.run([BASH, str(UNINSTALL_SH), "--help"], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0)
        self.assertIn("Usage", result.stdout)


class TestDryRun(InstallerTestBase):
    def test_dry_run_makes_no_changes(self):
        result = self.run_install("--dry-run")
        self.assertEqual(result.returncode, 0)
        self.assertFalse(self.target.exists(), "dry-run must not create the target directory")


class TestCleanInstall(InstallerTestBase):
    def test_installs_every_source_skill(self):
        result = self.run_install()
        self.assertEqual(result.returncode, 0, result.stderr)
        installed = {d.name for d in self.target.iterdir() if d.is_dir()}
        self.assertEqual(installed, ALL_SLUGS)

    def test_manifest_lists_every_installed_skill(self):
        self.run_install()
        self.assertEqual(self.manifest_slugs(), ALL_SLUGS)

    def test_skill_md_discoverable_after_install(self):
        """Proxy for 'Claude Code would discover this': every installed
        SKILL.md still parses, and its frontmatter name matches the folder."""
        self.run_install()
        for slug in ALL_SLUGS:
            skill = sl.load_skill(self.target / slug)
            self.assertEqual(skill.frontmatter.get("name"), slug)
            self.assertTrue(skill.frontmatter.get("description"))


class TestReinstallAndUpdate(InstallerTestBase):
    def test_reinstall_updates_instead_of_skipping(self):
        first = self.run_install()
        self.assertEqual(first.returncode, 0)
        second = self.run_install()
        self.assertEqual(second.returncode, 0, second.stderr)
        self.assertNotIn("  skip    ", second.stdout)
        self.assertIn(f"updated={len(ALL_SLUGS)} skipped=0", second.stdout)
        self.assertEqual(self.manifest_slugs(), ALL_SLUGS)


class TestUnmanagedCollisionSafety(InstallerTestBase):
    def test_foreign_directory_is_never_touched(self):
        self.run_install()
        foreign = self.target / "my-own-skill"
        foreign.mkdir()
        (foreign / "SKILL.md").write_text("not ours", encoding="utf-8")

        self.run_install()  # reinstall/update pass
        self.assertEqual((foreign / "SKILL.md").read_text(encoding="utf-8"), "not ours")

        self.run_uninstall()  # full uninstall pass
        self.assertTrue(foreign.exists(), "uninstall must not remove a directory it didn't install")
        self.assertEqual((foreign / "SKILL.md").read_text(encoding="utf-8"), "not ours")

    def test_name_collision_with_unmanaged_dir_is_skipped_without_force(self):
        self.target.mkdir(parents=True)
        collide = self.target / "data-preprocessing"
        collide.mkdir()
        (collide / "SKILL.md").write_text("mine, not ml-ai-skills'", encoding="utf-8")

        result = self.run_install()
        self.assertEqual(result.returncode, 0)
        self.assertIn("skip", result.stdout)
        self.assertEqual((collide / "SKILL.md").read_text(encoding="utf-8"), "mine, not ml-ai-skills'")

    def test_force_overwrites_the_collision(self):
        self.target.mkdir(parents=True)
        collide = self.target / "data-preprocessing"
        collide.mkdir()
        (collide / "SKILL.md").write_text("mine, not ml-ai-skills'", encoding="utf-8")

        result = self.run_install("--force")
        self.assertEqual(result.returncode, 0)
        content = (collide / "SKILL.md").read_text(encoding="utf-8")
        self.assertNotEqual(content, "mine, not ml-ai-skills'")


class TestInvalidEnvironment(InstallerTestBase):
    def test_non_repo_source_fails_cleanly_and_leaves_target_untouched(self):
        not_a_repo = self.tmp / "not-a-repo"
        not_a_repo.mkdir()
        result = subprocess.run(
            [BASH, str(INSTALL_SH), "--source", str(not_a_repo), "--target", str(self.target)],
            capture_output=True, text=True,
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("does not look like an ml-ai-skills checkout", result.stderr)
        self.assertFalse(self.target.exists())

    def test_missing_manifest_uninstall_fails_cleanly(self):
        result = self.run_uninstall()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("nothing to uninstall", result.stderr)


class TestUninstall(InstallerTestBase):
    def test_full_uninstall_removes_all_managed_skills_and_manifest(self):
        self.run_install()
        result = self.run_uninstall()
        self.assertEqual(result.returncode, 0, result.stderr)
        remaining = {d.name for d in self.target.iterdir() if d.is_dir()}
        self.assertEqual(remaining, set())
        self.assertFalse((self.target / MANIFEST_NAME).exists())
        self.assertTrue(self.target.exists(), "uninstall must leave the parent skills/ dir in place")

    def test_uninstall_single_skill_leaves_the_rest(self):
        self.run_install()
        result = self.run_uninstall("--skill", "data-preprocessing")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse((self.target / "data-preprocessing").exists())
        self.assertTrue((self.target / "attention-mechanisms").exists())
        self.assertEqual(self.manifest_slugs(), ALL_SLUGS - {"data-preprocessing"})

    def test_uninstall_dry_run_makes_no_changes(self):
        self.run_install()
        before = self.manifest_slugs()
        result = self.run_uninstall("--dry-run")
        self.assertEqual(result.returncode, 0)
        self.assertEqual(self.manifest_slugs(), before)
        remaining = {d.name for d in self.target.iterdir() if d.is_dir()}
        self.assertEqual(remaining, ALL_SLUGS)


if __name__ == "__main__":
    unittest.main()
