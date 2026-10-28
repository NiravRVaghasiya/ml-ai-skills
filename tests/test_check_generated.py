"""Tests for scripts/check_generated.py - the generated-artifact drift gate.

The drift logic is exercised against throwaway fixture repos with tiny fake
generators (fast, deterministic); one test pins the real GENERATORS table to the
repo's actual write-mode scripts so a new generator cannot be forgotten. The full
real-repo run is the CI `generated` job (about a minute on Windows).
"""
from __future__ import annotations

import contextlib
import io
import re
import shutil
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

from testutil import REPO_ROOT

sys.path.insert(0, str(REPO_ROOT / "scripts"))
import check_generated as cg  # noqa: E402

GIT = shutil.which("git")

# Fake generator: out/result.txt = upper-cased src.txt, always written with LF.
UPPER = textwrap.dedent("""\
    import sys
    from pathlib import Path
    root = Path(__file__).resolve().parent.parent
    text = (root / "src.txt").read_text(encoding="utf-8")
    (root / "out").mkdir(exist_ok=True)
    for stale in (root / "out").glob("stale-*"):
        stale.unlink()
    (root / "out" / "result.txt").write_bytes(text.upper().encode("utf-8"))
    if "--also-other" in sys.argv:
        (root / "other.txt").write_bytes(b"surprise\\n")
    if "--append" in sys.argv:
        with open(root / "out" / "log.txt", "ab") as f:
            f.write(b"run\\n")
    if "--fail" in sys.argv:
        print("boom", file=sys.stderr)
        sys.exit(3)
""")


def gen(*args: str, outputs: tuple[str, ...] = ("out/*",)) -> cg.Generator:
    return cg.Generator("scripts/upper.py", ("--write", *args), outputs)


class FixtureRepo(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name) / "repo"
        (self.root / "scripts").mkdir(parents=True)
        (self.root / "scripts" / "upper.py").write_bytes(UPPER.encode("utf-8"))
        (self.root / "src.txt").write_bytes(b"hello\nworld\n")
        (self.root / "out").mkdir()
        (self.root / "out" / "result.txt").write_bytes(b"HELLO\nWORLD\n")

    def tearDown(self):
        self._tmp.cleanup()

    def tree(self) -> dict[str, bytes]:
        return {p.relative_to(self.root).as_posix(): p.read_bytes()
                for p in sorted(self.root.rglob("*")) if p.is_file() and ".git" not in p.parts}


class TestCopyCheck(FixtureRepo):
    def test_clean_repo_passes(self):
        report = cg.check_copy(self.root, (gen(),))
        self.assertTrue(report.ok, report)

    def test_stale_output_is_drift_attributed_to_its_generator(self):
        (self.root / "out" / "result.txt").write_bytes(b"HELLO\n")
        report = cg.check_copy(self.root, (gen(),))
        self.assertEqual(report.drift, {"out/result.txt": "modified"})
        self.assertEqual(report.attributed["out/result.txt"], "scripts/upper.py --write")
        self.assertFalse(report.ok)

    def test_crlf_in_a_generated_file_fails(self):
        (self.root / "out" / "result.txt").write_bytes(b"HELLO\r\nWORLD\r\n")
        report = cg.check_copy(self.root, (gen(),))
        self.assertEqual(report.drift, {"out/result.txt": "modified"})
        self.assertTrue(any("CRLF" in p for p in report.problems), report.problems)

    def test_missing_and_stale_extra_outputs_are_drift(self):
        (self.root / "out" / "result.txt").unlink()
        (self.root / "out" / "stale-old.txt").write_bytes(b"x\n")
        report = cg.check_copy(self.root, (gen(),))
        self.assertEqual(report.drift, {"out/result.txt": "created", "out/stale-old.txt": "deleted"})

    def test_write_outside_declared_outputs_is_reported(self):
        # Detected through content changes: a byte-identical rewrite of an undeclared file is invisible.
        report = cg.check_copy(self.root, (gen("--also-other"),))
        self.assertEqual(report.drift, {"other.txt": "created"})
        self.assertTrue(any("not one of its declared outputs" in p and "other.txt" in p for p in report.problems),
                        report.problems)

    def test_non_idempotent_generator_is_reported(self):
        (self.root / "out" / "log.txt").write_bytes(b"run\n")
        report = cg.check_copy(self.root, (gen("--append"),))
        self.assertTrue(any(p.startswith("not idempotent") and "out/log.txt" in p for p in report.problems),
                        report.problems)

    def test_failing_generator_is_reported_with_its_output(self):
        report = cg.check_copy(self.root, (gen("--fail"),))
        self.assertTrue(any("exited 3" in p and "boom" in p for p in report.problems), report.problems)

    def test_missing_generator_script_is_reported(self):
        report = cg.check_copy(self.root, (cg.Generator("scripts/nope.py", ("--write",), ("x",)),))
        self.assertTrue(any("generator not found" in p for p in report.problems))

    def test_working_tree_is_never_modified(self):
        (self.root / "out" / "result.txt").write_bytes(b"STALE\r\n")
        before = self.tree()
        cg.check_copy(self.root, (gen("--also-other", "--append"),))
        self.assertEqual(self.tree(), before)

    def test_caches_are_not_copied_or_compared(self):
        (self.root / "scripts" / "__pycache__").mkdir()
        (self.root / "scripts" / "__pycache__" / "upper.cpython-313.pyc").write_bytes(b"\0")
        self.assertNotIn("scripts/__pycache__/upper.cpython-313.pyc", cg.list_source_files(self.root))
        self.assertTrue(cg.check_copy(self.root, (gen(),)).ok)


