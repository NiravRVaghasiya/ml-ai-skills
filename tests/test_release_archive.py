"""The real release archive is a self-contained developer distribution (docs/RELEASE.md,
"What ships"): build it from this checkout, verify and safely extract it, then run the
documented checks from the extracted tree, which is not a git checkout.

Default run: the fast documented checks (validators, offline evals, routing gate against
the shipped baseline, claims, freshness, generator --check modes, contract, workflow
audit, quality report, installer). ML_AI_SKILLS_RELEASE_SLOW=1 adds the slow ones:
scripts/check_generated.py and the stdlib unit suite, both run from the extracted tree
(release.yml's verify job sets it). Nested runs set ML_AI_SKILLS_RELEASE_NESTED=1 so the
unit suite inside the archive does not rebuild and re-test the archive recursively.

What these tests build is the working tree (tracked + untracked-but-not-ignored files),
so a check that fails in the checkout also fails here; the failure message says whether
the same command also fails in the checkout.
"""
from __future__ import annotations

import json
import os
import posixpath
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path, PurePosixPath

from testutil import REPO_ROOT

sys.path.insert(0, str(REPO_ROOT / "scripts"))
import build_release as br  # noqa: E402
import eval_router as er  # noqa: E402

EPOCH = 1_700_000_000
BASH = shutil.which("bash")
GIT = shutil.which("git")
SLOW = os.environ.get("ML_AI_SKILLS_RELEASE_SLOW") == "1"
NESTED = os.environ.get("ML_AI_SKILLS_RELEASE_NESTED") == "1"
TIMEOUT = 900
GENERATED_TIMEOUT = 1800
UNIT_TIMEOUT = 3600

LINK_RE = re.compile(r"(?<!!)\[[^\]\n]*\]\(\s*<?([^)\s>]+)>?(?:\s+\"[^\"]*\")?\s*\)")
FENCE_RE = re.compile(r"^(```|~~~).*?^\1", re.M | re.S)
# Backticked repo paths in prose ("see `tests/test_x.py`"): checked when they start with
# one of these trees or name a top-level file, end in a file extension, and are not a
# pattern or placeholder.
PATH_TOKEN_RE = re.compile(r"`((?:scripts|schema|tests|evals|docs|\.github|bin|plugins|_TEMPLATE)/[^`\s]+"
                           r"|(?:CLAUDE|CONTRIBUTING|SECURITY|README|CHANGELOG|INDEX)\.md|requirements-[a-z]+\.txt"
                           r"|ruff\.toml)`")
PLACEHOLDER_CHARS = set("*<>{}$[]()|=,'\"")
REPO_SUFFIXES = (".py", ".md", ".json", ".yml", ".yaml", ".txt", ".toml", ".sh")


def run_py(tree: Path, scratch: Path, *args: str, timeout: int = TIMEOUT, env_extra: dict | None = None,
           isolated: bool = True):
    """Run a shipped script from `tree` the way a user would: cwd = tree, isolated mode by
    default (-I: no user site, no PYTHON* env, only the script's directory on sys.path) and
    git discovery stopped at `scratch`, so an enclosing repository is never picked up."""
    env = {k: v for k, v in os.environ.items() if not k.startswith(("PYTHON", "ML_AI_SKILLS_RELEASE_"))}
    env.update(GIT_CEILING_DIRECTORIES=str(scratch), PYTHONUTF8="1", **(env_extra or {}))
    flags = ["-I"] if isolated else []
    return subprocess.run([sys.executable, *flags, *args], cwd=tree, capture_output=True, text=True,
                          encoding="utf-8", errors="replace", timeout=timeout, env=env)


def routing_floor_args(tree: Path) -> list[str]:
    """`eval_router.py` threshold flags from the shipped baseline's test split (CI checks 1+2)."""
    baseline = json.loads((tree / "evals/routing/baseline.json").read_text(encoding="utf-8"))
    floor = baseline["metrics"]["test"]
    args = ["--top-k", str(baseline.get("top_k", 5))]
    for flag, (metric, _kind) in er.THRESHOLD_FLAGS.items():
        if floor.get(metric) is not None:
            args += ["--" + flag.replace("_", "-"), str(floor[metric])]
    return args


def unit_modules(tree: Path) -> list[str]:
    """CI's stdlib unit list: tests/test_*.py except the installer tests (ci.yml `unit`)."""
    return sorted(p.stem for p in (tree / "tests").glob("test_*.py") if not p.name.startswith("test_install"))


