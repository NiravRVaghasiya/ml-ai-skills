"""Tests for scripts/build_release.py - the reproducible release tarball."""
from __future__ import annotations

import contextlib
import hashlib
import io
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from testutil import REPO_ROOT

sys.path.insert(0, str(REPO_ROOT / "scripts"))
import build_release as br  # noqa: E402

EPOCH = 1_700_000_000
BASH = shutil.which("bash")

FIXTURE = {
    "VERSION": b"2.3.4\n",
    "LICENSE": b"MIT\n",
    "README.md": b"# readme\n",
    "CHANGELOG.md": b"# Changelog\n\n## [2.3.4]\n",
    "INDEX.md": b"# index\n",
    "install.sh": b"#!/usr/bin/env bash\necho install\n",
    "uninstall.sh": b"#!/usr/bin/env bash\necho uninstall\n",
    "bin/ml-ai-skills": b"#!/usr/bin/env bash\necho cli\n",
    "alpha-skill/SKILL.md": b"---\nname: alpha-skill\n---\n# Alpha\n",
    "beta-skill/SKILL.md": b"---\nname: beta-skill\n---\n# Beta\n",
    "plugins/all/.claude-plugin/plugin.json": b'{"name": "all"}\n',
    "plugins/all/skills/alpha-skill/SKILL.md": b"---\nname: alpha-skill\n---\n# Alpha\n",
    ".claude-plugin/marketplace.json": b'{"name": "m"}\n',
    "schema/allowed_values.py": b"DOMAIN = []\n",
    "scripts/tool.py": b"#!/usr/bin/env python3\nprint('x')\n",
    "docs/GUIDE.md": b"guide\n",
    # not shipped:
    "tests/test_x.py": b"x = 1\n",
    "evals/a/case.yaml": b"id: a\n",
    ".github/workflows/ci.yml": b"name: ci\n",
    "_TEMPLATE/SKILL.md": b"template\n",
    "requirements-dev.txt": b"ruff==0\n",
    "scripts/__pycache__/tool.cpython-313.pyc": b"\0\0",
    "scripts/sub/nested.py": b"x = 1\n",
}


def members(archive: Path) -> list[tarfile.TarInfo]:
    with tarfile.open(archive, "r:gz") as tar:
        return tar.getmembers()


class FixtureCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.root = self.tmp / "repo"
        for rel, data in FIXTURE.items():
            p = self.root / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_bytes(data)

    def tearDown(self):
        self._tmp.cleanup()

    def build(self, out: str = "dist", **kw) -> Path:
        tar_path, _ = br.build(self.root, self.tmp / out, epoch=kw.pop("epoch", EPOCH), **kw)
        return tar_path


class TestContent(FixtureCase):
    def test_ships_the_distribution_and_nothing_else(self):
        names = {m.name for m in members(self.build()) if m.isfile()}
        p = "ml-ai-skills-2.3.4/"
        expected = {p + r for r in FIXTURE if not r.startswith(("tests/", "evals/", ".github/", "_TEMPLATE/",
                                                                  "requirements", "scripts/__pycache__",
                                                                  "scripts/sub/"))}
        expected.add(p + br.MANIFEST_NAME)
        self.assertEqual(names, expected)

    def test_manifest_lists_every_other_file_with_its_sha256(self):
        with tarfile.open(self.build(), "r:gz") as tar:
            prefix = "ml-ai-skills-2.3.4/"
            manifest = tar.extractfile(prefix + br.MANIFEST_NAME).read().decode()
            listed = {}
            for line in manifest.splitlines():
                digest, rel = line.split("  ", 1)
                listed[rel] = digest
            files = {m.name[len(prefix):]: m for m in tar.getmembers() if m.isfile()}
            files.pop(br.MANIFEST_NAME)
            self.assertEqual(set(listed), set(files))
            for rel in files:
                self.assertEqual(listed[rel], hashlib.sha256(tar.extractfile(prefix + rel).read()).hexdigest())

    def test_sha256sums_matches_the_archive(self):
        tar_path = self.build()
        line = (tar_path.parent / "SHA256SUMS").read_text(encoding="ascii")
        self.assertEqual(line, f"{hashlib.sha256(tar_path.read_bytes()).hexdigest()}  {tar_path.name}\n")

    def test_missing_required_file_is_an_error(self):
        (self.root / "LICENSE").unlink()
        with self.assertRaisesRegex(br.ReleaseError, "LICENSE"):
            self.build()

    def test_crlf_text_file_is_an_error(self):
        (self.root / "README.md").write_bytes(b"# readme\r\n")
        with self.assertRaisesRegex(br.ReleaseError, "README.md: CRLF"):
            self.build()

    def test_version_must_match_the_tag(self):
        self.assertTrue(self.build(expect_version="v2.3.4").exists())
        with self.assertRaisesRegex(br.ReleaseError, "VERSION is 2.3.4"):
            self.build(expect_version="v2.3.5")

    def test_non_semver_version_is_an_error(self):
        (self.root / "VERSION").write_bytes(b"latest\n")
        with self.assertRaises(br.ReleaseError):
            self.build()


