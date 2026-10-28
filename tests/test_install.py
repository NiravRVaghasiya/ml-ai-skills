"""Installer tests: drive install.sh/uninstall.sh via subprocess against a
temp directory and assert on the filesystem. Requires `bash` on PATH (present
on Linux/macOS by default; Windows via Git Bash/WSL) — skipped otherwise."""
from __future__ import annotations

import os
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
        # Format 2 lines are 'slug<TAB>sha256' (docs/INSTALLER.md); format 1 was bare slugs.
        text = (self.target / MANIFEST_NAME).read_text(encoding="utf-8")
        return {ln.split("\t", 1)[0].strip() for ln in text.splitlines()
                if ln.strip() and not ln.startswith("#")}


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


class TestWrapper(InstallerTestBase):
    """bin/ml-ai-skills must run the scripts of the checkout it really lives
    in, even when reached through a symlink or a linked bin/ folder."""

    def setUp(self):
        super().setUp()
        self._links: list[Path] = []
        # A fake checkout whose install.sh only echoes: nothing real is ever run,
        # and no link ever points into this repository.
        self.fake = self.tmp / "checkout"
        (self.fake / "bin").mkdir(parents=True)
        (self.fake / "schema").mkdir()
        (self.fake / "INDEX.md").write_text("# index\n", encoding="utf-8")
        (self.fake / "schema" / "allowed_values.py").write_text("", encoding="utf-8")
        (self.fake / "install.sh").write_text('echo "REAL install.sh: $*"\n', encoding="utf-8", newline="\n")
        shutil.copy2(REPO_ROOT / "bin" / "ml-ai-skills", self.fake / "bin" / "ml-ai-skills")
        # Next to every link: a decoy that must never run.
        self.elsewhere = self.tmp / "elsewhere"
        self.elsewhere.mkdir()
        (self.elsewhere / "install.sh").write_text('echo "DECOY install.sh ran"\n', encoding="utf-8", newline="\n")
        (self.elsewhere / "INDEX.md").write_text("", encoding="utf-8")

    def tearDown(self):
        for link in reversed(self._links):  # before the recursive cleanup
            if os.path.lexists(link):
                try:
                    os.unlink(link)
                except OSError:
                    os.rmdir(link)
        super().tearDown()

    def wrapper(self, path: Path, *args: str) -> subprocess.CompletedProcess:
        return subprocess.run([BASH, str(path), *args], capture_output=True, text=True,
                              encoding="utf-8", errors="replace")

    def test_wrapper_reached_through_a_linked_bin_dir_uses_the_real_checkout(self):
        link = self.elsewhere / "bin2"
        try:
            os.symlink(self.fake / "bin", link, target_is_directory=True)
        except OSError:
            if os.name != "nt":
                raise
            import _winapi  # junctions need no privilege on Windows
            _winapi.CreateJunction(str(self.fake / "bin"), str(link))
        self._links.append(link)
        r = self.wrapper(link / "ml-ai-skills", "install", "--dry-run")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("REAL install.sh: --dry-run", r.stdout)
        self.assertNotIn("DECOY", r.stdout)

    def test_symlinked_wrapper_uses_the_real_checkout(self):
        link = self.elsewhere / "bin" / "ml-ai-skills"
        link.parent.mkdir()
        try:
            # Relative target: resolved against the link's own directory.
            os.symlink(os.path.relpath(self.fake / "bin" / "ml-ai-skills", link.parent), link)
        except OSError as e:
            self.skipTest(f"cannot create file symlinks here ({e})")
        self._links.append(link)
        r = self.wrapper(link, "install", "--dry-run")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("REAL install.sh: --dry-run", r.stdout)
        self.assertNotIn("DECOY", r.stdout)

    def test_exported_cdpath_does_not_redirect_the_wrapper(self):
        # Regression: `cd -P -- bin` (relative $0) went through CDPATH and the
        # echoed directory ended up inside SCRIPT_DIR.
        (self.elsewhere / "bin").mkdir()
        r = subprocess.run([BASH, "bin/ml-ai-skills", "install", "--dry-run"], capture_output=True,
                           text=True, encoding="utf-8", errors="replace", cwd=self.fake,
                           env={**os.environ, "CDPATH": "../elsewhere"})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.strip(), "REAL install.sh: --dry-run")

    def test_wrapper_outside_a_checkout_refuses_to_run_anything(self):
        lonely = self.tmp / "lonely"
        (lonely / "bin").mkdir(parents=True)
        shutil.copy2(REPO_ROOT / "bin" / "ml-ai-skills", lonely / "bin" / "ml-ai-skills")
        (lonely / "install.sh").write_text('echo "DECOY install.sh ran"\n', encoding="utf-8", newline="\n")
        r = self.wrapper(lonely / "bin" / "ml-ai-skills", "install")
        self.assertEqual(r.returncode, 1, r.stdout)
        self.assertIn("does not look like an ml-ai-skills checkout", r.stderr)
        self.assertNotIn("DECOY", r.stdout)


if __name__ == "__main__":
    unittest.main()
