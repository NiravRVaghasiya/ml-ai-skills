"""Self-tests for the example harness (stdlib only — always runs)."""
from __future__ import annotations

import ast
import importlib.util
import itertools
import os
import re
import subprocess
import sys
import unittest
from pathlib import Path
from unittest import mock

from examples import harness

SKILLS = ()  # harness self-tests: evidence for no skill (scripts/quality_report.py example attribution)


class TestHarness(unittest.TestCase):
    def test_extracts_blocks_from_canonical_skill(self):
        blocks = harness.python_blocks("data-preprocessing")
        self.assertGreater(len(blocks), 0)

    def test_illustrative_marker(self):
        self.assertTrue(harness.is_illustrative("\n# illustrative: needs an API key\nx = 1\n"))
        self.assertFalse(harness.is_illustrative("x = 1\n# illustrative\n"))

    def test_replacement_must_match_exactly_once(self):
        with self.assertRaises(AssertionError):
            harness._apply_replacements("a = 1\na = 1\n", {"a = 1": "a = 2"}, "x")
        with self.assertRaises(AssertionError):
            harness._apply_replacements("a = 1\n", {"b = 1": "b = 2"}, "x")
        self.assertEqual(harness._apply_replacements("a = 1\n", {"a = 1": "a = 2"}, "x"), "a = 2\n")

    def test_out_of_range_block_index_errors(self):
        with self.assertRaises(AssertionError):
            harness.run_blocks("data-preprocessing", [999])

    def test_requires_skips_or_fails_on_missing_module(self):
        # requires() reads the env var when the decorator is applied, so each
        # variant is decorated inside its own patched environment.
        with mock.patch.dict(os.environ, {harness.REQUIRE_ENV: "0"}):
            @harness.requires("definitely_not_a_real_module_xyz")
            class T(unittest.TestCase):
                def test_x(self):
                    pass

        result = unittest.TestResult()
        unittest.defaultTestLoader.loadTestsFromTestCase(T).run(result)
        self.assertEqual(len(result.skipped), 1)

        with mock.patch.dict(os.environ, {harness.REQUIRE_ENV: "1"}):
            @harness.requires("definitely_not_a_real_module_xyz")
            class Strict(unittest.TestCase):
                def test_x(self):
                    pass
        strict_result = unittest.TestResult()
        unittest.defaultTestLoader.loadTestsFromTestCase(Strict).run(strict_result)
        self.assertEqual(len(strict_result.skipped), 0)
        self.assertGreaterEqual(len(strict_result.errors) + len(strict_result.failures), 1)


class TestBlockWatchdog(unittest.TestCase):
    """ML_AI_SKILLS_EXAMPLE_TIMEOUT: opt-in per-run_blocks watchdog (faulthandler)."""

    def _env(self, value: str | None):
        env = {k: v for k, v in os.environ.items() if k != harness.TIMEOUT_ENV}
        if value is not None:
            env[harness.TIMEOUT_ENV] = value
        return mock.patch.dict(os.environ, env, clear=True)

    def test_off_unless_set_to_a_positive_number(self):
        for value in (None, "", "0", "-5"):
            with (self.subTest(value=value), self._env(value),
                  mock.patch.object(harness.faulthandler, "dump_traceback_later") as arm):
                with harness.block_watchdog():
                    pass
                arm.assert_not_called()

    def test_bad_value_is_an_error_not_silently_off(self):
        for value in ("ten", "inf", "nan"):
            with self.subTest(value=value), self._env(value), self.assertRaises(AssertionError):
                harness.example_timeout()

    def test_armed_with_the_limit_and_cancelled_even_when_a_block_raises(self):
        with (self._env("900"),
              mock.patch.object(harness.faulthandler, "dump_traceback_later") as arm,
              mock.patch.object(harness.faulthandler, "cancel_dump_traceback_later") as cancel,
              mock.patch.object(harness, "python_blocks", return_value=["raise ValueError('boom')"])):
            with self.assertRaises(ValueError):
                harness.run_blocks("data-preprocessing", [0])
        arm.assert_called_once()
        self.assertEqual(arm.call_args.args, (900.0,))
        self.assertIs(arm.call_args.kwargs["exit"], True)
        cancel.assert_called_once_with()

    def test_a_hung_block_exits_the_process_with_a_traceback(self):
        # Real faulthandler, in a child process (it exits the interpreter). The block sleeps
        # 60 s; the 2 s watchdog must end the child long before the 120 s bound.
        child = (
            "import sys\n"
            f"sys.path.insert(0, {str(harness.TESTS_DIR)!r})\n"
            "from examples import harness\n"
            "harness.python_blocks = lambda slug: ['import time; time.sleep(60)']\n"
            "harness.run_blocks('data-preprocessing', [0])\n"
            "print('not reached')\n"
        )
        env = {**os.environ, harness.TIMEOUT_ENV: "2"}
        env.pop(harness.COVERAGE_ENV, None)
        proc = subprocess.run([sys.executable, "-c", child], capture_output=True, text=True,
                              env=env, timeout=120)
        self.assertEqual(proc.returncode, 1, proc.stdout + proc.stderr)
        self.assertNotIn("not reached", proc.stdout)
        self.assertIn("Timeout", proc.stderr)
        self.assertIn("python block #0", proc.stderr)


