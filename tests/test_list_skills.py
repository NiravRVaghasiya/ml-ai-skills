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
SCRIPT_TIMEOUT = 300  # seconds per install.sh/uninstall.sh run (forks are slow on Windows)
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
            ls.read_manifest(self.write_manifest("# format: 4\nalpha\n"))

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
                           capture_output=True, text=True, encoding="utf-8", errors="replace", env=env,
                           timeout=SCRIPT_TIMEOUT)
        self.assertEqual(r.returncode, 0, r.stderr)
        m = ls.read_manifest(self.target / ls.MANIFEST_NAME)
        self.assertEqual(m.format, 3)
        self.assertEqual(set(m.entries), {"alpha", "beta"})
        self.assertEqual(set(m.files), {"alpha", "beta"}, "install.sh records every file")
        for slug, recorded in m.entries.items():
            self.assertEqual(ls.skill_status(self.target, slug, recorded, m.files[slug]), "ok", slug)

    def test_python_and_bash_agree_on_unicode_whitespace_slugs(self):
        h = self.add_skill("alpha")
        h_beta = self.add_skill("beta")
        self.write_manifest(f"# format: 2\n\u00a0alpha\t{h}\nbeta\t{h_beta}\n")
        python_view = set(ls.read_manifest(self.target / ls.MANIFEST_NAME).entries)
        r = subprocess.run([BASH, str(REPO_ROOT / "uninstall.sh"), "--target", str(self.target), "--dry-run"],
                           capture_output=True, text=True, encoding="utf-8", errors="replace",
                           env=dict(os.environ, HOME=str(self.root / "home")), timeout=SCRIPT_TIMEOUT)
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
                           env=dict(os.environ, HOME=str(self.root / "home")), timeout=SCRIPT_TIMEOUT)
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
                           env=dict(os.environ, HOME=str(self.root / "home")), timeout=SCRIPT_TIMEOUT)
        self.assertEqual(r.returncode, 1, r.stdout)
        self.assertIn("NUL", r.stderr)
        self.assertNotIn("remove  alpha", r.stdout)


@unittest.skipUnless(BASH, "bash not found on PATH")
class TestAgreesWithInstallerPerFile(Base):
    def test_python_and_bash_agree_on_which_files_changed(self):
        src = self.root / "src"
        (src / "schema").mkdir(parents=True)
        (src / "INDEX.md").write_text("", encoding="utf-8")
        (src / "schema" / "allowed_values.py").write_text("", encoding="utf-8")
        slugs = ("alpha", "beta", "gamma", "delta", "epsilon")
        for slug in slugs:
            (src / slug / "ref").mkdir(parents=True)
            (src / slug / "SKILL.md").write_text(SKILL.format(slug=slug, title=slug), encoding="utf-8")
            (src / slug / "ref" / "notes é.md").write_text("orig", encoding="utf-8")
        env = dict(os.environ, HOME=str(self.root / "home"))
        r = subprocess.run([BASH, str(REPO_ROOT / "install.sh"), "--source", str(src), "--target", str(self.target)],
                           capture_output=True, text=True, encoding="utf-8", errors="replace", env=env,
                           timeout=SCRIPT_TIMEOUT)
        self.assertEqual(r.returncode, 0, r.stderr)
        (self.target / "alpha" / "ref" / "notes é.md").write_text("edited", encoding="utf-8")
        (self.target / "beta" / "ref" / "notes é.md").unlink()
        (self.target / "gamma" / "ref" / "mine.md").write_text("new", encoding="utf-8")
        (self.target / "delta" / "ref").rename(self.target / "delta" / "moved")
        m = ls.read_manifest(self.target / ls.MANIFEST_NAME)
        python_view = {s: ls.skill_check(self.target, s, rec, m.files.get(s)) for s, rec in m.entries.items()}
        r = subprocess.run([BASH, str(REPO_ROOT / "uninstall.sh"), "--target", str(self.target), "--dry-run"],
                           capture_output=True, text=True, encoding="utf-8", errors="replace", env=env,
                           timeout=SCRIPT_TIMEOUT)
        self.assertEqual(r.returncode, 0, r.stderr)
        bash_kept = {ln.split()[2]: ln for ln in r.stderr.splitlines() if ln.startswith("WARN:   keep    ")}
        self.assertEqual(set(bash_kept), {s for s, (status, _) in python_view.items() if status != "ok"})
        self.assertEqual(set(bash_kept), {"alpha", "beta", "gamma", "delta"})
        for slug, (_, detail) in python_view.items():
            if slug in bash_kept:
                self.assertIn(f"({detail})", bash_kept[slug], "both name the same files the same way")
        self.assertIn("remove  epsilon", r.stdout)