@unittest.skipUnless(GIT, "git not on PATH")
class TestInPlace(FixtureRepo):
    def git(self, *args: str) -> None:
        subprocess.run([GIT, "-C", str(self.root), "-c", "user.name=t", "-c", "user.email=t@example.invalid",
                        "-c", "commit.gpgsign=false", *args], check=True, capture_output=True)

    def setUp(self):
        super().setUp()
        self.git("init", "-q")
        self.git("config", "core.autocrlf", "false")
        self.git("add", "-A")
        self.git("commit", "-q", "-m", "init")

    def in_place(self, *gens: cg.Generator) -> int:
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            return cg.check_in_place(self.root, gens)

    def test_clean_and_up_to_date_passes(self):
        self.assertEqual(self.in_place(gen()), 0)

    def test_stale_committed_output_fails(self):
        (self.root / "out" / "result.txt").write_bytes(b"OLD\n")
        self.git("commit", "-q", "-am", "stale")
        self.assertEqual(self.in_place(gen()), 1)

    def test_new_untracked_output_fails(self):
        self.assertEqual(self.in_place(gen("--append")), 1)

    def test_refuses_dirty_tree(self):
        (self.root / "src.txt").write_bytes(b"edited\n")
        self.assertEqual(self.in_place(gen()), 2)
        self.assertEqual((self.root / "out" / "result.txt").read_bytes(), b"HELLO\nWORLD\n")


class TestRealGeneratorTable(unittest.TestCase):
    def test_every_write_mode_script_is_registered(self):
        """Any scripts/*.py offering --write or --fix regenerates a committed artifact
        and must be in GENERATORS (eval_router's --write-baseline is a different flag)."""
        found = set()
        for path in sorted((REPO_ROOT / "scripts").glob("*.py")):
            src = path.read_text(encoding="utf-8")
            if re.search(r"add_argument\(\s*[\"']--(write|fix)[\"']", src):
                found.add(f"scripts/{path.name}")
        found.discard("scripts/check_generated.py")
        self.assertEqual(found, {g.script for g in cg.GENERATORS})

    def test_declared_outputs_exist(self):
        for g in cg.GENERATORS:
            for pat in g.outputs:
                hits = [p for p in REPO_ROOT.glob(pat.replace("/*", "/**/*"))] if "*" in pat else [REPO_ROOT / pat]
                self.assertTrue(any(h.exists() for h in hits), f"{g.label}: declared output {pat} matches nothing")


class TestGeneratorsWriteLF(unittest.TestCase):
    """generate_index.py --fix and generate_readme_stats.py --write pass newline="\\n" to
    write_text. Without it, Windows translates to CRLF and the drift gate (and the
    .gitattributes eol=lf policy) would flag the file. On POSIX this passes either way;
    it discriminates on Windows, where these generators are also run by maintainers."""

    def test_generate_index_fix_writes_lf(self):
        import generate_index as gi
        import skills_lib as sl
        with tempfile.TemporaryDirectory() as tmp:
            index = Path(tmp, "INDEX.md")
            index.write_bytes((REPO_ROOT / "INDEX.md").read_bytes().replace(b"\r\n", b"\n"))
            gi.apply_fix(sl.load_all_skills(), index)
            self.assertNotIn(b"\r\n", index.read_bytes())

    def test_generate_readme_stats_write_writes_lf(self):
        from unittest import mock

        import generate_readme_stats as gr
        with tempfile.TemporaryDirectory() as tmp:
            readme = Path(tmp, "README.md")
            readme.write_bytes((REPO_ROOT / "README.md").read_bytes().replace(b"\r\n", b"\n"))
            with mock.patch.object(gr, "README", readme), mock.patch.object(sys, "argv", ["x", "--write"]), \
                    contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(gr.main(), 0)
            self.assertNotIn(b"\r\n", readme.read_bytes())


if __name__ == "__main__":
    unittest.main()
