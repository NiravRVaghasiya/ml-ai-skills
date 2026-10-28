"""Security/correctness tests for install.sh and uninstall.sh.

Every test builds a tiny fake ml-ai-skills checkout (3 skills) and a target
directory inside its own temp dir, so runs are fast and isolated; HOME is
pointed into the temp dir too, so even a bug that dropped --target could not
reach the real ~/.claude. Requires `bash` on PATH; skipped otherwise.

Link tests use real symlinks where the OS allows it. On Windows without the
symlink privilege (WinError 1314) directory links fall back to NTFS
junctions (no privilege needed, and Git Bash reports them as symlinks);
file-symlink tests are skipped with the reason.
"""
from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from testutil import REPO_ROOT

BASH = shutil.which("bash")
GIT = shutil.which("git")
INSTALL_SH = REPO_ROOT / "install.sh"
UNINSTALL_SH = REPO_ROOT / "uninstall.sh"
MANIFEST_NAME = ".ml-ai-skills-manifest"
LOCK_NAME = ".ml-ai-skills.lock"
SLUGS = ("alpha", "beta", "gamma")


def skill_text(slug: str, version: str = "1") -> str:
    return f"---\nname: {slug}\ndisplay_name: {slug.title()}\ntype: workflow\n---\nversion {version}\n"


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def is_link(path: Path) -> bool:
    isjunction = getattr(os.path, "isjunction", None)
    return path.is_symlink() or bool(isjunction and isjunction(path))


def snapshot(root: Path, exclude: Path | None = None) -> dict[str, tuple]:
    """Every entry below root (not following links) -> (kind, content)."""
    out: dict[str, tuple] = {}

    def walk(d: Path) -> None:
        for p in sorted(d.iterdir()):
            if exclude is not None and p == exclude:
                continue
            rel = p.relative_to(root).as_posix()
            if is_link(p):
                out[rel] = ("link", os.readlink(p))
            elif p.is_dir():
                out[rel] = ("dir",)
                walk(p)
            else:
                out[rel] = ("file", p.read_bytes())

    if root.exists():
        walk(root)
    return out


