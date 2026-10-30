"""Line endings and executable bits survive a Windows checkout (docs/KNOWN-ISSUES.md #13).

- The current .gitattributes (working-tree copy, committed or not) is copied into a temp
  repository and checked with `git check-attr` and a `core.autocrlf=true` clone there.
- A fresh `core.autocrlf=true` clone of this repository's committed HEAD (uncommitted
  edits are not in it) must check out every text file with LF, and scripts/build_release.py
  must accept that checkout with --require-clean.
- Every tracked file that starts with "#!" must have git mode 100755 in the index, and
  every 100755 file a shebang, so a Linux/macOS clone can run it and the release
  archive's content-derived 0755 mode matches git.
All git commands run with the system and global config disabled and bounded timeouts.
"""
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
import build_release as br  # noqa: E402

GIT = shutil.which("git")
IN_CHECKOUT = bool(GIT) and br._is_repo_root(REPO_ROOT)  # False when run from an extracted release archive
EPOCH = 1_700_000_000
TEXT_SAMPLES = ("install.sh", "scripts/x.py", "docs/X.md", ".github/workflows/ci.yml", "evals/a/case.yaml",
                "docs/claims.json", "ruff.toml", "requirements-dev.txt", "bin/ml-ai-skills", "VERSION", "LICENSE",
                ".gitattributes", ".gitignore", ".shellcheckrc", "RELEASE-MANIFEST.sha256", "Makefile.unknown")
BINARY_SAMPLES = ("docs/figure.png", "assets/photo.jpg", "model.pt", "data.parquet", "release.tar.gz")


def git(*args: str, cwd: Path | None = None, check: bool = True, **kw) -> subprocess.CompletedProcess:
    env = {**os.environ, "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull, "GIT_TERMINAL_PROMPT": "0"}
    cmd = [GIT, "-c", "user.name=t", "-c", "user.email=t@example.invalid", "-c", "commit.gpgsign=false",
           "-c", "core.quotepath=off", *args]
    return subprocess.run(cmd, cwd=cwd, capture_output=True, timeout=kw.pop("timeout", 120), env=env, check=check,
                          **kw)


def crlf_text_files(root: Path, names: list[str]) -> list[str]:
    bad = []
    for n in names:
        data = (root / n).read_bytes()
        if b"\0" not in data and b"\r\n" in data:
            bad.append(n)
    return bad


@unittest.skipUnless(GIT, "git is not installed")
class TestCurrentGitattributes(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.repo = self.tmp / "repo"
        self.repo.mkdir()
        git("init", "-q", cwd=self.repo)
        shutil.copyfile(REPO_ROOT / ".gitattributes", self.repo / ".gitattributes")

    def tearDown(self):
        self._tmp.cleanup()

    def attrs(self, path: str) -> dict[str, str]:
        out = git("check-attr", "text", "eol", "binary", "diff", "merge", "--", path, cwd=self.repo,
                  text=True).stdout
        return {line.split(": ")[1]: line.split(": ")[2] for line in out.splitlines()}

    def test_text_types_are_lf(self):
        for path in TEXT_SAMPLES:
            with self.subTest(path=path):
                a = self.attrs(path)
                self.assertEqual(a["eol"], "lf", a)
                self.assertIn(a["text"], ("set", "auto"), a)
        for path in TEXT_SAMPLES[:-1]:  # every named type is explicitly text, not left to auto-detection
            with self.subTest(explicit=path):
                self.assertEqual(self.attrs(path)["text"], "set")

    def test_binary_types_are_never_converted(self):
        for path in BINARY_SAMPLES:
            with self.subTest(path=path):
                a = self.attrs(path)
                self.assertEqual((a["text"], a["diff"], a["merge"]), ("unset", "unset", "unset"), a)

    def test_autocrlf_true_clone_checks_out_lf_and_keeps_binary_bytes(self):
        files = {"install.sh": b"#!/usr/bin/env bash\necho hi\n", "docs/X.md": b"# x\n\ntext\n",
                 "a/b.yaml": b"k: v\n", "bin/ml-ai-skills": b"#!/usr/bin/env bash\n", "x.json": b"{}\n",
                 "img.png": b"\x89PNG\r\n\x1a\n\0\0\r\n"}
        for rel, data in files.items():
            (self.repo / rel).parent.mkdir(parents=True, exist_ok=True)
            (self.repo / rel).write_bytes(data)
        git("-c", "core.autocrlf=false", "add", "-A", cwd=self.repo)
        git("-c", "core.autocrlf=false", "commit", "-q", "-m", "fixture", cwd=self.repo)
        clone = self.tmp / "clone"
        git("-c", "core.autocrlf=true", "clone", "-q", "--config", "core.autocrlf=true", str(self.repo), str(clone))
        for rel, data in files.items():
            with self.subTest(rel=rel):
                self.assertEqual((clone / rel).read_bytes(), data)


@unittest.skipUnless(IN_CHECKOUT, "needs git and a git checkout of this repository (not a release archive)")
class TestFreshAutocrlfCloneOfHead(unittest.TestCase):
    """Reflects the committed HEAD, not uncommitted working-tree edits."""

    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory()
        cls.tmp = Path(cls._tmp.name)
        cls.clone = cls.tmp / "clone"
        git("-c", "core.autocrlf=true", "clone", "-q", "--no-hardlinks", "--config", "core.autocrlf=true",
            str(REPO_ROOT), str(cls.clone), timeout=600)
        cls.head = git("rev-parse", "HEAD", cwd=cls.clone, text=True).stdout.strip()
        out = git("ls-files", "-z", cwd=cls.clone).stdout.decode("utf-8")
        cls.files = [n for n in out.split("\0") if n]

    @classmethod
    def tearDownClass(cls):
        cls._tmp.cleanup()

    def test_every_text_file_is_lf(self):
        self.assertEqual(crlf_text_files(self.clone, self.files), [], f"CRLF in a fresh clone of {self.head}")

    def test_build_release_accepts_the_checkout(self):
        tar_path, sums = br.build(self.clone, self.tmp / "dist", epoch=EPOCH, require_clean=True)
        self.assertIn("every SHA-256", br.verify_archive(tar_path, sums))


@unittest.skipUnless(IN_CHECKOUT, "needs git and a git checkout of this repository (not a release archive)")
class TestExecutableBits(unittest.TestCase):
    def test_shebang_files_are_100755_in_the_index_and_only_they(self):
        out = git("-C", str(REPO_ROOT), "ls-files", "-z", "--stage").stdout.decode("utf-8")
        wrong = []
        for entry in out.split("\0"):
            if not entry:
                continue
            meta, name = entry.split("\t", 1)
            mode = meta.split(" ", 1)[0]
            path = REPO_ROOT / name
            if not path.is_file():
                continue
            with open(path, "rb") as fh:
                shebang = fh.read(2) == b"#!"
            if shebang != (mode == "100755"):
                wrong.append(f"{name} ({mode}, {'shebang' if shebang else 'no shebang'})")
        self.assertEqual(wrong, [], "fix with: git update-index --chmod=+x <file> (or --chmod=-x)")

    def test_installer_entry_points_have_a_shebang(self):
        for rel in ("install.sh", "uninstall.sh", "bin/ml-ai-skills"):
            with self.subTest(rel=rel):
                self.assertEqual((REPO_ROOT / rel).read_bytes().split(b"\n", 1)[0], b"#!/usr/bin/env bash")


if __name__ == "__main__":
    unittest.main()
