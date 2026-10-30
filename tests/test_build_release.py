"""Tests for scripts/build_release.py on fixture trees: what ships, determinism, atomic
output, the generated-artifact gate and archive verification / safe extraction.

The real repository is built in tests/test_release_archive.py; line endings and git
modes are checked in tests/test_release_gitattributes.py."""
from __future__ import annotations

import contextlib
import gzip
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
GIT = shutil.which("git")
PREFIX = "ml-ai-skills-2.3.4/"
OK_CHECK = b"import sys\nsys.exit(0)\n"

FIXTURE = {
    "VERSION": b"2.3.4\n",
    "LICENSE": b"MIT\n",
    "README.md": b"# readme\n",
    "CHANGELOG.md": b"# Changelog\n\n## [2.3.4]\n",
    "INDEX.md": b"# index\n",
    "CLAUDE.md": b"# operating manual\n",
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
    "scripts/check_generated.py": OK_CHECK,
    "scripts/sub/nested.py": b"x = 1\n",
    "docs/GUIDE.md": b"guide\n",
    "tests/testutil.py": b"x = 1\n",
    "tests/test_x.py": b"x = 1\n",
    "evals/a/case.yaml": b"id: a\n",
    "evals/routing/benchmark.json": b"{}\n",
    ".github/workflows/ci.yml": b"name: ci\n",
    "_TEMPLATE/SKILL.md": b"template\n",
    "requirements-dev.txt": b"ruff==0\n",
    "ruff.toml": b"line-length = 120\n",
    ".gitattributes": b"* text=auto eol=lf\n",
    ".gitignore": b"__pycache__/\n",
}
NOT_SHIPPED = {
    "scripts/__pycache__/tool.cpython-313.pyc": b"\0\0",
    ".claude/settings.local.json": b"{}\n",
    "validate_out.json": b"{}\n",
    "dist/ml-ai-skills-0.0.1.tar.gz": b"\x1f\x8b",
    "run.log": b"log\n",
    ".ruff_cache/x": b"x\n",
}


def members(archive: Path) -> list[tarfile.TarInfo]:
    with tarfile.open(archive, "r:gz") as tar:
        return tar.getmembers()


def write_tree(root: Path, files: dict[str, bytes]) -> None:
    for rel, data in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data)