@unittest.skipUnless(BASH, "bash not found on PATH")
class Base(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.src = self.root / "src"
        # A space in the path on every test: quoting bugs surface everywhere.
        self.target = self.root / "inst root" / "skills"
        self.victim = self.root / "inst root" / "victim"
        self.victim.mkdir(parents=True)
        (self.victim / "keep.txt").write_text("precious", encoding="utf-8")
        self._links: list[Path] = []
        self.write_source({s: skill_text(s) for s in SLUGS})

    def tearDown(self):
        # Remove links before the recursive cleanup so it can never walk through them.
        for link in reversed(self._links):
            try:
                if os.path.lexists(link):
                    try:
                        os.unlink(link)
                    except OSError:
                        os.rmdir(link)
            except OSError:
                pass
        self._tmp.cleanup()

    # --- fixtures --------------------------------------------------------
    def write_source(self, skills: dict[str, str]) -> None:
        (self.src / "schema").mkdir(parents=True, exist_ok=True)
        (self.src / "INDEX.md").write_text("# index\n", encoding="utf-8")
        (self.src / "schema" / "allowed_values.py").write_text("", encoding="utf-8")
        for slug, body in skills.items():
            (self.src / slug).mkdir(exist_ok=True)
            (self.src / slug / "SKILL.md").write_bytes(body.encode("utf-8"))

    def bump_source(self, version: str) -> None:
        self.write_source({s: skill_text(s, version) for s in SLUGS if (self.src / s).exists()})

    def make_dir_link(self, link: Path, target: Path) -> None:
        try:
            os.symlink(target, link, target_is_directory=True)
        except OSError as e:
            if os.name != "nt":
                raise unittest.SkipTest(f"cannot create directory symlinks here: {e}") from e
            import _winapi  # junctions need no privilege on Windows
            _winapi.CreateJunction(str(target), str(link))
        self._links.append(link)

    def make_file_link(self, link: Path, target: Path) -> None:
        try:
            os.symlink(target, link)
        except OSError as e:
            raise unittest.SkipTest(f"cannot create file symlinks here ({e}); on Windows this needs "
                                    "Developer Mode or admin") from e
        self._links.append(link)

    # --- running ---------------------------------------------------------
    def env(self, **extra: str) -> dict[str, str]:
        env = dict(os.environ)
        env.pop("ML_AI_SKILLS_FAULT_INJECT", None)
        env["HOME"] = str(self.root / "home")
        env.update(extra)
        return env

    def _run(self, script: Path, args, env=None) -> subprocess.CompletedProcess:
        return subprocess.run([BASH, str(script), *args], capture_output=True, text=True,
                              encoding="utf-8", errors="replace", env=env or self.env())

    def install(self, *args: str, env=None, target: Path | None = None, source: Path | None = None):
        return self._run(INSTALL_SH, ["--source", str(source or self.src),
                                      "--target", str(target or self.target), *args], env)

    def uninstall(self, *args: str, env=None, target: Path | None = None):
        return self._run(UNINSTALL_SH, ["--target", str(target or self.target), *args], env)

    def ok(self, result: subprocess.CompletedProcess, code: int = 0) -> subprocess.CompletedProcess:
        self.assertEqual(result.returncode, code, f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}")
        return result

    # --- inspecting ------------------------------------------------------
    @property
    def manifest(self) -> Path:
        return self.target / MANIFEST_NAME

    def manifest_entries(self) -> dict[str, str]:
        entries = {}
        for line in self.manifest.read_text(encoding="utf-8").splitlines():
            if line and not line.startswith("#"):
                slug, _, h = line.partition("\t")
                entries[slug] = h
        return entries

    def content(self, slug: str) -> str:
        return (self.target / slug / "SKILL.md").read_text(encoding="utf-8")

    def assert_victim_intact(self) -> None:
        self.assertEqual(sorted(p.name for p in self.victim.iterdir()), ["keep.txt"])
        self.assertEqual((self.victim / "keep.txt").read_text(encoding="utf-8"), "precious")

    def assert_manifest_matches_disk(self) -> None:
        """Every manifest hash equals the real hash of the installed SKILL.md."""
        for slug, h in self.manifest_entries().items():
            self.assertEqual(h, sha256(self.target / slug / "SKILL.md"), slug)

    def assert_no_lock(self) -> None:
        self.assertFalse(os.path.lexists(self.target / LOCK_NAME), "lock/staging dir left behind")


class TestSharedHelpersInSync(unittest.TestCase):
    def test_shared_block_is_identical_in_both_scripts(self):
        def block(p: Path) -> str:
            text = p.read_text(encoding="utf-8")
            start = text.index("# >>> shared helpers")
            return text[start:text.index("# <<< shared helpers")]
        self.assertEqual(block(INSTALL_SH), block(UNINSTALL_SH))


class TestSlugValidation(Base):
    POISON = ["../victim", "..", ".", "a/b", ".hidden", "Upper", "x y", "-lead", "trail-", "a--b"]

    def poison_manifest(self) -> None:
        with self.manifest.open("a", encoding="utf-8", newline="\n") as f:
            for line in self.POISON:
                f.write(line + "\n")
            f.write("../victim\t" + "0" * 64 + "\n")

    def test_uninstall_ignores_traversal_entries_in_manifest(self):
        self.ok(self.install())
        self.poison_manifest()
        r = self.ok(self.uninstall())
        self.assertIn("not a valid skill slug", r.stderr)
        self.assert_victim_intact()
        self.assertEqual(list(self.target.iterdir()), [], "managed skills removed, nothing else")

    def test_install_drops_traversal_entries_from_manifest(self):
        self.ok(self.install())
        self.poison_manifest()
        self.ok(self.install("--force", "--prune"))
        self.assert_victim_intact()
        self.assertEqual(set(self.manifest_entries()), set(SLUGS))

    def test_skill_argument_traversal_is_rejected_before_any_change(self):
        self.ok(self.install())
        before = snapshot(self.root)
        for bad in ("../victim", "..", "alpha/..", "/abs", "", "Alpha"):
            r = self.uninstall("--skill", bad, "--force")
            self.assertEqual(r.returncode, 2, (bad, r.stderr))
            self.assertIn("not a valid skill slug", r.stderr)
        self.assertEqual(snapshot(self.root), before)

    def test_invalid_source_folder_names_are_not_installed(self):
        self.write_source({"Bad_Name": skill_text("x"), "dotted.name": skill_text("y")})
        r = self.ok(self.install())
        self.assertIn("not a valid skill slug", r.stderr)
        self.assertEqual({p.name for p in self.target.iterdir() if p.is_dir()}, set(SLUGS))


class TestCRLFManifest(Base):
    def test_crlf_manifest_is_understood(self):
        self.ok(self.install())
        raw = self.manifest.read_bytes()
        self.manifest.write_bytes(raw.replace(b"\n", b"\r\n"))
        self.ok(self.uninstall("--skill", "alpha"))
        self.assertFalse((self.target / "alpha").exists())
        r = self.ok(self.install())
        self.assertNotIn("not managed", r.stderr)  # beta/gamma still recognised as ours
        self.assertNotIn(b"\r", self.manifest.read_bytes())
        self.assertEqual(set(self.manifest_entries()), set(SLUGS))


class TestManifestV1Upgrade(Base):
    def write_v1(self, slugs=SLUGS) -> None:
        self.manifest.write_text("# ml-ai-skills manifest\n# source: x\n" + "".join(s + "\n" for s in slugs),
                                 encoding="utf-8")

    def test_v1_manifest_is_upgraded_with_hashes(self):
        self.ok(self.install())
        self.write_v1()
        r = self.ok(self.install())
        self.assertIn("updated=3 skipped=0", r.stdout)
        self.assertIn("# format: 2", self.manifest.read_text(encoding="utf-8"))
        self.assert_manifest_matches_disk()

    def test_v1_entry_that_differs_from_source_is_kept_unless_force(self):
        self.ok(self.install())
        self.write_v1()
        (self.target / "alpha" / "SKILL.md").write_text("my notes", encoding="utf-8")
        r = self.ok(self.install())
        self.assertIn("format 1", r.stderr)
        self.assertIn("--force --skill alpha", r.stderr, "a targeted way out, not just a global --force")
        self.assertEqual(self.content("alpha"), "my notes")
        self.assertEqual(self.manifest_entries()["alpha"], "-")
        self.ok(self.install("--force"))
        self.assertEqual(self.content("alpha"), skill_text("alpha"))
        self.assert_manifest_matches_disk()

    def test_uninstall_keeps_a_format_1_manifest_in_format_1(self):
        # Regression: a no-op (or partial) uninstall rewrote it as format 2 with '-'
        # hashes, so the next run claimed install.sh had already failed to prove them.
        self.ok(self.install())
        self.write_v1()
        before = self.manifest.read_bytes()
        r = self.ok(self.uninstall("--skill", "alpha"))
        self.assertIn("Run ./install.sh once", r.stderr)
        self.assertEqual(self.manifest.read_bytes(), before, "nothing removed, so nothing rewritten")
        self.ok(self.uninstall("--force", "--skill", "alpha"))
        self.assertFalse((self.target / "alpha").exists())
        self.assertNotIn("# format:", self.manifest.read_text(encoding="utf-8"))
        self.assertEqual(self.manifest_entries(), {"beta": "", "gamma": ""}, "bare format-1 slugs")
        r = self.ok(self.uninstall())
        self.assertEqual(r.stderr.count("Run ./install.sh once"), 2, r.stderr)
        self.assertNotIn("could not prove", r.stderr)
        # install.sh still upgrades it and proves the unedited copies.
        r = self.ok(self.install())
        self.assertIn("# format: 2", self.manifest.read_text(encoding="utf-8"))
        self.assert_manifest_matches_disk()

    def test_keep_message_mentions_git_history_only_when_it_was_checked(self):
        self.ok(self.install())  # self.src is not a git checkout
        self.write_v1()
        (self.target / "alpha" / "SKILL.md").write_text("my notes", encoding="utf-8")
        for run in ("first", "second"):  # the second run reads the format-2 manifest with alpha '-'
            r = self.ok(self.install())
            keep = [ln for ln in r.stderr.splitlines() if "keep    alpha" in ln]
            self.assertEqual(len(keep), 1, (run, r.stderr))
            self.assertNotIn("git history", keep[0], run)
            self.assertIn("the source is not a git checkout", keep[0], run)
            self.assertNotIn("old format 1 manifest", r.stdout + r.stderr, run)
        self.assertEqual(self.manifest_entries()["alpha"], "-")

    @unittest.skipUnless(GIT, "git not found on PATH")
    def test_keep_reason_is_per_skill(self):
        # Regression: one run-wide "git ran" flag. A copy with no SKILL.md is never
        # a git candidate, yet it got the git-history wording (or, alone, was told
        # the source is not a git checkout).
        genv = self.git_env()
        self.git("init", "-q")
        self.git("add", "-A")
        self.git("commit", "-qm", "v1")
        self.ok(self.install(env=genv))
        self.write_v1()
        (self.target / "alpha" / "SKILL.md").write_text("my notes", encoding="utf-8")
        (self.target / "beta" / "SKILL.md").unlink()
        (self.target / "beta" / "notes.txt").write_text("mine", encoding="utf-8")

        def keep_line(r, slug):
            lines = [ln for ln in r.stderr.splitlines() if f"keep    {slug} " in ln]
            self.assertEqual(len(lines), 1, r.stderr)
            return lines[0]

        r = self.ok(self.install(env=genv))
        self.assertIn("earlier release in the source's git history", keep_line(r, "alpha"))
        beta = keep_line(r, "beta")
        self.assertIn("no SKILL.md", beta)
        self.assertNotIn("git", beta)
        self.ok(self.install("--force", "--skill", "alpha", env=genv))
        r = self.ok(self.install(env=genv))  # beta is now the only '-' entry
        beta = keep_line(r, "beta")
        self.assertIn("no SKILL.md", beta)
        self.assertNotIn("git", beta)
        self.assertEqual((self.target / "beta" / "notes.txt").read_text(encoding="utf-8"), "mine")

    def test_uninstall_refuses_unverifiable_v1_entries_without_force(self):
        self.ok(self.install())
        self.write_v1()
        r = self.ok(self.uninstall())
        self.assertIn("format 1", r.stderr)
        self.assertTrue(all((self.target / s).is_dir() for s in SLUGS))
        self.ok(self.uninstall("--force"))
        self.assertFalse(any((self.target / s).exists() for s in SLUGS))
        self.assertFalse(self.manifest.exists())

    # The common real upgrade: installed with the old script, `git pull` brought
    # new SKILL.md versions, the user edited one skill. Only that one is kept.
    def git_env(self) -> dict[str, str]:
        cfg = self.root / "empty.gitconfig"
        cfg.touch()
        # No system/global config: the user's hooks, autocrlf or signing can't leak in.
        return self.env(GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=str(cfg))

    def git(self, *args: str) -> None:
        subprocess.run([GIT, "-C", str(self.src), "-c", "user.name=t", "-c", "user.email=t@example.invalid",
                        "-c", "commit.gpgsign=false", *args], check=True, capture_output=True, env=self.git_env())

    @unittest.skipUnless(GIT, "git not found on PATH")
    def test_v1_upgrade_after_upstream_change_verifies_against_git_history(self):
        genv = self.git_env()
        self.git("init", "-q")
        self.git("add", "-A")
        self.git("commit", "-qm", "v1")
        self.ok(self.install(env=genv))
        self.write_v1()
        (self.target / "alpha" / "SKILL.md").write_text("my notes", encoding="utf-8")
        self.bump_source("2")
        self.git("commit", "-qam", "v2")

        r = self.ok(self.install(env=genv))
        self.assertIn("2 match an earlier release", r.stdout)
        self.assertIn("updated=2 skipped=1", r.stdout)
        keep = [ln for ln in r.stderr.splitlines() if "keep    alpha" in ln]
        self.assertEqual(len(keep), 1, r.stderr)
        self.assertIn("earlier release in the source's git history", keep[0])
        self.assertEqual(self.content("alpha"), "my notes")
        self.assertEqual(self.content("beta"), skill_text("beta", "2"))
        self.assertEqual(self.content("gamma"), skill_text("gamma", "2"))
        entries = self.manifest_entries()
        self.assertEqual(entries["alpha"], "-")
        for slug in ("beta", "gamma"):
            self.assertEqual(entries[slug], sha256(self.target / slug / "SKILL.md"))

        # uninstall must not send the user back to install.sh in a loop.
        r = self.ok(self.uninstall("--dry-run"))
        self.assertNotIn("Run ./install.sh once", r.stderr)
        self.assertIn("--force --skill alpha", r.stderr)

        # Targeted escape: replaces alpha only.
        (self.target / "beta" / "SKILL.md").write_text("beta edit", encoding="utf-8")
        self.ok(self.install("--force", "--skill", "alpha", env=genv))
        self.assertEqual(self.content("alpha"), skill_text("alpha", "2"))
        self.assertEqual(self.content("beta"), "beta edit")


class TestInstallSkillFilter(Base):
    def test_invalid_skill_argument_is_rejected_before_any_change(self):
        self.ok(self.install())
        self.bump_source("2")
        before = snapshot(self.root)
        for bad in ("../victim", "..", "alpha/..", "/abs", "", "Alpha"):
            r = self.install("--skill", bad, "--force")
            self.assertEqual(r.returncode, 2, (bad, r.stderr))
            self.assertIn("not a valid skill slug", r.stderr)
        self.assertEqual(snapshot(self.root), before)

    def test_only_named_skills_are_installed_updated_or_pruned(self):
        self.ok(self.install())
        self.bump_source("2")
        r = self.ok(self.install("--skill", "beta"))
        self.assertIn("updated=1 ", r.stdout)
        self.assertEqual(self.content("alpha"), skill_text("alpha", "1"))
        self.assertEqual(self.content("beta"), skill_text("beta", "2"))
        self.assert_manifest_matches_disk()

        shutil.rmtree(self.src / "gamma")
        self.ok(self.install("--prune", "--skill", "beta"))
        self.assertTrue((self.target / "gamma").is_dir(), "gamma is out of scope")
        self.ok(self.install("--prune", "--skill", "gamma"))
        self.assertFalse((self.target / "gamma").exists())
        self.assertEqual(set(self.manifest_entries()), {"alpha", "beta"})

        r = self.ok(self.install("--skill", "nosuch"))
        self.assertIn("neither in the source nor installed", r.stderr)
        self.assertIn("installed=0 updated=0", r.stdout)


class TestLocalModifications(Base):
    def test_update_keeps_user_edits_and_force_overwrites(self):
        self.ok(self.install())
        original = self.manifest_entries()["alpha"]
        (self.target / "alpha" / "SKILL.md").write_text("my edit", encoding="utf-8")
        self.bump_source("2")
        r = self.ok(self.install())
        self.assertIn("modified locally", r.stderr)
        self.assertEqual(self.content("alpha"), "my edit")
        self.assertEqual(self.content("beta"), skill_text("beta", "2"))
        self.assertEqual(self.manifest_entries()["alpha"], original, "keep the install-time hash")

        self.ok(self.install("--force"))
        self.assertEqual(self.content("alpha"), skill_text("alpha", "2"))
        self.assert_manifest_matches_disk()

    def test_uninstall_keeps_user_edits_unless_force(self):
        self.ok(self.install())
        (self.target / "alpha" / "SKILL.md").write_text("my edit", encoding="utf-8")
        r = self.ok(self.uninstall())
        self.assertIn("modified locally", r.stderr)
        self.assertEqual(self.content("alpha"), "my edit")
        self.assertFalse((self.target / "beta").exists())
        self.assertEqual(set(self.manifest_entries()), {"alpha"})

        self.ok(self.uninstall("--force"))
        self.assertFalse((self.target / "alpha").exists())
        self.assertFalse(self.manifest.exists())
        self.assert_no_lock()

    def test_deleted_skill_md_counts_as_modified(self):
        self.ok(self.install())
        (self.target / "beta" / "SKILL.md").unlink()
        (self.target / "beta" / "notes.md").write_text("mine", encoding="utf-8")
        self.ok(self.uninstall())
        self.assertTrue((self.target / "beta" / "notes.md").exists())


class TestUpstreamRemoval(Base):
    def remove_from_source(self, slug: str) -> None:
        shutil.rmtree(self.src / slug)

    def test_removed_skill_stays_tracked_until_prune(self):
        self.ok(self.install())
        self.remove_from_source("gamma")
        r = self.ok(self.install())
        self.assertIn("--prune", r.stderr)
        self.assertTrue((self.target / "gamma").is_dir())
        self.assertIn("gamma", self.manifest_entries())

        self.ok(self.install("--prune"))
        self.assertFalse((self.target / "gamma").exists())
        self.assertEqual(set(self.manifest_entries()), {"alpha", "beta"})
        self.assert_no_lock()

    def test_prune_keeps_modified_skill_unless_force(self):
        self.ok(self.install())
        self.remove_from_source("gamma")
        (self.target / "gamma" / "SKILL.md").write_text("edited", encoding="utf-8")
        self.ok(self.install("--prune"))
        self.assertEqual(self.content("gamma"), "edited")
        self.assertIn("gamma", self.manifest_entries())
        self.ok(self.install("--prune", "--force"))
        self.assertFalse((self.target / "gamma").exists())

    def test_removed_upstream_and_on_disk_is_forgotten(self):
        self.ok(self.install())
        self.remove_from_source("gamma")
        shutil.rmtree(self.target / "gamma")
        self.ok(self.install())
        self.assertNotIn("gamma", self.manifest_entries())


class TestIdempotency(Base):
    def test_two_installs_give_identical_tree_and_manifest(self):
        def normalized():
            snap = snapshot(self.target)
            kind, data = snap[MANIFEST_NAME]
            snap[MANIFEST_NAME] = (kind, b"\n".join(ln for ln in data.split(b"\n")
                                                     if not ln.startswith(b"# updated_at:")))
            return snap
        self.ok(self.install())
        first = normalized()
        r = self.ok(self.install())
        self.assertIn("updated=3 skipped=0", r.stdout)
        self.assertEqual(normalized(), first)
        self.assert_manifest_matches_disk()


class TestDryRun(Base):
    def test_dry_run_on_fresh_target_creates_nothing(self):
        before = snapshot(self.root)
        self.ok(self.install("--dry-run"))
        self.assertEqual(snapshot(self.root), before)
        self.assertFalse(self.target.exists())

    def test_dry_run_with_pending_changes_writes_nothing(self):
        self.ok(self.install())
        (self.target / "alpha" / "SKILL.md").write_text("edit", encoding="utf-8")
        (self.target / "foreign").mkdir()
        (self.target / "foreign" / "SKILL.md").write_text("theirs", encoding="utf-8")
        self.write_source({"foreign": skill_text("foreign"), "delta": skill_text("delta")})
        self.bump_source("2")
        shutil.rmtree(self.src / "gamma")
        with self.manifest.open("a", encoding="utf-8") as f:
            f.write("../victim\n")
        before = snapshot(self.root)
        for args in ((), ("--force",), ("--prune",), ("--force", "--prune")):
            r = self.install("--dry-run", *args)
            self.assertIn(r.returncode, (0,), r.stderr)
            self.assertEqual(snapshot(self.root), before, args)
        for args in ((), ("--force",), ("--skill", "beta")):
            self.ok(self.uninstall("--dry-run", *args))
            self.assertEqual(snapshot(self.root), before, args)


class TestLocking(Base):
    def test_held_lock_fails_fast_without_changes(self):
        self.ok(self.install())
        lock = self.target / LOCK_NAME
        lock.mkdir()
        (lock / "owner").write_text("pid=999 started=then\n", encoding="utf-8")
        self.bump_source("2")
        before = snapshot(self.root)
        r = self.install()
        self.assertEqual(r.returncode, 3, r.stderr)
        self.assertIn("holds the lock", r.stderr)
        self.assertIn("pid=999", r.stderr)
        self.assertEqual(snapshot(self.root), before)
        r = self.uninstall("--force")
        self.assertEqual(r.returncode, 3, r.stderr)
        self.assertEqual(snapshot(self.root), before, "someone else's lock must not be removed")

    def test_file_at_lock_path_is_an_error_not_a_held_lock(self):
        self.ok(self.install())
        (self.target / LOCK_NAME).write_text("not ours", encoding="utf-8")
        before = snapshot(self.root)
        for r in (self.install(), self.uninstall("--force")):
            self.assertEqual(r.returncode, 1, r.stderr)
            self.assertIn("is not a directory", r.stderr)
            self.assertNotIn("holds the lock", r.stderr)
        self.assertEqual(snapshot(self.root), before)


class TestRollback(Base):
    def test_failed_commit_restores_previous_copy_and_manifest_stays_accurate(self):
        self.ok(self.install())
        self.bump_source("2")
        r = self.install(env=self.env(ML_AI_SKILLS_FAULT_INJECT="commit:beta"))
        self.assertEqual(r.returncode, 1, r.stderr)
        self.assertIn("restored previous copy of beta", r.stdout)
        self.assertEqual(self.content("alpha"), skill_text("alpha", "2"))
        self.assertEqual(self.content("beta"), skill_text("beta", "1"))
        self.assertEqual(self.content("gamma"), skill_text("gamma", "2"))
        self.assert_manifest_matches_disk()
        self.assert_no_lock()

        r = self.ok(self.install())
        self.assertNotIn("modified locally", r.stderr)
        self.assertEqual(self.content("beta"), skill_text("beta", "2"))

    def test_failed_fresh_install_leaves_no_trace_of_that_skill(self):
        r = self.install(env=self.env(ML_AI_SKILLS_FAULT_INJECT="commit:beta"))
        self.assertEqual(r.returncode, 1)
        self.assertFalse((self.target / "beta").exists())
        self.assertEqual(set(self.manifest_entries()), {"alpha", "gamma"})
        self.assert_manifest_matches_disk()
        self.assert_no_lock()

    def test_failed_staging_changes_nothing(self):
        self.ok(self.install())
        self.bump_source("2")
        before = snapshot(self.target)
        r = self.install(env=self.env(ML_AI_SKILLS_FAULT_INJECT="stage"))
        self.assertEqual(r.returncode, 1)
        self.assertIn("nothing was changed", r.stderr)
        self.assertEqual(snapshot(self.target), before)


# A PATH wrapper around mv: it performs the real move, then (when "$1|$2"
# matches the glob in ML_TEST_MV_TRIGGER) either SIGTERMs the calling script,
# i.e. a signal that arrives while that mv is running, or recreates the folder
# it just moved away (another process "squatting" the destination mid-commit).
# Portable, and it needs no change to the scripts under test.
MV_WRAPPER = """#!/bin/sh
"$ML_TEST_REAL_MV" "$@"
rc=$?
case "$1|$2" in
  $ML_TEST_MV_TRIGGER)
    case "$ML_TEST_MV_ACTION" in
      kill) kill -TERM "$PPID" ;;
      squat) mkdir "$1" && printf 'squatter' > "$1/SKILL.md" ;;
    esac ;;
esac
case "$1|$2" in
  ${ML_TEST_MV_TRIGGER2:-}) kill -TERM "$PPID" ;;
esac
exit $rc
"""


class TestInterruptedCommit(Base):
    def wrapped_env(self, trigger: str, action: str = "kill", trigger2: str = "") -> dict[str, str]:
        wrap = self.root / "wrapbin"
        wrap.mkdir(exist_ok=True)
        (wrap / "mv").write_text(MV_WRAPPER, encoding="utf-8", newline="\n")
        os.chmod(wrap / "mv", 0o755)  # noqa: S103 - the fake `mv` must be executable to shadow the real one
        real_mv = subprocess.run([BASH, "-c", "command -v mv"], capture_output=True, text=True).stdout.strip()
        self.assertTrue(real_mv)
        return self.env(PATH=str(wrap) + os.pathsep + os.environ.get("PATH", ""), ML_TEST_REAL_MV=real_mv,
                        ML_TEST_MV_TRIGGER=trigger, ML_TEST_MV_ACTION=action, ML_TEST_MV_TRIGGER2=trigger2)

    def test_sigterm_while_moving_old_copy_aside_restores_it(self):
        # Regression: the trap ran with the progress flag still "nothing moved",
        # so the previous copy was deleted with the lock dir and dropped from the manifest.
        self.ok(self.install())
        self.bump_source("2")
        r = self.install(env=self.wrapped_env("*|*/old/beta"))
        self.assertEqual(r.returncode, 143, r.stderr)
        self.assertIn("restored previous copy of beta", r.stdout)
        self.assertEqual(self.content("beta"), skill_text("beta", "1"))
        self.assertEqual(set(self.manifest_entries()), set(SLUGS))
        self.assert_manifest_matches_disk()
        self.assert_no_lock()
        r = self.ok(self.install())
        self.assertNotIn("modified locally", r.stderr)
        self.assertEqual(self.content("beta"), skill_text("beta", "2"))

    def test_sigterm_right_after_new_copy_lands_records_it(self):
        self.ok(self.install())
        self.bump_source("2")
        r = self.install(env=self.wrapped_env("*/stage/beta|*"))
        self.assertEqual(r.returncode, 143, r.stderr)
        self.assertEqual(self.content("beta"), skill_text("beta", "2"))
        self.assertEqual(set(self.manifest_entries()), set(SLUGS))
        self.assert_manifest_matches_disk()
        self.assert_no_lock()

    def test_sigterm_during_fresh_install_keeps_landed_skill_tracked(self):
        r = self.install(env=self.wrapped_env("*/stage/beta|*"))
        self.assertEqual(r.returncode, 143, r.stderr)
        self.assertEqual(set(self.manifest_entries()), {"alpha", "beta"})
        self.assertFalse((self.target / "gamma").exists())
        self.assert_manifest_matches_disk()
        self.assert_no_lock()

    def test_sigterm_during_cleanup_is_ignored(self):
        # SIGTERM while the EXIT-trap cleanup renames the manifest into place
        # ("-f|<tmp>": write_manifest runs `mv -f`). Without `trap '' ...` in
        # cleanup, the TERM trap's `exit 143` cut it short and left the lock.
        self.ok(self.install())
        self.bump_source("2")
        r = self.install(env=self.wrapped_env("no-match", trigger2="*manifest.tmp*"))
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.content("beta"), skill_text("beta", "2"))
        self.assertEqual(set(self.manifest_entries()), set(SLUGS))
        self.assert_manifest_matches_disk()
        self.assert_no_lock()

    def test_folder_appearing_mid_commit_never_costs_the_old_copy(self):
        self.ok(self.install())
        recorded = self.manifest_entries()["beta"]
        self.bump_source("2")
        r = self.install(env=self.wrapped_env("*|*/old/beta", action="squat"))
        self.assertEqual(r.returncode, 1, r.stderr)
        self.assertIn("previous copy is kept", r.stderr)
        self.assertEqual(self.content("beta"), "squatter", "the newcomer is not overwritten")
        kept = self.target / LOCK_NAME / "old" / "beta" / "SKILL.md"
        self.assertEqual(kept.read_text(encoding="utf-8"), skill_text("beta", "1"))
        self.assertEqual(self.manifest_entries()["beta"], recorded, "the new hash was not recorded")
        self.assertEqual(self.install().returncode, 3, "the kept lock dir blocks the next run until inspected")