class TestFormat3(Base):
    """Per-file records (manifest format 3, docs/INSTALLER.md "Manifest format")."""

    def files_of(self, slug: str) -> dict[str, str]:
        return ls.tree_files(self.target / slug)

    def v3(self, slugs, extra_lines: str = "") -> Path:
        lines = ["# format: 3"]
        for slug in slugs:
            files = self.files_of(slug)
            lines.append(f"{slug}\t{files['SKILL.md']}")
            lines += [f"{slug}\t{h}\t{rel}" for rel, h in sorted(files.items())]
        return self.write_manifest("\n".join(lines) + "\n" + extra_lines)

    def test_file_lines_are_parsed_per_skill(self):
        self.add_skill("alpha")
        (self.target / "alpha" / "ref").mkdir()
        (self.target / "alpha" / "ref" / "notes with space .md").write_text("n", encoding="utf-8")
        m = ls.read_manifest(self.v3(["alpha"]))
        self.assertEqual(m.format, 3)
        self.assertEqual(set(m.files["alpha"]), {"SKILL.md", "ref/notes with space .md"},
                         "a trailing space in a name survives, like in bash")
        self.assertEqual(m.warnings, [])
        self.assertEqual(ls.skill_check(self.target, "alpha", m.entries["alpha"], m.files["alpha"]), ("ok", ""))

    def test_any_file_edit_is_modified_and_named(self):
        for slug in ("alpha", "beta", "gamma", "delta"):
            self.add_skill(slug)
            (self.target / slug / "extra.md").write_text("orig", encoding="utf-8")
        self.v3(["alpha", "beta", "gamma", "delta"])
        (self.target / "alpha" / "extra.md").write_text("edited", encoding="utf-8")       # changed
        (self.target / "beta" / "extra.md").unlink()                                         # removed
        (self.target / "gamma" / "mine.md").write_text("new", encoding="utf-8")              # added
        (self.target / "delta" / "extra.md").rename(self.target / "delta" / "renamed.md")    # renamed
        m = ls.read_manifest(self.target / ls.MANIFEST_NAME)
        got = {s: ls.skill_check(self.target, s, m.entries[s], m.files[s]) for s in m.entries}
        self.assertEqual(got["alpha"], ("modified", "changed: extra.md"))
        self.assertEqual(got["beta"], ("modified", "removed: extra.md"))
        self.assertEqual(got["gamma"], ("modified", "added: mine.md"))
        self.assertEqual(got["delta"], ("modified", "added: renamed.md; removed: extra.md"))
        rc, out, _ = self.run_list()
        self.assertEqual(rc, 0)
        self.assertIn("4 modified", out)
        self.assertIn("(changed: extra.md)", out)

    def test_corrupt_file_lines_only_ever_make_a_skill_modified(self):
        h = self.add_skill("alpha")
        self.add_skill("beta")
        bad = "\n".join([
            f"alpha\t{h}\t../../victim/keep.txt",   # traversal: invalid relpath
            f"alpha\t{h}\t/abs",                     # absolute
            f"alpha\t{h}\ta//b",                     # empty component
            "alpha\tnothex\tSKILL.md",              # bad hash
            f"../x\t{h}\tSKILL.md",                  # bad slug
            f"ghost\t{h}\tSKILL.md",                 # no skill line
            f"beta\t{'0' * 64}\tSKILL.md",           # duplicate of beta's SKILL.md line (second one)
        ]) + "\n"
        m = ls.read_manifest(self.v3(["alpha", "beta"], bad))
        self.assertEqual(set(m.files["alpha"]), {"SKILL.md"})
        self.assertNotIn("ghost", m.entries)
        self.assertEqual(len(m.warnings), 7, m.warnings)
        self.assertEqual(ls.skill_status(self.target, "alpha", m.entries["alpha"], m.files["alpha"]), "ok")
        self.assertEqual(ls.skill_status(self.target, "beta", m.entries["beta"], m.files["beta"]), "ok",
                         "the first entry for a file wins")
        # A dropped (missing) file line: the file is now unrecorded -> modified.
        (self.target / "alpha" / "x.md").write_text("x", encoding="utf-8")
        m = ls.read_manifest(self.v3(["alpha"]))
        text = (self.target / ls.MANIFEST_NAME).read_text(encoding="utf-8")
        self.write_manifest("".join(ln + "\n" for ln in text.splitlines() if not ln.endswith("\tx.md")))
        m = ls.read_manifest(self.target / ls.MANIFEST_NAME)
        self.assertEqual(ls.skill_check(self.target, "alpha", m.entries["alpha"], m.files["alpha"]),
                         ("modified", "added: x.md"))

    def test_file_lines_in_a_format_2_manifest_are_ignored(self):
        h = self.add_skill("alpha")
        m = ls.read_manifest(self.write_manifest(f"# format: 2\nalpha\t{h}\nalpha\t{h}\tSKILL.md\n"))
        self.assertEqual(m.files, {})
        self.assertEqual(len(m.warnings), 1)

    def test_legacy_entry_with_unrecorded_files_is_unverified(self):
        # A format-2 entry records SKILL.md only; other files may be the user's.
        h = self.add_skill("alpha")
        self.write_manifest(f"# format: 2\nalpha\t{h}\n")
        self.assertEqual(ls.skill_check(self.target, "alpha", h), ("ok", ""))
        (self.target / "alpha" / "notes.md").write_text("mine", encoding="utf-8")
        self.assertEqual(ls.skill_check(self.target, "alpha", h), ("unverified", "notes.md"))
        _, out, _ = self.run_list()
        self.assertIn("UNVERIFIED — the manifest recorded only SKILL.md", out)
        self.assertIn("1 unverified", out)

    def test_control_character_in_a_name_is_unsafe(self):
        if os.name == "nt":
            self.skipTest("Windows file names cannot contain control characters")
        h = self.add_skill("alpha")
        (self.target / "alpha" / "a\nb").write_text("x", encoding="utf-8")
        self.write_manifest(f"# format: 2\nalpha\t{h}\n")
        self.assertEqual(ls.skill_status(self.target, "alpha", h), "unsafe")