class TestReproducibility(FixtureCase):
    def test_two_builds_are_byte_identical(self):
        a, b = self.build("a"), self.build("b")
        self.assertEqual(a.read_bytes(), b.read_bytes())
        self.assertEqual((a.parent / "SHA256SUMS").read_bytes(), (b.parent / "SHA256SUMS").read_bytes())

    def test_file_mtimes_and_permissions_do_not_leak_into_the_archive(self):
        first = self.build("a").read_bytes()
        for i, p in enumerate(sorted(self.root.rglob("*"))):
            if p.is_file():
                os.utime(p, (EPOCH + 10_000 * i, EPOCH + 10_000 * i))
                with contextlib.suppress(OSError):
                    os.chmod(p, 0o600 if i % 2 else 0o755)
        self.assertEqual(self.build("b").read_bytes(), first)

    def test_normalized_metadata(self):
        ms = members(self.build())
        names = [m.name for m in ms]
        self.assertEqual(names, sorted(names))
        self.assertEqual(names[0], "ml-ai-skills-2.3.4")
        for m in ms:
            self.assertEqual((m.mtime, m.uid, m.gid, m.uname, m.gname), (EPOCH, 0, 0, "", ""), m.name)
            self.assertIn(m.type, (tarfile.REGTYPE, tarfile.DIRTYPE), m.name)
            want = 0o755 if m.isdir() or m.name.endswith(("install.sh", "uninstall.sh", "bin/ml-ai-skills",
                                                            "scripts/tool.py")) else 0o644
            self.assertEqual(m.mode, want, m.name)

    def test_gzip_header_carries_the_epoch_and_no_file_name(self):
        raw = self.build().read_bytes()
        self.assertEqual(raw[:3], b"\x1f\x8b\x08")
        self.assertEqual(raw[3] & 0x08, 0, "FNAME flag set: the output file name leaked into the gzip header")
        self.assertEqual(int.from_bytes(raw[4:8], "little"), EPOCH)

    def test_epoch_changes_the_archive(self):
        self.assertNotEqual(self.build("a").read_bytes(), self.build("b", epoch=EPOCH + 1).read_bytes())

    def test_source_date_epoch_env_is_used_and_validated(self):
        with mock.patch.dict(os.environ, {"SOURCE_DATE_EPOCH": "1234"}):
            self.assertEqual(br.source_date_epoch(self.root, None), 1234)
        with mock.patch.dict(os.environ, {"SOURCE_DATE_EPOCH": "yesterday"}):
            with self.assertRaises(br.EnvError):
                br.source_date_epoch(self.root, None)
        env = {k: v for k, v in os.environ.items() if k != "SOURCE_DATE_EPOCH"}
        with mock.patch.dict(os.environ, env, clear=True):
            with self.assertRaises(br.EnvError):  # fixture is not a git repo: no commit time either
                br.source_date_epoch(self.root, None)


class TestRealRepository(unittest.TestCase):
    """Builds the real distribution twice. A local checkout made before .gitattributes
    existed can hold CRLF copies of LF-committed files; that is reported as a skip with
    the reason (a fresh checkout, as in CI, is LF)."""

    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory()
        cls.tmp = Path(cls._tmp.name)
        try:
            cls.a, _ = br.build(REPO_ROOT, cls.tmp / "a", epoch=EPOCH)
            cls.b, _ = br.build(REPO_ROOT, cls.tmp / "b", epoch=EPOCH)
        except br.ReleaseError as e:
            cls._tmp.cleanup()
            if "CRLF" in str(e):
                raise unittest.SkipTest(f"working tree has CRLF in a shipped file ({e})") from e
            raise

    @classmethod
    def tearDownClass(cls):
        cls._tmp.cleanup()

    def test_two_builds_of_the_repo_are_byte_identical(self):
        self.assertEqual(self.a.read_bytes(), self.b.read_bytes())

    def test_every_skill_and_plugin_file_ships(self):
        names = {m.name.split("/", 1)[1] for m in members(self.a) if m.isfile()}
        for d in br.sl.iter_skill_dirs(REPO_ROOT):
            self.assertIn(f"{d.name}/SKILL.md", names)
        for p in (REPO_ROOT / "plugins").rglob("*"):
            if p.is_file():
                self.assertIn(p.relative_to(REPO_ROOT).as_posix(), names)
        self.assertFalse(any(n.startswith(("tests/", "evals/", ".github/")) for n in names))

    def test_extracted_archive_is_self_sufficient(self):
        out = self.tmp / "x"
        with tarfile.open(self.a, "r:gz") as tar:
            if hasattr(tarfile, "data_filter"):  # 3.12+, and 3.10.12+/3.11.4+ security backports
                tar.extractall(out, filter="data")
            else:
                tar.extractall(out)  # noqa: S202 - our own freshly built archive
        tree = next(out.iterdir())
        proc = subprocess.run([sys.executable, str(tree / "scripts" / "validate_skills.py"), "--root", str(tree)],
                              capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=300)
        self.assertEqual(proc.returncode, 0, proc.stdout[-2000:] + proc.stderr[-2000:])
        if BASH:
            target = self.tmp / "installed"
            proc = subprocess.run([BASH, str(tree / "install.sh"), "--target", str(target)],
                                  capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=300)
            self.assertEqual(proc.returncode, 0, proc.stdout[-2000:] + proc.stderr[-2000:])
            installed = {p.parent.name for p in target.glob("*/SKILL.md")}
            self.assertEqual(installed, {d.name for d in br.sl.iter_skill_dirs(REPO_ROOT)})


class TestCli(FixtureCase):
    def run_main(self, *args: str) -> tuple[int, str]:
        err = io.StringIO()
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(err):
            code = br.main(["--root", str(self.root), *args])
        return code, err.getvalue()

    def test_exit_codes(self):
        self.assertEqual(self.run_main("--out-dir", str(self.tmp / "o"), "--source-date-epoch", "5")[0], 0)
        self.assertEqual(self.run_main("--out-dir", str(self.tmp / "o"), "--source-date-epoch", "5",
                                       "--expect-version", "9.9.9")[0], 1)
        code, err = self.run_main("--out-dir", str(self.tmp / "o"), "--source-date-epoch", "5", "--require-clean")
        self.assertEqual(code, 2, err)  # fixture is not a git checkout


if __name__ == "__main__":
    unittest.main()