class TestPathsWithSpecialCharacters(Base):
    def test_full_cycle_with_spaces_quotes_and_dollar_signs(self):
        odd = self.root / "it's a $HOME dir"
        src = odd / "source checkout"
        shutil.copytree(self.src, src)
        target = odd / "my skills"
        self.ok(self.install(source=src, target=target))
        # The literal "$HOME" directory received the install: no expansion/eval happened.
        self.assertEqual({p.name for p in target.iterdir() if p.is_dir()}, set(SLUGS))
        self.ok(self.uninstall(target=target))
        self.assertEqual(list(target.iterdir()), [])


class TestOptionLikePaths(Base):
    def test_target_and_project_values_starting_with_a_dash_are_paths(self):
        # Regression: `cd -P "$TARGET_DIR"` with '-' ran `cd -` (jump to $OLDPWD),
        # and '-L' was parsed as a cd option (-> $HOME).
        work = self.root / "work"
        work.mkdir()
        other = self.root / "other"
        other.mkdir()
        env = self.env(OLDPWD=str(other))

        def run(script: Path, *args: str) -> subprocess.CompletedProcess:
            return subprocess.run([BASH, str(script), *args], capture_output=True, text=True,
                                  encoding="utf-8", errors="replace", env=env, cwd=work)

        self.ok(run(INSTALL_SH, "--source", str(self.src), "--target", "-"))
        self.assertEqual({p.name for p in (work / "-").iterdir() if p.is_dir()}, set(SLUGS))
        self.assertEqual(list(other.iterdir()), [], "the run must not be redirected to $OLDPWD")
        (work / "-L").mkdir()
        r = self.ok(run(INSTALL_SH, "--source", str(self.src), "--target", "-L", "--dry-run"))
        target_line = next(ln for ln in r.stdout.splitlines() if ln.startswith("Target: "))
        self.assertTrue(target_line.endswith("/-L"), target_line)
        (work / "-p").mkdir()
        self.ok(run(INSTALL_SH, "--source", str(self.src), "--project", "-p"))
        self.assertTrue((work / "-p" / ".claude" / "skills" / "alpha").is_dir())
        self.ok(run(UNINSTALL_SH, "--project", "-p"))
        self.assertEqual(list((work / "-p" / ".claude" / "skills").iterdir()), [])
        self.ok(run(UNINSTALL_SH, "--target", "-"))
        self.assertEqual(list((work / "-").iterdir()), [])
        self.assertEqual(list(other.iterdir()), [])
        self.assertFalse((self.root / "home").exists(), "nothing landed under $HOME")

    def test_exported_cdpath_is_ignored(self):
        # Regression: with CDPATH exported, `$(cd -P "$DIR" && pwd)` resolved a
        # relative DIR through CDPATH and also captured cd's echo of the new
        # directory, so TARGET_DIR became a two-line value.
        elsewhere = self.root / "elsewhere"
        for d in ("src", "tgt", "proj"):
            (elsewhere / d).mkdir(parents=True)
        (self.root / "proj").mkdir()
        env = self.env(CDPATH="elsewhere")  # relative entry: resolved against cwd

        def run(script: Path, *args: str) -> subprocess.CompletedProcess:
            return subprocess.run([BASH, str(script), *args], capture_output=True, text=True,
                                  encoding="utf-8", errors="replace", env=env, cwd=self.root)

        self.ok(run(INSTALL_SH, "--source", "src", "--target", "tgt"))
        self.assertEqual({p.name for p in (self.root / "tgt").iterdir() if p.is_dir()}, set(SLUGS))
        self.ok(run(INSTALL_SH, "--source", "src", "--project", "proj"))
        self.assertTrue((self.root / "proj" / ".claude" / "skills" / "alpha").is_dir())
        self.ok(run(UNINSTALL_SH, "--project", "proj"))
        self.assertEqual(list((self.root / "proj" / ".claude" / "skills").iterdir()), [])
        self.ok(run(UNINSTALL_SH, "--target", "tgt"))
        self.assertEqual(list((self.root / "tgt").iterdir()), [])
        for d in ("src", "tgt", "proj"):
            self.assertEqual(list((elsewhere / d).iterdir()), [], d)