class TestJunctionsBefore312(Base):
    """KNOWN-ISSUES #12: os.path.isjunction is 3.12+; older Pythons read the reparse tag."""

    def make_junction_with_mklink(self, link: Path, target: Path) -> None:
        if os.name != "nt":
            self.skipTest("NTFS junctions exist only on Windows")
        r = subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(target)], capture_output=True,
                           text=True, timeout=60)
        if r.returncode != 0:
            self.fail(f"mklink /J failed: {r.stdout}{r.stderr}")
        self._links.append(link)

    def test_mklink_junction_is_unsafe_on_this_python(self):
        outside = self.root / "outside"
        outside.mkdir()
        (outside / "SKILL.md").write_text(SKILL.format(slug="alpha", title="Alpha"), encoding="utf-8")
        self.make_junction_with_mklink(self.target / "alpha", outside)
        h = self.add_skill("beta")
        self.make_junction_with_mklink(self.target / "beta" / "escape", outside)
        self.write_manifest(f"# format: 2\nalpha\t-\nbeta\t{h}\n")
        rc, out, _ = self.run_list()
        self.assertEqual(rc, 0)
        self.assertIn("[UNSAFE] alpha", out)
        self.assertIn("[UNSAFE] beta", out)
        self.assertIn("2 unsafe", out)

    def test_fallback_without_os_path_isjunction(self):
        # Exercise the < 3.12 code path on any Python by hiding os.path.isjunction.
        outside = self.root / "outside"
        outside.mkdir()
        self.make_junction_with_mklink(self.target / "alpha", outside)
        saved = getattr(os.path, "isjunction", None)
        if saved is not None:
            del os.path.isjunction
        try:
            self.assertTrue(ls._is_junction(self.target / "alpha"))
            self.assertTrue(ls._is_link(self.target / "alpha"))
            self.assertFalse(ls._is_junction(outside))
            self.assertFalse(ls._is_junction(self.root / "does-not-exist"))
            self.assertEqual(ls.unsafe_reason(self.target, "alpha"), "path is a symlink/junction; not followed")
        finally:
            if saved is not None:
                os.path.isjunction = saved