@unittest.skipIf(NESTED, "running inside a release-archive self-test (no recursive rebuild)")
class TestReleaseArchive(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # ignore_cleanup_errors: on Windows a process the nested unit suite started can still hold
        # a handle on the extracted tree for a moment; a leftover temp dir must not fail the class.
        cls._tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        cls.tmp = Path(cls._tmp.name)
        try:
            cls.archive, cls.sums = br.build(REPO_ROOT, cls.tmp / "a", epoch=EPOCH)
            cls.archive_b, _ = br.build(REPO_ROOT, cls.tmp / "b", epoch=EPOCH)
        except br.ReleaseError as e:
            cls._tmp.cleanup()
            # A checkout made before .gitattributes existed may have CRLF text: a local run skips
            # with the reason (tests/test_release_gitattributes.py covers line endings), but in
            # CI or the release gate (ML_AI_SKILLS_RELEASE_SLOW=1) it fails instead of skipping.
            if "CRLF" in str(e) and not (SLOW or os.environ.get("CI")):
                raise unittest.SkipTest(f"working tree has CRLF in a shipped file ({e})") from e
            raise
        cls.tree = br.safe_extract(cls.archive, cls.tmp / "x")
        cls.shipped = sorted(p.relative_to(cls.tree).as_posix() for p in cls.tree.rglob("*") if p.is_file())

    @classmethod
    def tearDownClass(cls):
        cls._tmp.cleanup()

    def assert_runs(self, *args: str, timeout: int = TIMEOUT, env_extra: dict | None = None) -> None:
        proc = run_py(self.tree, self.tmp, *args, timeout=timeout, env_extra=env_extra)
        if proc.returncode != 0:
            here = run_py(REPO_ROOT, self.tmp, *args, timeout=timeout, env_extra=env_extra)
            where = ("it also fails in the checkout (exit %d), so this is the tree's state, not a packaging gap"
                     % here.returncode if here.returncode else "it PASSES in the checkout: the archive is missing "
                     "something")
            self.fail(f"{' '.join(args)} exited {proc.returncode} from the extracted archive; {where}\n"
                      + proc.stdout[-3000:] + proc.stderr[-3000:])

    # ------------------------------------------------------------------ the archive itself

    def test_two_builds_are_byte_identical(self):
        self.assertEqual(self.archive.read_bytes(), self.archive_b.read_bytes())

    def test_archive_verifies_against_sha256sums_and_its_manifest(self):
        summary = br.verify_archive(self.archive, self.sums, require_sums=True)
        self.assertIn("every SHA-256", summary)
        self.assertIn("SHA256SUMS: matches", summary)

    def test_rebuild_from_the_extracted_tree_is_byte_identical(self):
        pristine = br.safe_extract(self.archive, self.tmp / "pristine")
        again, _ = br.build(pristine, self.tmp / "rebuilt", epoch=EPOCH)
        self.assertEqual(again.read_bytes(), self.archive.read_bytes())

    @unittest.skipUnless(GIT, "git is not installed")
    def test_every_file_git_would_commit_ships(self):
        def ls(*args: str) -> set[str]:
            out = subprocess.run([GIT, "-C", str(REPO_ROOT), "-c", "core.quotepath=off", "ls-files", "-z", *args],
                                 capture_output=True, timeout=60, check=True)
            return {n for n in out.stdout.decode("utf-8").split("\0") if n and (REPO_ROOT / n).is_file()}

        # Every tracked file ships, with no exclusion rule applied (the builder refuses a tracked
        # file that matches one); only untracked, not-ignored files go through EXCLUDED_*.
        tracked = ls("--cached")
        untracked = ls("--others", "--exclude-standard") - tracked
        expected = tracked | {n for n in untracked if not br._excluded(n)}
        self.assertEqual(set(self.shipped) - {br.MANIFEST_NAME}, expected)
        for tree in ("tests/examples/", "evals/routing/", ".github/workflows/"):
            self.assertTrue(any(n.startswith(tree) for n in self.shipped), tree)

    def test_contributor_files_ship(self):
        for rel in ("CLAUDE.md", "CONTRIBUTING.md", "SECURITY.md", "requirements-dev.txt", "requirements-examples.txt",
                    "ruff.toml", ".shellcheckrc", ".gitattributes", ".github/dependabot.yml",
                    "_TEMPLATE/SKILL.md", "tests/examples/harness.py", "evals/routing/baseline.json"):
            with self.subTest(rel=rel):
                self.assertIn(rel, self.shipped)

    def test_nothing_local_ships(self):
        bad = [n for n in self.shipped if n.split("/", 1)[0] in (".claude", ".git", "dist", ".venv")
               or "__pycache__" in n.split("/") or n == "validate_out.json"]
        self.assertEqual(bad, [])

    # ------------------------------------------------------------------ shipped docs

    def _shipped_markdown(self):
        for rel in self.shipped:
            if rel.endswith(".md"):
                yield rel, FENCE_RE.sub("", (self.tree / rel).read_text(encoding="utf-8"))

    def _exists(self, target: str) -> bool:
        return (self.tree / target).exists() and PurePosixPath(target).parts[:1] != ("..",)

    def test_relative_markdown_links_resolve_inside_the_archive(self):
        broken = []
        for rel, text in self._shipped_markdown():
            for m in LINK_RE.finditer(text):
                target = m.group(1)
                if re.match(r"^[a-z][a-z0-9+.-]*:", target, re.I) or target.startswith("#"):
                    continue
                path = posixpath.normpath(posixpath.join(posixpath.dirname(rel), target.split("#", 1)[0]))
                if not self._exists(path):
                    broken.append(f"{rel}: ({target})")
        self.assertEqual(broken, [])

    def test_repo_paths_named_in_shipped_docs_exist_in_the_archive(self):
        missing = []
        for rel, text in self._shipped_markdown():
            if rel.startswith("plugins/"):  # byte-identical copies of the skill files
                continue
            for m in PATH_TOKEN_RE.finditer(text):
                token = m.group(1).split("#", 1)[0].rstrip("/")
                # Only this repo's own file types: `docs/index.rst` naming a file in another
                # project's repository is not a reference into this tree.
                if PLACEHOLDER_CHARS & set(token) or "..." in token or not token.endswith(REPO_SUFFIXES):
                    continue
                if not self._exists(token):
                    missing.append(f"{rel}: `{token}`")
        self.assertEqual(sorted(set(missing)), [])

    # ------------------------------------------------------------------ documented checks

    def test_validate_skills(self):
        self.assert_runs("scripts/validate_skills.py", "--warnings")

    def test_validate_evals(self):
        self.assert_runs("scripts/validate_evals.py", "--strict", "--coverage")
        self.assert_runs("scripts/validate_evals.py", "--strict", "--require-full-coverage")

    def test_routing_benchmark_meets_the_shipped_baseline(self):
        baseline = json.loads((self.tree / "evals/routing/baseline.json").read_text(encoding="utf-8"))
        self.assertEqual(baseline["benchmark_sha256"], er.benchmark_sha256(self.tree / "evals/routing/benchmark.json"),
                         "baseline.json does not match the shipped benchmark (CI integrity check)")
        self.assert_runs("scripts/eval_router.py", "--split", "test", *routing_floor_args(self.tree))
        self.assert_runs("scripts/eval_router.py", "--split", "dev")

    def test_offline_evals(self):
        self.assert_runs("scripts/run_evals.py", "--out", str(self.tmp / "eval-report.json"))

    def test_claims_freshness_contract_schema_and_workflows(self):
        for args in (("scripts/check_claims.py",), ("scripts/check_freshness.py",),
                     ("scripts/check_contract.py", "--check"), ("scripts/generate_schema.py", "--check"),
                     ("scripts/audit_workflows.py", "--strict"), ("scripts/list_skills.py",),
                     ("scripts/router.py", "fix data leakage in my train/test split")):
            with self.subTest(args=args):
                self.assert_runs(*args)

    def test_generator_check_modes(self):
        for script in ("generate_index.py", "generate_plugins.py", "generate_readme_stats.py",
                       "build_dependency_graph.py"):
            with self.subTest(script=script):
                self.assert_runs(f"scripts/{script}", "--check")

    def test_quality_report(self):
        out = self.tmp / "quality-report.json"
        self.assert_runs("scripts/quality_report.py", "--out", str(out))
        report = json.loads(out.read_text(encoding="utf-8"))
        self.assertIsNone(report["environment"]["git_revision"],
                          "the extracted tree is not a git checkout; a revision here came from an enclosing repo")

    @unittest.skipUnless(BASH, "bash not found on PATH")
    def test_install_and_uninstall_from_the_archive(self):
        target = self.tmp / "installed"
        for script in ("install.sh", "uninstall.sh"):
            proc = subprocess.run([BASH, str(self.tree / script), "--target", str(target)], capture_output=True,
                                  text=True, encoding="utf-8", errors="replace", timeout=TIMEOUT)
            self.assertEqual(proc.returncode, 0, proc.stdout[-2000:] + proc.stderr[-2000:])
            if script == "install.sh":
                installed = {p.parent.name for p in target.glob("*/SKILL.md")}
                self.assertEqual(installed, {d.name for d in br.sl.iter_skill_dirs(self.tree)})
        self.assertEqual(list(target.glob("*/SKILL.md")), [])

    # ------------------------------------------------------------------ slow (opt-in)

    @unittest.skipUnless(SLOW, "set ML_AI_SKILLS_RELEASE_SLOW=1 to run check_generated.py from the archive (~2 min)")
    def test_check_generated_from_the_archive(self):
        self.assert_runs("scripts/check_generated.py", timeout=GENERATED_TIMEOUT)

    @unittest.skipUnless(SLOW, "set ML_AI_SKILLS_RELEASE_SLOW=1 to run the stdlib unit suite from the archive "
                               "(10+ min)")
    def test_stdlib_unit_suite_from_the_archive(self):
        unit_tree = br.safe_extract(self.archive, self.tmp / "unit")
        # Not -I: the suite needs PYTHONPATH=tests, exactly as ci.yml's `unit` job runs it.
        proc = run_py(unit_tree, self.tmp, "-m", "unittest", *unit_modules(unit_tree), timeout=UNIT_TIMEOUT,
                      env_extra={"ML_AI_SKILLS_RELEASE_NESTED": "1", "PYTHONPATH": "tests"}, isolated=False)
        self.assertEqual(proc.returncode, 0, proc.stdout[-4000:] + proc.stderr[-6000:])



if __name__ == "__main__":
    unittest.main()