class TestLinks(Base):
    def test_full_cycle_through_a_linked_target_dir(self):
        # TARGET_DIR itself may be a link (dotfiles setups): resolved once, and
        # the run then operates inside the real directory; the link survives.
        real = self.root / "dotfiles" / "skills"
        real.mkdir(parents=True)
        self.make_dir_link(self.target, real)
        self.ok(self.install())
        self.assertEqual({p.name for p in real.iterdir() if p.is_dir()}, set(SLUGS))
        self.assertTrue((real / MANIFEST_NAME).is_file())
        self.assert_manifest_matches_disk()
        self.bump_source("2")
        self.ok(self.install())
        self.assertEqual(self.content("beta"), skill_text("beta", "2"))
        self.ok(self.uninstall())
        self.assertEqual(list(real.iterdir()), [])
        self.assertTrue(is_link(self.target), "the link itself is left in place")
        self.assert_victim_intact()

    def test_source_skill_containing_a_directory_link_is_refused(self):
        self.make_dir_link(self.src / "beta" / "escape", self.victim)
        r = self.install()
        self.assertEqual(r.returncode, 1)
        self.assertIn("symlink", r.stderr)
        self.assertFalse((self.target / "beta").exists())
        self.assertTrue((self.target / "alpha").is_dir())
        self.assertNotIn("beta", self.manifest_entries())
        self.assert_victim_intact()

    def test_source_skill_that_is_a_link_is_refused(self):
        outside = self.root / "outside-skill"
        outside.mkdir()
        (outside / "SKILL.md").write_text(skill_text("delta"), encoding="utf-8")
        self.make_dir_link(self.src / "delta", outside)
        r = self.install()
        self.assertEqual(r.returncode, 1)
        self.assertFalse(os.path.lexists(self.target / "delta"))

    def test_source_skill_containing_a_file_symlink_is_refused(self):
        self.make_file_link(self.src / "beta" / "passwd", self.victim / "keep.txt")
        r = self.install()
        self.assertEqual(r.returncode, 1)
        self.assertFalse((self.target / "beta").exists())

    def test_managed_skill_replaced_by_a_link_is_never_followed(self):
        self.ok(self.install())
        shutil.rmtree(self.target / "beta")
        self.make_dir_link(self.target / "beta", self.victim)
        self.bump_source("2")
        r = self.install("--force")
        self.assertEqual(r.returncode, 1)
        self.assertIn("symlink", r.stderr)
        self.assert_victim_intact()
        r = self.uninstall("--force")
        self.assertEqual(r.returncode, 1)
        self.assert_victim_intact()
        self.assertTrue(is_link(self.target / "beta"))
        self.assert_no_lock()

    def test_managed_skill_containing_a_link_is_not_removed(self):
        self.ok(self.install())
        self.make_dir_link(self.target / "beta" / "escape", self.victim)
        for args in ((), ("--force",)):
            r = self.uninstall(*args)
            self.assertEqual(r.returncode, 1, r.stderr)
            self.assertIn("symlink", r.stderr)
            self.assert_victim_intact()
        self.assertTrue(is_link(self.target / "beta" / "escape"))
        r = self.install("--force")
        self.assertEqual(r.returncode, 1)
        self.assert_victim_intact()

    def test_lock_path_that_is_a_link_is_refused(self):
        self.target.mkdir(parents=True)
        self.make_dir_link(self.target / LOCK_NAME, self.victim)
        r = self.install()
        self.assertEqual(r.returncode, 1)
        self.assertIn("symlink", r.stderr)
        self.assert_victim_intact()

    def test_manifest_that_is_a_symlink_is_refused(self):
        self.ok(self.install())
        real = self.victim / "keep.txt"
        self.manifest.unlink()
        self.make_file_link(self.manifest, real)
        for r in (self.install(), self.uninstall("--force")):
            self.assertEqual(r.returncode, 1, r.stderr)
            self.assertIn("symlink", r.stderr)
        self.assert_victim_intact()