def _load_fresh(path: Path, env: dict[str, str]):
    """Import a test module under a throwaway name with ``env`` applied.

    Skip decorators read the environment at import time, so the already-imported module says
    nothing about how it would behave in CI; a fresh copy imported under CI's variables does.
    """
    name = f"examples._env_probe_{path.stem}"
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    with mock.patch.dict(os.environ, env):
        for var in ("ML_AI_SKILLS_SLOW_EXAMPLES",):
            if var not in env:
                os.environ.pop(var, None)   # restored when the patch exits
        try:
            spec.loader.exec_module(module)
        finally:
            sys.modules.pop(name, None)
    return module


def _skipped_tests(module) -> list[str]:
    out = []
    for name, obj in vars(module).items():
        if not (isinstance(obj, type) and issubclass(obj, unittest.TestCase)) or obj.__module__ != module.__name__:
            continue
        if getattr(obj, "__unittest_skip__", False):
            out.append(f"{name}: {obj.__unittest_skip_why__}")
            continue
        for attr, fn in vars(obj).items():
            if attr.startswith("test") and getattr(fn, "__unittest_skip__", False):
                out.append(f"{name}.{attr}: {fn.__unittest_skip_why__}")
    return out


class TestNothingSkipsAtImportUnderTheCiEnvironment(unittest.TestCase):
    """docs/KNOWN-ISSUES.md #10: with ML_AI_SKILLS_REQUIRE_EXAMPLE_DEPS=1, as the CI examples job
    sets it, no example test may be skipped by a decorator: a missing dependency fails, and the
    slow MLflow classes (experiment-tracking, ci-cd-for-ml) run. Runtime skips that depend on the
    installed version or hardware (``self.skipTest`` inside a test) are outside this check."""

    def test_no_class_or_method_level_skip_with_require_env(self):
        paths = sorted(Path(__file__).resolve().parent.glob("test_*.py"))
        self.assertGreater(len(paths), 20)
        skipped = {}
        for path in paths:
            if path.name == Path(__file__).name:
                continue
            found = _skipped_tests(_load_fresh(path, {harness.REQUIRE_ENV: "1"}))
            if found:
                skipped[path.stem] = found
        self.assertEqual(skipped, {})

    def test_slow_mlflow_classes_skip_by_default_and_run_under_either_opt_in(self):
        examples_dir = Path(__file__).resolve().parent
        for stem, cls_name in (("test_experiment_tracking", "TestExperimentTrackingSkill"),
                               ("test_ci_cd_for_ml", "TestReleaseAgainstRealMlflow")):
            with self.subTest(module=stem):
                default = getattr(_load_fresh(examples_dir / f"{stem}.py", {harness.REQUIRE_ENV: ""}), cls_name)
                self.assertTrue(getattr(default, "__unittest_skip__", False))
                self.assertIn("ML_AI_SKILLS_SLOW_EXAMPLES=1", default.__unittest_skip_why__)
                for env in ({harness.REQUIRE_ENV: "1"}, {"ML_AI_SKILLS_SLOW_EXAMPLES": "1", harness.REQUIRE_ENV: ""}):
                    cls = getattr(_load_fresh(examples_dir / f"{stem}.py", env), cls_name)
                    # a missing mlflow makes it fail (REQUIRE) or skip naming the dependency,
                    # never skip as "slow"
                    why = getattr(cls, "__unittest_skip_why__", "") if getattr(cls, "__unittest_skip__", False) else ""
                    self.assertNotIn("slow", why, env)


