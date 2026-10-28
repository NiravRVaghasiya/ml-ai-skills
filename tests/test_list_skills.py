"""Tests for scripts/list_skills.py --target: manifest parsing (formats 1 and
2, CRLF, invalid slugs) and per-skill status (ok / modified / unverified /
missing / unsafe). Pure stdlib; the one cross-check against install.sh is
skipped when bash is not on PATH."""
from __future__ import annotations

import contextlib
import hashlib
import io
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from testutil import REPO_ROOT

import list_skills as ls  # scripts/ is on sys.path via testutil

BASH = shutil.which("bash")
SKILL = "---\nname: {slug}\ndisplay_name: {title}\ntype: workflow\n---\nbody\n"


class Base(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.target = self.root / "skills"
        self.target.mkdir()
        self._links: list[Path] = []

    def tearDown(self):
        # Remove links first so the recursive cleanup can never walk through them.
        for link in reversed(self._links):
            if os.path.lexists(link):
                try:
                    os.unlink(link)
                except OSError:
                    os.rmdir(link)
        self._tmp.cleanup()

    def add_skill(self, slug: str) -> str:
        d = self.target / slug
        d.mkdir()
        (d / "SKILL.md").write_text(SKILL.format(slug=slug, title=slug.title()), encoding="utf-8")
        return hashlib.sha256((d / "SKILL.md").read_bytes()).hexdigest()

    def write_manifest(self, text: str, newline: str = "\n") -> Path:
        p = self.target / ls.MANIFEST_NAME
        p.write_bytes(text.replace("\n", newline).encode("utf-8"))
        return p

    def make_dir_link(self, link: Path, target: Path) -> None:
        try:
            os.symlink(target, link, target_is_directory=True)
        except OSError:
            if os.name != "nt":
                self.skipTest("cannot create directory symlinks here")
            import _winapi  # junctions need no privilege on Windows
            _winapi.CreateJunction(str(target), str(link))
        self._links.append(link)

    def run_list(self) -> tuple[int, str, str]:
        out, errs = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(errs):
            rc = ls.list_installed(self.target)
        return rc, out.getvalue(), errs.getvalue()


class TestReadManifest(Base):
    def test_format_2_entries_and_headers(self):
        h = "a" * 64
        m = ls.read_manifest(self.write_manifest(
            f"# ml-ai-skills manifest\n# format: 2\n# source: /src\n# updated_at: x\nalpha\t{h}\nbeta\t-\n"))
        self.assertEqual(m.format, 2)
        self.assertEqual(m.source, "/src")
        self.assertEqual(m.entries, {"alpha": h, "beta": None})
        self.assertEqual(m.warnings, [])

    def test_format_1_bare_slugs(self):
        m = ls.read_manifest(self.write_manifest("# source: /src\nalpha\nbeta\n"))
        self.assertEqual(m.format, 1)
        self.assertEqual(m.entries, {"alpha": None, "beta": None})

    def test_crlf_is_stripped(self):
        h = "b" * 64
        m = ls.read_manifest(self.write_manifest(f"# format: 2\nalpha\t{h}\nbeta\n", newline="\r\n"))
        self.assertEqual(m.entries, {"alpha": h, "beta": None})

    def test_invalid_slugs_are_ignored_with_warnings(self):
        m = ls.read_manifest(self.write_manifest("alpha\n../victim\n/abs\na/b\n.hidden\nUpper\n-x\n"))
        self.assertEqual(list(m.entries), ["alpha"])
        self.assertEqual(len(m.warnings), 6)

    def test_first_duplicate_wins_and_bad_hash_is_unverifiable(self):
        h = "c" * 64
        m = ls.read_manifest(self.write_manifest(f"# format: 2\nalpha\t{h}\nalpha\t{'d' * 64}\nbeta\tnothex\n"))
        self.assertEqual(m.entries, {"alpha": h, "beta": None})
        self.assertEqual(len(m.warnings), 1)

    def test_unknown_format_is_refused(self):
        with self.assertRaises(ls.ManifestError):
            ls.read_manifest(self.write_manifest("# format: 3\nalpha\n"))

    def test_format_is_compared_as_a_string_like_bash(self):
        # bash's `case "$fmt" in 1|2)` refuses "02"; int("02") would accept it.
        for declared in ("02", "2.0", "+2", " 2x"):
            with self.subTest(declared=declared), self.assertRaises(ls.ManifestError):
                ls.read_manifest(self.write_manifest(f"# format: {declared}\nalpha\n"))
        self.assertEqual(ls.read_manifest(self.write_manifest("# format: \t2 \nalpha\n")).format, 2)

    def test_nul_byte_is_refused_not_dropped(self):
        # bash's `read` silently drops NUL, which would turn 'al\0pha' into the
        # valid slug 'alpha'; all three tools refuse such a manifest instead.
        p = self.target / ls.MANIFEST_NAME
        p.write_bytes(b"al\x00pha\n")
        with self.assertRaises(ls.ManifestError):
            ls.read_manifest(p)
        self.add_skill("alpha")
        rc, _, err = self.run_list()
        self.assertEqual(rc, 1)
        self.assertIn("NUL", err)

    def test_only_ascii_whitespace_is_stripped_like_bash(self):
        # U+00A0 is not [:space:] under LC_ALL=C, so install.sh rejects the slug.
        h = "a" * 64
        m = ls.read_manifest(self.write_manifest(f"# format: 2\n\u00a0alpha\t{h}\nbeta\u00a0\n \t gamma \t\n\u00a0\n"))
        self.assertEqual(m.entries, {"gamma": None})
        self.assertEqual(len(m.warnings), 3)


class TestListInstalled(Base):
    def test_reports_ok_modified_unverified_and_missing(self):
        h_ok = self.add_skill("alpha")
        h_mod = self.add_skill("beta")
        (self.target / "beta" / "SKILL.md").write_text(
            SKILL.format(slug="beta", title="Beta") + "edit\n", encoding="utf-8")
        self.add_skill("gamma")
        self.write_manifest(f"# format: 2\nalpha\t{h_ok}\nbeta\t{h_mod}\ngamma\t-\ndelta\t{'e' * 64}\n../x\n")
        rc, out, err = self.run_list()
        self.assertEqual(rc, 0)
        lines = {ln.split()[1]: ln for ln in out.splitlines() if ln.startswith("  [")}
        self.assertNotIn("MODIFIED", lines["alpha"])
        self.assertIn("MODIFIED", lines["beta"])
        self.assertIn("UNVERIFIED", lines["gamma"])
        self.assertIn("[MISSING] delta", out)
        self.assertIn("1 missing, 1 modified, 1 unverified", out)
        self.assertIn("not a valid skill slug", err)
        self.assertNotIn("../x", lines)

    def test_unverified_note_depends_on_manifest_format(self):
        # Format 1: install.sh has not run since, so re-running it can still help.
        # Format 2 '-': install.sh already tried and failed to prove the copy.
        self.add_skill("alpha")
        self.write_manifest("alpha\n")
        _, out, _ = self.run_list()
        self.assertIn("re-run install.sh", out)
        self.write_manifest("# format: 2\nalpha\t-\n")
        _, out, _ = self.run_list()
        self.assertIn("UNVERIFIED", out)
        self.assertNotIn("format-1 manifest has no hash", out)
        self.assertNotIn("re-run install.sh", out)
        self.assertIn("--force --skill alpha", out)

    def test_nested_link_and_non_directory_are_unsafe(self):
        # Mirrors tree_is_safe in install.sh/uninstall.sh, which refuse both.
        h = self.add_skill("alpha")
        outside = self.root / "outside"
        outside.mkdir()
        self.make_dir_link(self.target / "alpha" / "link", outside)
        (self.target / "beta").write_text("a file, not a folder", encoding="utf-8")
        self.write_manifest(f"# format: 2\nalpha\t{h}\nbeta\t-\n")
        rc, out, _ = self.run_list()
        self.assertEqual(rc, 0)
        self.assertIn("[UNSAFE] alpha", out)
        self.assertIn("symlink/junction", out)
        self.assertIn("[UNSAFE] beta", out)
        self.assertIn("not a directory", out)
        self.assertNotIn("MISSING", out)
        self.assertIn("2 unsafe", out)

    def test_missing_manifest_returns_1(self):
        rc, out, _ = self.run_list()
        self.assertEqual(rc, 1)
        self.assertIn("No ml-ai-skills manifest", out)

    def test_unknown_format_returns_1(self):
        self.write_manifest("# format: 9\n")
        rc, _, err = self.run_list()
        self.assertEqual(rc, 1)
        self.assertIn("format", err)

    def test_linked_skill_dir_is_reported_unsafe_and_not_followed(self):
        outside = self.root / "outside"
        outside.mkdir()
        (outside / "SKILL.md").write_text(SKILL.format(slug="alpha", title="Alpha"), encoding="utf-8")
        link = self.target / "alpha"
        try:
            os.symlink(outside, link, target_is_directory=True)
        except OSError:
            if os.name != "nt":
                self.skipTest("cannot create directory symlinks here")
            import _winapi
            _winapi.CreateJunction(str(outside), str(link))
        try:
            self.write_manifest("# format: 2\nalpha\t-\n")
            rc, out, _ = self.run_list()
            self.assertEqual(rc, 0)
            self.assertIn("[UNSAFE] alpha", out)
        finally:
            try:
                os.unlink(link)
            except OSError:
                os.rmdir(link)


@unittest.skipUnless(BASH, "bash not found on PATH")
class TestAgreesWithInstaller(Base):
    def test_hashes_written_by_install_sh_verify_in_python(self):
        src = self.root / "src"
        (src / "schema").mkdir(parents=True)
        (src / "INDEX.md").write_text("", encoding="utf-8")
        (src / "schema" / "allowed_values.py").write_text("", encoding="utf-8")
        for slug in ("alpha", "beta"):
            (src / slug).mkdir()
            # CRLF content: hashing must be byte-exact on both sides (no text-mode translation).
            (src / slug / "SKILL.md").write_bytes(SKILL.format(slug=slug, title=slug).replace("\n", "\r\n").encode())
        env = dict(os.environ, HOME=str(self.root / "home"))
        r = subprocess.run([BASH, str(REPO_ROOT / "install.sh"), "--source", str(src), "--target", str(self.target)],
                           capture_output=True, text=True, encoding="utf-8", errors="replace", env=env)
        self.assertEqual(r.returncode, 0, r.stderr)
        m = ls.read_manifest(self.target / ls.MANIFEST_NAME)
        self.assertEqual(m.format, 2)
        self.assertEqual(set(m.entries), {"alpha", "beta"})
        for slug, recorded in m.entries.items():
            self.assertEqual(ls.skill_status(self.target, slug, recorded), "ok", slug)

    def test_python_and_bash_agree_on_unicode_whitespace_slugs(self):
        h = self.add_skill("alpha")
        h_beta = self.add_skill("beta")
        self.write_manifest(f"# format: 2\n\u00a0alpha\t{h}\nbeta\t{h_beta}\n")
        python_view = set(ls.read_manifest(self.target / ls.MANIFEST_NAME).entries)
        r = subprocess.run([BASH, str(REPO_ROOT / "uninstall.sh"), "--target", str(self.target), "--dry-run"],
                           capture_output=True, text=True, encoding="utf-8", errors="replace",
                           env=dict(os.environ, HOME=str(self.root / "home")))
        self.assertEqual(r.returncode, 0, r.stderr)
        bash_view = {ln.split()[1] for ln in r.stdout.splitlines() if ln.startswith("  remove  ")}
        self.assertEqual(python_view, {"beta"})
        self.assertEqual(bash_view, python_view)

    def test_python_and_bash_agree_on_unsafe_entries(self):
        h = self.add_skill("alpha")
        h_beta = self.add_skill("beta")
        h_gamma = self.add_skill("gamma")
        outside = self.root / "outside"
        outside.mkdir()
        self.make_dir_link(self.target / "alpha" / "link", outside)
        (self.target / "delta").write_text("x", encoding="utf-8")
        self.write_manifest(f"# format: 2\nalpha\t{h}\nbeta\t{h_beta}\ngamma\t{h_gamma}\ndelta\t-\n")
        m = ls.read_manifest(self.target / ls.MANIFEST_NAME)
        python_unsafe = {s for s, rec in m.entries.items() if ls.skill_status(self.target, s, rec) == "unsafe"}
        r = subprocess.run([BASH, str(REPO_ROOT / "uninstall.sh"), "--target", str(self.target), "--dry-run"],
                           capture_output=True, text=True, encoding="utf-8", errors="replace",
                           env=dict(os.environ, HOME=str(self.root / "home")))
        self.assertEqual(r.returncode, 1, r.stderr)
        bash_refused = {ln.split()[2] for ln in r.stderr.splitlines() if ln.startswith("WARN:   refuse  ")}
        self.assertEqual(python_unsafe, {"alpha", "delta"})
        self.assertEqual(bash_refused, python_unsafe)

    def test_python_and_bash_both_refuse_nul_manifest(self):
        self.add_skill("alpha")
        (self.target / ls.MANIFEST_NAME).write_bytes(b"al\x00pha\n")
        with self.assertRaises(ls.ManifestError):
            ls.read_manifest(self.target / ls.MANIFEST_NAME)
        r = subprocess.run([BASH, str(REPO_ROOT / "uninstall.sh"), "--target", str(self.target), "--dry-run",
                            "--force"], capture_output=True, text=True, encoding="utf-8", errors="replace",
                           env=dict(os.environ, HOME=str(self.root / "home")))
        self.assertEqual(r.returncode, 1, r.stdout)
        self.assertIn("NUL", r.stderr)
        self.assertNotIn("remove  alpha", r.stdout)


class TestCli(unittest.TestCase):
    def test_non_ascii_output_on_a_legacy_codepage_does_not_crash(self):
        # Git Bash pipes/CI logs on Windows give Python a cp1252 stdout; a target
        # path or display_name outside it used to raise UnicodeEncodeError (rc 1).
        with tempfile.TemporaryDirectory() as d:
            target = Path(d) / "\u65e5\u672c skills"
            (target / "alpha").mkdir(parents=True)
            (target / "alpha" / "SKILL.md").write_text(
                SKILL.format(slug="alpha", title="F\u00fcnf \u2192 \u65e5\u672c"), encoding="utf-8")
            (target / ls.MANIFEST_NAME).write_text("alpha\n", encoding="utf-8")
            env = {k: v for k, v in os.environ.items() if k not in ("PYTHONUTF8",)}
            env["PYTHONIOENCODING"] = "cp1252"
            r = subprocess.run([sys.executable, str(REPO_ROOT / "scripts" / "list_skills.py"), "--target", str(target)],
                               capture_output=True, env=env)
            self.assertEqual(r.returncode, 0, r.stderr.decode("utf-8", "replace"))
            self.assertIn(b"alpha", r.stdout)
            self.assertIn(b"1 skill(s) installed", r.stdout)

    def test_target_flag_via_subprocess(self):
        with tempfile.TemporaryDirectory() as d:
            r = subprocess.run([sys.executable, str(REPO_ROOT / "scripts" / "list_skills.py"), "--target", d],
                               capture_output=True, text=True, encoding="utf-8", errors="replace")
            self.assertEqual(r.returncode, 1)
            self.assertIn("No ml-ai-skills manifest", r.stdout)


if __name__ == "__main__":
    unittest.main()