class TestNoWritesOutsideTarget(Base):
    def test_every_operation_only_writes_inside_target(self):
        def run_checked(fn, *args, code=0):
            before = snapshot(self.root, exclude=self.target)
            self.ok(fn(*args), code)
            self.assertEqual(snapshot(self.root, exclude=self.target), before, (fn.__name__, args))

        run_checked(self.install)
        self.bump_source("2")
        (self.target / "alpha" / "SKILL.md").write_text("edit", encoding="utf-8")
        run_checked(self.install)
        run_checked(self.install, "--force")
        shutil.rmtree(self.src / "gamma")
        run_checked(self.install, "--prune")
        run_checked(self.uninstall, "--skill", "alpha")
        run_checked(self.uninstall, "--force")


class TestMiscErrors(Base):
    def test_unknown_manifest_format_is_refused(self):
        self.ok(self.install())
        text = self.manifest.read_text(encoding="utf-8").replace("# format: 2", "# format: 3")
        self.manifest.write_text(text, encoding="utf-8")
        before = snapshot(self.target)
        for r in (self.install("--force"), self.uninstall("--force")):
            self.assertEqual(r.returncode, 1, r.stderr)
            self.assertIn("format", r.stderr)
        self.assertEqual(snapshot(self.target), before)

    def test_manifest_with_a_nul_byte_is_refused(self):
        # bash's `read` drops NUL, which turned 'al<NUL>pha' into the valid slug 'alpha'.
        self.ok(self.install())
        self.manifest.write_bytes(b"al\x00pha\n")
        before = snapshot(self.root)
        for r in (self.uninstall("--force"), self.uninstall("--dry-run", "--force"),
                  self.install("--force"), self.install("--dry-run")):
            self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
            self.assertIn("NUL byte", r.stderr)
            self.assertNotIn("remove  alpha", r.stdout)
        self.assertEqual(snapshot(self.root), before)
        self.assert_no_lock()

    def test_target_that_is_a_file_is_refused(self):
        self.target.parent.mkdir(parents=True, exist_ok=True)
        self.target.write_text("x", encoding="utf-8")
        r = self.install()
        self.assertEqual(r.returncode, 1)
        self.assertIn("not a directory", r.stderr)

    def test_target_equal_to_source_is_refused(self):
        r = self.install(target=self.src, source=self.src)
        self.assertEqual(r.returncode, 1)
        self.assertIn("source checkout itself", r.stderr)

    def test_repeated_skill_argument_is_processed_once(self):
        self.ok(self.install())
        r = self.ok(self.uninstall("--skill", "alpha", "--skill", "alpha"))
        self.assertIn("removed=1 ", r.stdout)
        self.assertEqual(set(self.manifest_entries()), {"beta", "gamma"})

    def test_force_adopts_unmanaged_collision_into_manifest(self):
        (self.target / "beta").mkdir(parents=True)
        (self.target / "beta" / "SKILL.md").write_text("theirs", encoding="utf-8")
        r = self.ok(self.install())
        self.assertNotIn("beta", self.manifest_entries())
        self.assertIn("not managed", r.stderr)
        self.ok(self.install("--force"))
        self.assertEqual(self.content("beta"), skill_text("beta"))
        self.assert_manifest_matches_disk()
        self.assertIn("beta", self.manifest_entries())

    def test_usage_errors_exit_2(self):
        self.assertEqual(self.install("--bogus").returncode, 2)
        self.assertEqual(self.uninstall("--bogus").returncode, 2)
        self.assertEqual(self._run(INSTALL_SH, ["--target"]).returncode, 2)


if __name__ == "__main__":
    unittest.main()