# Every runtime ``self.skipTest(...)`` in tests/examples that does not fail under
# ML_AI_SKILLS_REQUIRE_EXAMPLE_DEPS=1 itself, keyed by (module stem, literal reason), with why a
# skip there is acceptable in CI. A new runtime skip must either check harness.REQUIRE_ENV in the
# same test (fail instead of skip in CI) or be classified here.
RUNTIME_SKIP_CLASSIFICATION = {
    ("test_ml_monitoring", "checks the 0.7 API break specifically"):
        "version gate: only meaningful on evidently 0.7.x (the pin is 0.7.23, so it runs in CI)",
    ("test_nlp_tasks", "summarization pipeline still exists before Transformers 5"):
        "version gate: only meaningful on transformers >= 5 (the pin is 5.x, so it runs in CI)",
    ("test_python_for_ml", "pandas >= 3 always uses Copy-on-Write"):
        "version gate: only meaningful on pandas < 3 (the pin is 2.2.3, so it runs in CI)",
    ("test_pytorch_patterns", "pin_memory is meaningful when CUDA is present"):
        "hardware gate: checks the CPU-only case; the CUDA case is tests/integration/test_gpu.py (opt-in)",
}


def _runtime_skips(path: Path) -> list[tuple[str, str, bool]]:
    """``(test function, literal reason or '<dynamic>', function checks REQUIRE_ENV)`` per skipTest call."""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    out = []
    for fn in ast.walk(tree):
        if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        nodes = list(ast.walk(fn))
        checks_require = any(
            (isinstance(n, ast.Attribute) and n.attr == "REQUIRE_ENV")
            or (isinstance(n, ast.Name) and n.id == "REQUIRE_ENV")
            or (isinstance(n, ast.Constant) and n.value == harness.REQUIRE_ENV) for n in nodes)
        for n in nodes:
            if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute) and n.func.attr == "skipTest":
                arg = n.args[0] if n.args else None
                reason = arg.value if isinstance(arg, ast.Constant) and isinstance(arg.value, str) else "<dynamic>"
                out.append((fn.name, reason, checks_require))
    return out


class TestRuntimeSkipsAndTheCiJobUnderTheRequireEnv(unittest.TestCase):
    """docs/KNOWN-ISSUES.md #10, the parts the import-time check above cannot see: runtime
    ``skipTest`` calls, and whether the CI examples job sets the variable at all (the import-time
    check sets it itself, so it would keep passing if ci.yml dropped it)."""

    def test_every_runtime_skip_fails_under_require_or_is_classified(self):
        examples_dir = Path(__file__).resolve().parent
        unclassified, seen = [], set()
        for path in sorted(examples_dir.glob("test_*.py")):
            for fn, reason, checks_require in _runtime_skips(path):
                key = (path.stem, reason)
                if key in RUNTIME_SKIP_CLASSIFICATION:
                    seen.add(key)
                elif not checks_require:
                    unclassified.append(f"{path.stem}.{fn}: skipTest({reason!r})")
        self.assertEqual(unclassified, [], "a runtime skip that can fire silently in CI: make it fail "
                         "when harness.REQUIRE_ENV is '1', or classify it in RUNTIME_SKIP_CLASSIFICATION")
        self.assertEqual(sorted(set(RUNTIME_SKIP_CLASSIFICATION) - seen), [],
                         "stale RUNTIME_SKIP_CLASSIFICATION entries (the skip is gone or reworded)")

    def test_ci_examples_job_sets_the_require_env_and_runs_this_directory(self):
        ci = Path(__file__).resolve().parents[2] / ".github" / "workflows" / "ci.yml"
        self.assertTrue(ci.is_file(), ci)
        lines = ci.read_text(encoding="utf-8").splitlines()
        start = lines.index("  examples:")   # a job id directly under jobs:
        end = next((i for i in range(start + 1, len(lines))
                    if re.match(r"^  [A-Za-z0-9_-]+:\s*$", lines[i])), len(lines))
        job = lines[start:end]
        # job-level env (4-space "env:", 6-space variables), so every step inherits it
        env_at = job.index("    env:")
        job_env = list(itertools.takewhile(lambda ln: ln.startswith("      ") or not ln.strip(), job[env_at + 1:]))
        self.assertTrue(any(re.fullmatch(rf"\s{{6}}{harness.REQUIRE_ENV}:\s*([\"']?)1\1\s*(#.*)?", ln)
                            for ln in job_env), job_env)
        self.assertTrue(any("discover -s tests/examples" in ln for ln in job),
                        "the examples job no longer runs tests/examples")


if __name__ == "__main__":
    unittest.main()