def git(root: Path, *args: str) -> subprocess.CompletedProcess:
    env = {**os.environ, "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull}
    return subprocess.run([GIT, "-C", str(root), "-c", "user.name=t", "-c", "user.email=t@example.invalid",
                           "-c", "commit.gpgsign=false", "-c", "core.autocrlf=false", *args],
                          capture_output=True, text=True, timeout=60, env=env, check=True)


class FixtureCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.root = self.tmp / "repo"
        write_tree(self.root, FIXTURE)
        write_tree(self.root, NOT_SHIPPED)

    def tearDown(self):
        self._tmp.cleanup()

    def build(self, out: str = "dist-out", **kw) -> Path:
        tar_path, _ = br.build(self.root, self.tmp / out, epoch=kw.pop("epoch", EPOCH), **kw)
        return tar_path


class TestContent(FixtureCase):
    def test_ships_every_classified_file_and_nothing_excluded(self):
        names = {m.name for m in members(self.build()) if m.isfile()}
        self.assertEqual(names, {PREFIX + r for r in FIXTURE} | {PREFIX + br.MANIFEST_NAME})

    def test_unclassified_file_is_an_error_not_silently_dropped(self):
        (self.root / "notes.txt").write_bytes(b"scratch\n")
        with self.assertRaisesRegex(br.ReleaseError, r"neither shipped nor excluded.*notes\.txt"):
            self.build()
        (self.root / "notes.txt").unlink()
        (self.root / "newtool").mkdir()
        (self.root / "newtool" / "a.py").write_bytes(b"x = 1\n")
        with self.assertRaisesRegex(br.ReleaseError, r"newtool/a\.py"):
            self.build()

    def test_output_dir_inside_the_tree_is_not_shipped(self):
        first = self.build("out")  # self.tmp/out is outside the tree; now build into the tree itself
        tar_path, _ = br.build(self.root, self.root / "out", epoch=EPOCH)
        self.assertEqual(br.select_files(self.root, exclude_dir=self.root / "out"),
                         sorted(FIXTURE))
        tar2, _ = br.build(self.root, self.root / "out", epoch=EPOCH)  # rebuilding over its own output
        self.assertEqual(tar2.read_bytes(), first.read_bytes())
        self.assertEqual(tar_path, tar2)

    def test_manifest_lists_every_other_file_with_its_sha256(self):
        with tarfile.open(self.build(), "r:gz") as tar:
            manifest = tar.extractfile(PREFIX + br.MANIFEST_NAME).read().decode()
            listed = {}
            for line in manifest.splitlines():
                digest, rel = line.split("  ", 1)
                listed[rel] = digest
            files = {m.name[len(PREFIX):]: m for m in tar.getmembers() if m.isfile()}
            files.pop(br.MANIFEST_NAME)
            self.assertEqual(set(listed), set(files))
            self.assertEqual(list(listed), sorted(listed))
            for rel in files:
                self.assertEqual(listed[rel], hashlib.sha256(tar.extractfile(PREFIX + rel).read()).hexdigest())

    def test_sha256sums_matches_the_archive(self):
        tar_path = self.build()
        line = (tar_path.parent / "SHA256SUMS").read_text(encoding="ascii")
        self.assertEqual(line, f"{hashlib.sha256(tar_path.read_bytes()).hexdigest()}  {tar_path.name}\n")

    def test_missing_required_file_is_an_error(self):
        for rel in ("LICENSE", "tests/testutil.py", "evals/routing/benchmark.json"):
            with self.subTest(rel=rel):
                data = (self.root / rel).read_bytes()
                (self.root / rel).unlink()
                with self.assertRaisesRegex(br.ReleaseError, rel.replace(".", r"\.")):
                    self.build()
                (self.root / rel).write_bytes(data)

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

    def test_unsafe_path_names_are_refused(self):
        (self.root / "docs" / "with space.md").write_bytes(b"x\n")
        with self.assertRaisesRegex(br.ReleaseError, "unsafe or non-portable path"):
            self.build()

    def test_path_problem_rules(self):
        # Names that cannot be created portably are checked as strings (creating "nul" or
        # "a.md." on Windows would hit the device or lose the dot rather than make the file).
        for bad in ("docs/with space.md", "a.md:hidden", "docs/nul", "docs/NUL.txt", "Com1.md", "lpt9/x.md",
                    "aux.tar.gz", "docs/a.md.", "x/./y", "x/../y", "caf\u00e9.md", "back\\slash"):
            with self.subTest(bad=bad):
                self.assertIsNotNone(br.path_problem(bad))
        for good in ("docs/GUIDE.md", "nullable.md", "console/x.py", "com10.md", "lpt.md", ".github/ci.yml",
                     "ml-ai-skills-2.3.4", "a.b.c/d_e@f+g-h"):
            with self.subTest(good=good):
                self.assertIsNone(br.path_problem(good))

    def test_case_colliding_or_file_and_directory_paths_are_refused(self):
        # A case-sensitive checkout can hold both; checked as strings for the same reason.
        for files in (["README.md", "readme.md"], ["docs/a.md", "Docs/b.md"], ["a", "a/b"]):
            with self.subTest(files=files), self.assertRaisesRegex(br.ReleaseError, "conflicts with"):
                br._check_layout(files, [])
        br._check_layout(["docs/a.md", "docs/b.md", "docs/sub/c.md"], ["docs", "docs/sub"])

    def test_symlink_is_refused_not_followed(self):
        secret = self.tmp / "secret.txt"
        secret.write_bytes(b"outside the tree\n")
        try:
            os.symlink(secret, self.root / "docs" / "link.md")
        except (OSError, NotImplementedError) as e:
            self.skipTest(f"cannot create a symlink here ({e})")
        with self.assertRaisesRegex(br.ReleaseError, "docs/link.md: not a regular file"):
            self.build()

    def test_existing_manifest_in_a_git_checkout_is_an_error(self):
        if not GIT:
            self.skipTest("git is not installed")
        git(self.root, "init", "-q")
        (self.root / br.MANIFEST_NAME).write_bytes(b"stale\n")
        with self.assertRaisesRegex(br.ReleaseError, "must not exist in the repo"):
            self.build()


@unittest.skipUnless(GIT, "git is not installed")
class TestGitCheckout(FixtureCase):
    def setUp(self):
        super().setUp()
        (self.root / ".gitignore").write_bytes(b"__pycache__/\n.claude/\nvalidate_out.json\n*.log\n.ruff_cache/\n")
        git(self.root, "init", "-q")
        git(self.root, "add", "-A")
        git(self.root, "rm", "-r", "-q", "--cached", "dist")  # dist/ stays untracked but not ignored
        git(self.root, "commit", "-q", "-m", "fixture")

    def test_file_list_comes_from_git(self):
        (self.root / "docs" / "untracked.md").write_bytes(b"new\n")  # untracked, not ignored: ships
        rels = br.select_files(self.root)
        self.assertIn("docs/untracked.md", rels)
        self.assertNotIn(".claude/settings.local.json", rels)
        self.assertNotIn("dist/ml-ai-skills-0.0.1.tar.gz", rels)  # excluded by build_release, not .gitignore

    def test_tracked_file_matching_an_exclusion_is_an_error_not_dropped(self):
        for rel, add in (("docs/notes.bak", ("add",)), ("tests/fixtures/sample.log", ("add", "-f"))):
            with self.subTest(rel=rel):
                write_tree(self.root, {rel: b"tracked\n"})
                git(self.root, *add, rel)
                git(self.root, "commit", "-q", "-m", rel)
                with self.assertRaisesRegex(br.ReleaseError, f"{rel}: tracked by git but matches an EXCLUDED"):
                    br.select_files(self.root)
                git(self.root, "rm", "-q", rel)
                git(self.root, "commit", "-q", "-m", "drop " + rel)
        (self.root / "docs" / "notes.bak").write_bytes(b"untracked\n")  # untracked: excluded, as before
        self.assertNotIn("docs/notes.bak", br.select_files(self.root))

    def test_require_clean(self):
        shutil.rmtree(self.root / "dist")  # the untracked build output would make the tree dirty
        self.assertTrue(self.build(require_clean=True).exists())
        (self.root / "docs" / "GUIDE.md").write_bytes(b"edited\n")
        with self.assertRaisesRegex(br.ReleaseError, "not clean"):
            self.build("again", require_clean=True)

    def test_epoch_defaults_to_the_head_commit_time(self):
        env = {k: v for k, v in os.environ.items() if k != "SOURCE_DATE_EPOCH"}
        with mock.patch.dict(os.environ, env, clear=True):
            want = int(git(self.root, "log", "-1", "--format=%ct").stdout.strip())
            self.assertEqual(br.source_date_epoch(self.root, None), want)

    def test_tracked_symlink_is_refused(self):
        blob = subprocess.run([GIT, "-C", str(self.root), "hash-object", "-w", "--stdin"], input="../outside",
                              capture_output=True, text=True, timeout=60, check=True).stdout.strip()
        git(self.root, "update-index", "--add", "--cacheinfo", f"120000,{blob},docs/link.md")
        with self.assertRaisesRegex(br.ReleaseError, "docs/link.md: git mode 120000"):
            self.build()


class TestReproducibility(FixtureCase):
    # sha256 of the uncompressed tar stream of FIXTURE at EPOCH. It is expected to depend
    # only on the file bytes, the version and the epoch (zlib is not involved), so a change
    # here most likely means the archive format changed. Observed identical with CPython
    # 3.10.22, 3.12.10 and 3.13.2 on one Windows machine; other interpreters and operating
    # systems are not measured (docs/REPRODUCIBILITY.md, "Release archive").
    FIXTURE_TAR_SHA256 = "1f8e41514fa7671e89b36e30a6f47ee252cdcda461cdd878f9d1bfbbdc1630bf"

    def test_two_builds_are_byte_identical(self):
        a, b = self.build("a"), self.build("b")
        self.assertEqual(a.read_bytes(), b.read_bytes())
        self.assertEqual((a.parent / "SHA256SUMS").read_bytes(), (b.parent / "SHA256SUMS").read_bytes())

    def test_uncompressed_tar_stream_is_pinned(self):
        tar = gzip.decompress(self.build().read_bytes())
        self.assertEqual(tar, br.build_tar_bytes(self.root, br.select_files(self.root), "2.3.4", EPOCH))
        self.assertEqual(hashlib.sha256(tar).hexdigest(), self.FIXTURE_TAR_SHA256)

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
            self.assertNotIn("\\", m.name)

    def test_pax_format_and_no_pax_headers_for_short_ascii_names(self):
        tar = gzip.decompress(self.build().read_bytes())
        self.assertNotIn(b"PaxHeader", tar)  # nothing in FIXTURE needs one
        self.assertEqual(tar[257:263], b"ustar\x00")  # POSIX ustar magic (pax = ustar + extended headers)

    def test_gzip_header_is_fixed(self):
        raw = self.build().read_bytes()
        self.assertEqual(raw[:10], b"\x1f\x8b\x08\x00\x00\x00\x00\x00\x02\xff",
                         "magic, deflate, no FNAME/FEXTRA flags, mtime 0, XFL 2 (level 9), OS 255")
        self.assertEqual(gzip.decompress(raw)[:len(PREFIX) - 1], PREFIX[:-1].encode())

    def test_epoch_changes_the_archive(self):
        self.assertNotEqual(self.build("a").read_bytes(), self.build("b", epoch=EPOCH + 1).read_bytes())

    def test_rebuild_from_the_extracted_archive_is_byte_identical(self):
        first = self.build("a")
        tree = br.safe_extract(first, self.tmp / "x")
        tar2, _ = br.build(tree, self.tmp / "b", epoch=EPOCH)  # walk mode: the shipped manifest is regenerated
        self.assertEqual(tar2.read_bytes(), first.read_bytes())

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


class TestAtomicOutput(FixtureCase):
    def outputs(self, out: Path) -> list[str]:
        return sorted(p.name for p in out.iterdir()) if out.exists() else []

    def test_failure_renaming_sha256sums_leaves_nothing_behind(self):
        out = self.tmp / "o"
        self.build("o")
        real = os.replace
        calls = []

        def flaky(src, dst):
            calls.append(dst)
            if Path(dst).name == "SHA256SUMS":
                raise OSError("injected: disk full")
            return real(src, dst)

        with mock.patch.object(br.os, "replace", flaky), self.assertRaisesRegex(OSError, "injected"):
            br.build(self.root, out, epoch=EPOCH + 1)
        self.assertEqual(len(calls), 2)
        self.assertEqual(self.outputs(out), [], "partial or stale outputs left behind")

    def test_failure_writing_the_temp_file_leaves_nothing_behind(self):
        out = self.tmp / "o"
        with mock.patch.object(br.os, "fsync", side_effect=OSError("injected: I/O error")), \
                self.assertRaisesRegex(OSError, "injected"):
            br.build(self.root, out, epoch=EPOCH)
        self.assertEqual(self.outputs(out), [])

    def test_interrupt_after_the_tarball_rename_removes_it(self):
        out = self.tmp / "o"
        real = os.replace

        def interrupted(src, dst):
            if Path(dst).name == "SHA256SUMS":
                raise KeyboardInterrupt
            return real(src, dst)

        with mock.patch.object(br.os, "replace", interrupted), self.assertRaises(KeyboardInterrupt):
            br.build(self.root, out, epoch=EPOCH)
        self.assertEqual(self.outputs(out), [])

    def test_content_error_leaves_a_previous_build_untouched(self):
        first = self.build("o")
        before = {p.name: p.read_bytes() for p in first.parent.iterdir()}
        (self.root / "README.md").write_bytes(b"# readme\r\n")
        with self.assertRaises(br.ReleaseError):
            self.build("o")
        self.assertEqual({p.name: p.read_bytes() for p in first.parent.iterdir()}, before)

    def test_sha256sums_is_renamed_last(self):
        order = []
        real = os.replace

        def spy(src, dst):
            order.append(Path(dst).name)
            return real(src, dst)

        with mock.patch.object(br.os, "replace", spy):
            self.build("o")
        self.assertEqual(order, ["ml-ai-skills-2.3.4.tar.gz", "SHA256SUMS"])


class TestGeneratedGate(FixtureCase):
    def set_check(self, code: int, message: str = "") -> None:
        (self.root / "scripts" / "check_generated.py").write_bytes(
            f"import sys\nprint({message!r})\nsys.exit({code})\n".encode())

    def test_drift_refuses_the_build_and_writes_nothing(self):
        self.set_check(1, "GENERATED DRIFT: INDEX.md modified")
        with self.assertRaisesRegex(br.ReleaseError, "(?s)generated artifacts do not match.*INDEX.md modified"):
            self.build("o", verify_generated=True)
        self.assertFalse((self.tmp / "o").exists())

    def test_checker_environment_error_is_an_env_error(self):
        self.set_check(2, "check_generated: git unavailable")
        with self.assertRaises(br.EnvError):
            self.build("o", verify_generated=True)

    def test_clean_check_builds(self):
        self.assertTrue(self.build("o", verify_generated=True).exists())

    def test_cli_runs_the_gate_by_default_and_can_skip_it(self):
        self.set_check(1, "drift")
        out = str(self.tmp / "o")
        err = io.StringIO()
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(err):
            code = br.main(["--root", str(self.root), "--out-dir", out, "--source-date-epoch", "5"])
        self.assertEqual(code, 1, err.getvalue())
        self.assertIn("generated artifacts do not match", err.getvalue())
        with contextlib.redirect_stdout(io.StringIO()):
            code = br.main(["--root", str(self.root), "--out-dir", out, "--source-date-epoch", "5",
                            "--skip-generated-check"])
        self.assertEqual(code, 0)


def make_tar(path: Path, entries: list[tuple[tarfile.TarInfo, bytes | None]]) -> Path:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w", format=tarfile.PAX_FORMAT) as tar:
        for ti, data in entries:
            tar.addfile(ti, io.BytesIO(data) if data is not None else None)
    path.write_bytes(gzip.compress(buf.getvalue()))
    return path


def entry(name: str, data: bytes | None = None, *, kind: bytes = tarfile.REGTYPE, mode: int | None = None,
          link: str = "") -> tuple[tarfile.TarInfo, bytes | None]:
    ti = tarfile.TarInfo(name)
    ti.type = kind
    ti.mode = mode if mode is not None else (0o755 if kind == tarfile.DIRTYPE else 0o644)
    ti.linkname = link
    if kind == tarfile.REGTYPE:
        ti.size = len(data or b"")
    return ti, data if kind == tarfile.REGTYPE else None


class TestVerifyAndSafeExtract(FixtureCase):
    TOP = "ml-ai-skills-2.3.4"

    def valid_entries(self, files: dict[str, bytes]) -> list:
        manifest = br.render_manifest(files)
        return ([entry(self.TOP, kind=tarfile.DIRTYPE)]
                + [entry(f"{self.TOP}/{r}", d) for r, d in sorted(files.items())]
                + [entry(f"{self.TOP}/{br.MANIFEST_NAME}", manifest)])

    def test_built_archive_verifies_and_extracts_with_modes(self):
        archive = self.build()
        self.assertIn("every SHA-256", br.verify_archive(archive, archive.parent / "SHA256SUMS"))
        tree = br.safe_extract(archive, self.tmp / "x")
        for rel, data in FIXTURE.items():
            self.assertEqual((tree / rel).read_bytes(), data, rel)
        if os.name == "posix":
            self.assertTrue(os.access(tree / "install.sh", os.X_OK))
            self.assertFalse(os.access(tree / "README.md", os.X_OK))

    def test_hostile_members_are_refused_before_anything_is_written(self):
        ok = {"a.md": b"a\n"}
        cases = {
            "absolute path": [entry(self.TOP, kind=tarfile.DIRTYPE), entry("/etc/evil", b"x")],
            "dot-dot": [entry(self.TOP, kind=tarfile.DIRTYPE), entry(f"{self.TOP}/../evil", b"x")],
            "backslash": [entry(self.TOP, kind=tarfile.DIRTYPE), entry(f"{self.TOP}\\..\\evil", b"x")],
            "drive letter": [entry(self.TOP, kind=tarfile.DIRTYPE), entry("C:/evil", b"x")],
            "outside prefix": [entry(self.TOP, kind=tarfile.DIRTYPE), entry("other/evil", b"x")],
            "symlink": [entry(self.TOP, kind=tarfile.DIRTYPE),
                        entry(f"{self.TOP}/link", kind=tarfile.SYMTYPE, link="/etc/passwd")],
            "hardlink": [entry(self.TOP, kind=tarfile.DIRTYPE),
                         entry(f"{self.TOP}/hard", kind=tarfile.LNKTYPE, link="/etc/passwd")],
            "device": [entry(self.TOP, kind=tarfile.DIRTYPE), entry(f"{self.TOP}/dev", kind=tarfile.CHRTYPE)],
            "fifo": [entry(self.TOP, kind=tarfile.DIRTYPE), entry(f"{self.TOP}/fifo", kind=tarfile.FIFOTYPE)],
            "setuid mode": [entry(self.TOP, kind=tarfile.DIRTYPE), entry(f"{self.TOP}/a.md", b"a\n", mode=0o4755)],
            "case-insensitive duplicate": self.valid_entries(ok)[:2] + [entry(f"{self.TOP}/A.md", b"b\n")],
            "first member not the version dir": [entry("a.md", b"x")],
            "missing manifest": self.valid_entries(ok)[:-1],
            "manifest mismatch": self.valid_entries(ok)[:-1] + [
                entry(f"{self.TOP}/{br.MANIFEST_NAME}", b"0" * 64 + b"  a.md\n")],
            "unlisted file": self.valid_entries(ok) + [entry(f"{self.TOP}/extra.md", b"e\n")],
            # Consistent manifests below: each is refused by a path rule, not by a hash mismatch.
            "NTFS alternate data stream": self.valid_entries({"a.md": b"a\n", "a.md:hidden": b"secret\n"}),
            "Windows device name": self.valid_entries({"nul": b"x\n"}),
            "device name with extension": self.valid_entries({"docs/CON.md": b"x\n"}),
            "space in name": self.valid_entries({"with space.md": b"x\n"}),
            "trailing dot": self.valid_entries({"a.md": b"a\n", "a.md.": b"b\n"}),
            "file and directory": self.valid_entries({"a": b"x\n", "a/b": b"y\n"}),
            "case-split directory": self.valid_entries({"D/x.md": b"x\n", "d/y.md": b"y\n"}),
            "file vs directory, case-insensitively": self.valid_entries({"A": b"x\n", "a/b": b"y\n"}),
        }
        for label, entries in cases.items():
            with self.subTest(label):
                archive = make_tar(self.tmp / "evil.tar.gz", entries)
                dest = self.tmp / "dest"
                with self.assertRaises(br.ReleaseError):
                    br.safe_extract(archive, dest)
                self.assertFalse(dest.exists() and any(dest.iterdir()), "something was written")
                self.assertFalse((self.tmp / "evil").exists())

    def test_failure_during_extraction_leaves_nothing_behind(self):
        archive = self.build()
        real_open = open
        calls = []

        def flaky(path, mode="r", *a, **kw):
            if mode == "xb":
                calls.append(path)
                if len(calls) == 5:
                    raise OSError("injected: disk full")
            return real_open(path, mode, *a, **kw)

        for dest, pre_exists in ((self.tmp / "new" / "dest", False), (self.tmp / "empty", True)):
            with self.subTest(pre_exists=pre_exists):
                calls.clear()
                if pre_exists:
                    dest.mkdir()
                with mock.patch("builtins.open", flaky), self.assertRaisesRegex(OSError, "injected"):
                    br.safe_extract(archive, dest)
                self.assertEqual(len(calls), 5)
                if pre_exists:
                    self.assertEqual(list(dest.iterdir()), [], "partial tree or staging dir left behind")
                else:
                    self.assertFalse(dest.exists(), "a destination this call created was left behind")

    def test_valid_handmade_archive_extracts(self):
        archive = make_tar(self.tmp / "ok.tar.gz", self.valid_entries({"a.md": b"a\n", "d/b.md": b"b\n"}))
        tree = br.safe_extract(archive, self.tmp / "dest")
        self.assertEqual((tree / "d" / "b.md").read_bytes(), b"b\n")

    def test_not_a_gzip_tarball_is_a_release_error(self):
        bad = self.tmp / "bad.tar.gz"
        bad.write_bytes(b"not gzip")
        with self.assertRaisesRegex(br.ReleaseError, "not a readable tar.gz"):
            br.verify_archive(bad)

    def test_non_empty_destination_is_refused(self):
        dest = self.tmp / "dest"
        dest.mkdir()
        (dest / "keep.txt").write_bytes(b"x")
        with self.assertRaisesRegex(br.EnvError, "not empty"):
            br.safe_extract(self.build(), dest)

    def test_sha256sums_mismatch_is_reported(self):
        archive = self.build()
        (archive.parent / "SHA256SUMS").write_text(f"{'0' * 64}  {archive.name}\n", encoding="ascii")
        with self.assertRaisesRegex(br.ReleaseError, "sha256"):
            br.verify_archive(archive, archive.parent / "SHA256SUMS")

    def test_summary_says_whether_sha256sums_was_checked(self):
        archive = self.build()
        sums = archive.parent / "SHA256SUMS"
        self.assertIn("SHA256SUMS: matches", br.verify_archive(archive, sums, require_sums=True))
        sums.unlink()
        self.assertIn("SHA256SUMS: not checked", br.verify_archive(archive, sums))
        with self.assertRaisesRegex(br.ReleaseError, "no SHA256SUMS next to"):
            br.verify_archive(archive, sums, require_sums=True)


class TestCli(FixtureCase):
    def run_main(self, *args: str) -> tuple[int, str]:
        err = io.StringIO()
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(err):
            code = br.main(["--root", str(self.root), *args])
        return code, err.getvalue()

    def test_exit_codes(self):
        out = str(self.tmp / "o")
        self.assertEqual(self.run_main("--out-dir", out, "--source-date-epoch", "5")[0], 0)
        self.assertEqual(self.run_main("--out-dir", out, "--source-date-epoch", "5",
                                       "--expect-version", "9.9.9")[0], 1)
        code, err = self.run_main("--out-dir", out, "--source-date-epoch", "5", "--require-clean")
        self.assertEqual(code, 2, err)  # fixture is not a git checkout
        archive = str(self.tmp / "o" / "ml-ai-skills-2.3.4.tar.gz")
        self.assertEqual(self.run_main("--verify-archive", archive)[0], 0)
        self.assertEqual(self.run_main("--verify-archive", archive, "--extract-to", str(self.tmp / "x"))[0], 0)
        self.assertTrue((self.tmp / "x" / "ml-ai-skills-2.3.4" / "VERSION").is_file())
        self.assertEqual(self.run_main("--verify-archive", archive, "--extract-to", str(self.tmp / "x"))[0], 2)
        (self.tmp / "o" / "SHA256SUMS").write_text(f"{'0' * 64}  ml-ai-skills-2.3.4.tar.gz\n", encoding="ascii")
        self.assertEqual(self.run_main("--verify-archive", archive)[0], 1)
        (self.tmp / "o" / "SHA256SUMS").unlink()  # the CLI requires SHA256SUMS unless --no-sums
        code, err = self.run_main("--verify-archive", archive)
        self.assertEqual(code, 1)
        self.assertIn("--no-sums", err)
        self.assertEqual(self.run_main("--verify-archive", archive, "--no-sums")[0], 0)
        for orphan in (["--extract-to", str(self.tmp / "y")], ["--no-sums"]):
            with self.subTest(orphan=orphan), contextlib.redirect_stderr(io.StringIO()), \
                    self.assertRaises(SystemExit):
                br.main(orphan)

    def test_list_prints_the_shipped_files(self):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            self.assertEqual(br.main(["--root", str(self.root), "--list"]), 0)
        self.assertEqual(buf.getvalue().splitlines(), sorted(FIXTURE))


if __name__ == "__main__":
    unittest.main()