class TestCli(unittest.TestCase):
    LIST = str(REPO_ROOT / "scripts" / "list_skills.py")

    def listing_env(self, **extra: str) -> dict[str, str]:
        env = {k: v for k, v in os.environ.items() if k not in ("PYTHONUTF8", "PYTHONIOENCODING")}
        env.update(extra)
        return env

    def non_ascii_target(self, d: str) -> Path:
        target = Path(d) / "日本 skills"
        (target / "alpha").mkdir(parents=True)
        (target / "alpha" / "SKILL.md").write_text(
            SKILL.format(slug="alpha", title="Fünf → 日本"), encoding="utf-8")
        (target / ls.MANIFEST_NAME).write_text("alpha\n", encoding="utf-8")
        return target

    def test_pipe_output_is_utf8_by_default(self):
        # KNOWN-ISSUES #30: a pipe/file gets UTF-8, so a cp1252 Windows default no longer
        # turns non-Latin paths into \\uXXXX escapes.
        with tempfile.TemporaryDirectory() as d:
            target = self.non_ascii_target(d)
            r = subprocess.run([sys.executable, self.LIST, "--target", str(target)], capture_output=True,
                               env=self.listing_env(), timeout=120)
            self.assertEqual(r.returncode, 0, r.stderr.decode("utf-8", "replace"))
            out = r.stdout.decode("utf-8")  # strict: must be valid UTF-8
            self.assertIn("日本 skills", out)
            self.assertIn("Fünf → 日本", out)
            self.assertNotIn("\\u", out)

    def test_explicit_pythonioencoding_is_honoured_with_escapes(self):
        with tempfile.TemporaryDirectory() as d:
            target = self.non_ascii_target(d)
            r = subprocess.run([sys.executable, self.LIST, "--target", str(target)], capture_output=True,
                               env=self.listing_env(PYTHONIOENCODING="cp1252"), timeout=120)
            self.assertEqual(r.returncode, 0, r.stderr.decode("utf-8", "replace"))
            out = r.stdout.decode("cp1252")
            self.assertIn("\\u65e5\\u672c skills", out)
            self.assertIn("Fünf \\u2192 \\u65e5\\u672c", out, "cp1252 keeps ü, escapes the rest")

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
                               capture_output=True, env=env, timeout=120)
            self.assertEqual(r.returncode, 0, r.stderr.decode("utf-8", "replace"))
            self.assertIn(b"alpha", r.stdout)
            self.assertIn(b"1 skill(s) installed", r.stdout)

    def test_target_flag_via_subprocess(self):
        with tempfile.TemporaryDirectory() as d:
            r = subprocess.run([sys.executable, str(REPO_ROOT / "scripts" / "list_skills.py"), "--target", d],
                               capture_output=True, text=True, encoding="utf-8", errors="replace",
                               timeout=120)
            self.assertEqual(r.returncode, 1)
            self.assertIn("No ml-ai-skills manifest", r.stdout)


if __name__ == "__main__":
    unittest.main()
