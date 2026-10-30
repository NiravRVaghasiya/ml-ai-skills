"""Executable tests for ci-cd-for-ml/SKILL.md.

The skill's python blocks are a pytest ``conftest.py`` and two test modules
that import a project-specific ``src.pipeline``. What is stubbed, and why:

* ``src.pipeline`` -> ``fake_pipeline(variant)``: a tiny synthetic churn
  dataset + LogisticRegression, with variants that inject exactly one defect
  (a leaked feature, dtype drift, a model that can't learn). The skill
  documents this module as the user's own code.
* The blocks are executed in-process with harness.run_blocks and their
  ``test_*`` functions called directly (fast), and the clean variant is also
  written out as a project and run under real ``pytest.main`` (in-process), so
  conftest discovery, the autouse fixture and the ``src.pipeline`` imports are
  exercised the way CI would run them.
* The YAML workflows are parsed with PyYAML and checked structurally; they are
  not executed (that needs a GitHub Actions runner).
* ``mlflow`` for the step-5 ``src/release.py`` block -> ``fake_mlflow``: an
  in-memory registry whose client methods take the keyword names of mlflow
  3.16.1's ``MlflowClient`` (read with ``inspect.signature`` on 2026-10-28).
  ``TestReleaseAgainstRealMlflow`` runs the same block against real MLflow on a
  SQLite store; it is opt-in like test_experiment_tracking (MLflow setup is slow).

Regressions (each failed against the pre-fix skill):
* the pre-fix leakage test (ci-cd-for-ml/SKILL.md at commit 8328c60, quoted in
  ORIGINAL_LEAKAGE_TEST) is a tautology — it passes even when the pipeline
  trains on a column that *is* the target. The fixed test fails on it.
* the release job promoted a registry version the gate never scored: the gate
  ran pytest on a model it trained itself, then ``src.promote_model`` (not in
  the skill) moved ``champion`` (observed 2026-10-28). The promote step must run
  a module the skill defines, and that module must refuse a bad registered
  version even when every pytest gate passes.
* the example lockfile pinned mlflow==2.16.2 and pytest==8.3.3, which have
  published advisories (OSV, 2026-10-28), and CI never audited the lockfile.
"""
from __future__ import annotations

import os
import re
import sys
import tempfile
import textwrap
import unittest
import warnings
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from examples import harness

SLUG = "ci-cd-for-ml"
# Python block indices (document order): 0 conftest, 1 data contract, 2 model quality,
# 3 src/release.py. Blocks 0-2 are the pytest suite; 3 needs mlflow.
PYTEST_BLOCKS, RELEASE_BLOCK = [0, 1, 2], 3
SHA = "0123456789abcdef0123456789abcdef01234567"
# First versions with no OSV advisory on 2026-10-28 (api.osv.dev/v1/query). mlflow 2.16.2
# had 32 GHSA ids, among them GHSA-wf7f-8fxf-xfxc (unsafe deserialization), GHSA-fhff-qmm8-h2fp
# (tar traversal, fixed 3.9.0rc0) and GHSA-wqmh-qccw-pqwf (fixed 3.17.2); pytest 8.3.3 had
# GHSA-6w46-j5rx-g56g (tmpdir handling, fixed 9.0.3).
MIN_ADVISORY_FREE = {"mlflow": (3, 17, 2), "pytest": (9, 0, 3)}

ORIGINAL_LEAKAGE_TEST = '''
from src.pipeline import load_training_data

def test_no_target_leakage_into_features():
    df = load_training_data()
    feature_cols = [c for c in df.columns if c != "churned"]
    assert "churned" not in feature_cols
'''

FAKE_PIPELINE_SRC = textwrap.dedent('''
    import os

    import numpy as np
    import pandas as pd
    from sklearn.dummy import DummyClassifier
    from sklearn.linear_model import LogisticRegression
    from sklearn.model_selection import train_test_split

    VARIANT = os.environ.get("FAKE_PIPELINE_VARIANT", "clean")
    FEATURE_COLUMNS = ["orders_30d", "spend_30d"] + (["churn_flag_copy"] if VARIANT == "leaky" else [])

    def load_training_data():
        g = np.random.default_rng(0)
        n = 400
        orders = g.poisson(3, n).astype("int64")
        spend = (orders * 20 + g.normal(0, 5, n)).clip(0)
        churned = (orders < 2).astype("int64")
        flip = g.random(n) < 0.05                      # label noise: no feature is perfect
        churned = np.where(flip, 1 - churned, churned).astype("int64")
        df = pd.DataFrame({"customer_id": np.arange(n, dtype="int64"), "orders_30d": orders,
                           "spend_30d": spend, "churned": churned})
        if VARIANT == "leaky":
            df["churn_flag_copy"] = df["churned"].astype(float)
        if VARIANT == "dtype_drift":
            df["spend_30d"] = df["spend_30d"].round(2).astype(str)
        return df

    def split(df):
        return train_test_split(df[FEATURE_COLUMNS], df["churned"], test_size=0.25,
                                random_state=0, stratify=df["churned"])

    def load_holdout_data():
        df = load_training_data().sample(frac=0.3, random_state=1)
        return df[FEATURE_COLUMNS], df["churned"]

    def train_model(X, y):
        if VARIANT == "weak_model":
            return DummyClassifier(strategy="most_frequent").fit(X, y)
        return LogisticRegression(max_iter=1000).fit(X, y)
''')

TEST_NAMES = [
    "test_schema_matches_contract",
    "test_values_within_contract",
    "test_no_nulls_in_required_columns",
    "test_no_target_leakage_into_features",
    "test_model_beats_quality_bar",
]


def fake_pipeline(variant: str):
    old = os.environ.get("FAKE_PIPELINE_VARIANT")
    os.environ["FAKE_PIPELINE_VARIANT"] = variant
    try:
        mod = harness.make_module("src.pipeline")
        exec(compile(FAKE_PIPELINE_SRC, "src/pipeline.py", "exec"), mod.__dict__)  # noqa: S102
    finally:
        if old is None:
            os.environ.pop("FAKE_PIPELINE_VARIANT", None)
        else:
            os.environ["FAKE_PIPELINE_VARIANT"] = old
    pkg = harness.make_module("src", pipeline=mod)
    pkg.__path__ = []
    return {"src": pkg, "src.pipeline": mod}


class FakeMlflowException(Exception):
    pass


def fake_mlflow(versions: dict, aliases: dict | None = None):
    """In-memory registry for MODEL_NAME: ``versions`` maps version -> (model, git_sha)."""
    aliases = {} if aliases is None else aliases
    calls = []

    class MlflowClient:
        def search_model_versions(self, filter_string=None, max_results=10000, order_by=None, page_token=None):
            calls.append(("search", filter_string))
            name = re.search(r"name = '([^']+)'", filter_string).group(1)
            sha = re.search(r"tags\.git_sha = '([^']+)'", filter_string).group(1)
            return [SimpleNamespace(name=name, version=v) for v, (_, tag) in versions.items() if tag == sha]

        def get_model_version_by_alias(self, name, alias):
            if alias not in aliases:
                raise FakeMlflowException(f"Registered model alias {alias} not found.")
            return SimpleNamespace(name=name, version=aliases[alias])

        def set_registered_model_alias(self, name, alias, version):
            calls.append(("alias", alias, version))
            aliases[alias] = version

    def load_model(model_uri, suppress_warnings=False, dst_path=None, model_config=None):
        calls.append(("load", model_uri))
        version = re.fullmatch(r"models:/[^/@]+/(\w+)", model_uri).group(1)
        return versions[version][0]

    mlflow = harness.make_module("mlflow", MlflowClient=MlflowClient,
                                 pyfunc=SimpleNamespace(load_model=load_model))
    exceptions = harness.make_module("mlflow.exceptions", MlflowException=FakeMlflowException)
    mlflow.exceptions = exceptions
    return {"mlflow": mlflow, "mlflow.exceptions": exceptions}, aliases, calls


def trained(kind: str):
    """A model trained on the fake pipeline's data: 'good' learns, 'bad' predicts one class."""
    stubs = fake_pipeline("clean" if kind == "good" else "weak_model")
    mod = stubs["src.pipeline"]
    X_train, _, y_train, _ = mod.split(mod.load_training_data())
    return mod.train_model(X_train, y_train)


def outcomes(ns) -> dict[str, str]:
    result = {}
    for name in TEST_NAMES:
        try:
            ns[name]()
            result[name] = "pass"
        except Exception:  # pytest reports any exception in a test as a failure
            result[name] = "fail"
    return result


def yaml_block(contains: str):
    import yaml

    skill = harness.sl.load_skill(harness.REPO_ROOT / SLUG)
    (code,) = [c for _, c in skill.code_blocks(lang="yaml") if contains in c]
    return code, yaml.safe_load(code)


@harness.requires("pytest", "pandas", "numpy", "sklearn")
class TestCiCdForMlBlocks(unittest.TestCase):
    def run_variant(self, variant: str):
        return harness.run_blocks(SLUG, PYTEST_BLOCKS, stubs=fake_pipeline(variant))

    def test_clean_pipeline_passes_every_test(self):
        self.assertEqual(set(outcomes(self.run_variant("clean")).values()), {"pass"})

    def test_each_defect_is_caught_by_the_right_test(self):
        expected = {
            "leaky": "test_no_target_leakage_into_features",
            "dtype_drift": "test_schema_matches_contract",
            "weak_model": "test_model_beats_quality_bar",
        }
        for variant, failing in expected.items():
            with self.subTest(variant=variant):
                got = outcomes(self.run_variant(variant))
                self.assertEqual(got[failing], "fail", got)

    def test_original_leakage_test_is_a_tautology(self):
        stubs = fake_pipeline("leaky")
        ns: dict = {}
        with harness.stub_modules(stubs):
            exec(compile(ORIGINAL_LEAKAGE_TEST, "original test_data_contract.py", "exec"), ns)  # noqa: S102
            ns["test_no_target_leakage_into_features"]()  # passes despite a copy of the target
        self.assertIn("churn_flag_copy", stubs["src.pipeline"].FEATURE_COLUMNS)

    def test_seed_fixture_makes_global_rngs_repeatable(self):
        import random

        import numpy as np

        ns = harness.run_blocks(SLUG, [0])
        seed = ns["_seed_everything"].__wrapped__ if hasattr(ns["_seed_everything"], "__wrapped__") else None
        self.assertEqual(ns["SEED"], 42)
        if seed is None:
            reason = "pytest fixture object does not expose the wrapped function"
            if os.environ.get(harness.REQUIRE_ENV) == "1":   # pinned pytest exposes it: not a skip in CI
                import pytest

                self.fail(f"{harness.REQUIRE_ENV}=1 but {reason} (pytest {pytest.__version__})")
            self.skipTest(reason)
        seed()
        a = (random.random(), np.random.rand())
        seed()
        self.assertEqual(a, (random.random(), np.random.rand()))

    def test_blocks_run_under_real_pytest(self):
        """Write the blocks out as a project and run pytest on it (in-process: a fresh
        interpreter re-importing sklearn/pandas costs tens of seconds on a busy machine).
        sys.path / sys.modules are restored so the temp project leaves nothing behind."""
        import pytest

        blocks = harness.python_blocks(SLUG)

        class Outcomes:
            def __init__(self):
                self.passed, self.failed = [], []

            def pytest_runtest_logreport(self, report):
                if report.when == "call":
                    (self.passed if report.passed else self.failed).append(report.nodeid)

        saved_path, saved_modules = list(sys.path), set(sys.modules)
        saved_cwd, saved_variant = os.getcwd(), os.environ.get("FAKE_PIPELINE_VARIANT")
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "src").mkdir()
            (root / "tests").mkdir()
            (root / "src" / "__init__.py").write_text("", encoding="utf-8")
            (root / "src" / "pipeline.py").write_text(FAKE_PIPELINE_SRC, encoding="utf-8")
            (root / "conftest.py").write_text(blocks[0], encoding="utf-8")
            (root / "tests" / "test_data_contract.py").write_text(blocks[1], encoding="utf-8")
            (root / "tests" / "test_model_quality.py").write_text(blocks[2], encoding="utf-8")
            plugin = Outcomes()
            try:
                os.chdir(root)
                os.environ["FAKE_PIPELINE_VARIANT"] = "clean"
                code = pytest.main(["-q", "-p", "no:cacheprovider", "--rootdir", str(root), "tests/"],
                                   plugins=[plugin])
            finally:
                os.chdir(saved_cwd)
                if saved_variant is None:
                    os.environ.pop("FAKE_PIPELINE_VARIANT", None)
                else:
                    os.environ["FAKE_PIPELINE_VARIANT"] = saved_variant
                sys.path[:] = saved_path
                for name in set(sys.modules) - saved_modules:
                    mod = sys.modules.get(name)
                    if getattr(mod, "__file__", None) and str(root) in str(mod.__file__):
                        del sys.modules[name]
                    elif name in ("src", "src.pipeline", "conftest"):
                        sys.modules.pop(name, None)
        self.assertEqual(int(code), 0, plugin.failed)
        self.assertEqual(len(plugin.passed), 5, plugin.passed)


@harness.requires("pandas", "numpy", "sklearn")
class TestReleaseScoresTheArtifactItPromotes(unittest.TestCase):
    def release(self, versions, aliases=None, git_sha=SHA):
        stubs, aliases, calls = fake_mlflow(versions, aliases)
        ns = harness.run_blocks(SLUG, [RELEASE_BLOCK], stubs={**fake_pipeline("clean"), **stubs})
        return ns, aliases, calls, lambda: ns["release"](git_sha)

    def test_good_candidate_is_scored_then_promoted(self):
        _, aliases, calls, run = self.release({"1": (trained("good"), "f" * 40), "2": (trained("good"), SHA)},
                                              {"champion": "1"})
        self.assertEqual(run(), "2")
        self.assertEqual(aliases["champion"], "2")
        loads = [c[1] for c in calls if c[0] == "load"]
        self.assertIn("models:/churn-model/2", loads)          # the promoted version was scored
        self.assertIn("models:/churn-model/1", loads)          # ... against the champion
        self.assertLess(calls.index(("load", "models:/churn-model/2")), calls.index(("alias", "champion", "2")))

    def test_bad_registered_version_is_refused_although_the_pytest_gate_passes(self):
        # The pre-fix hazard: CI's own freshly trained model clears the bar...
        self.assertEqual(set(outcomes(harness.run_blocks(SLUG, PYTEST_BLOCKS, stubs=fake_pipeline("clean"))).values()),
                         {"pass"})
        # ...while the version registered for this commit is broken.
        _, aliases, _, run = self.release({"1": (trained("good"), "f" * 40), "2": (trained("bad"), SHA)},
                                          {"champion": "1"})
        with self.assertRaisesRegex(SystemExit, "below|< bar"):
            run()
        self.assertEqual(aliases["champion"], "1")

    def test_regression_against_the_champion_is_refused(self):
        ns, aliases, _, run = self.release({"1": (trained("good"), "f" * 40), "2": (trained("good"), SHA)},
                                           {"champion": "1"})
        real = ns["holdout_f1"]
        ns["holdout_f1"] = lambda uri, X, y: real(uri, X, y) - (0.05 if uri.endswith("/2") else 0.0)
        with self.assertRaisesRegex(SystemExit, "vs champion"):
            run()
        self.assertEqual(aliases["champion"], "1")

    def test_first_release_and_lookup_failures(self):
        _, aliases, _, run = self.release({"1": (trained("good"), SHA)})
        self.assertEqual(run(), "1")                            # no champion yet
        _, _, _, run = self.release({"1": (trained("good"), "f" * 40)})
        with self.assertRaisesRegex(SystemExit, "found 0"):
            run()
        _, _, calls, run = self.release({"1": (trained("good"), SHA)}, git_sha="x' OR name != '")
        with self.assertRaises(ValueError):
            run()
        self.assertEqual(calls, [], "an invalid SHA must not reach the registry filter")


@harness.slow("MLflow import + SQLite store setup")
@harness.requires("mlflow", "pandas", "numpy", "sklearn")
class TestReleaseAgainstRealMlflow(unittest.TestCase):
    """The step-5 block against real MLflow: registry search by tag, alias lookup and
    set, and pyfunc loading of the exact version."""

    def test_release_promotes_only_the_scored_version(self):
        import mlflow
        import mlflow.sklearn

        tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)  # sqlite file lock on Windows
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        # sqlite:/// stores default to a RELATIVE ./mlruns artifact root: run inside the
        # temp dir so log_model cannot write into the repo (cleanups run LIFO, so the
        # chdir back happens before tmp.cleanup).
        self.addCleanup(os.chdir, os.getcwd())
        os.chdir(root)
        uri = f"sqlite:///{(root / 'mlflow.db').as_posix()}"
        env = mock.patch.dict(os.environ, {"MLFLOW_TRACKING_URI": uri, "MLFLOW_DISABLE_AGENT_HINT": "1"})
        env.start()
        self.addCleanup(env.stop)
        no_infer = mock.patch("mlflow.models.infer_pip_requirements",
                              lambda path, flavor, fallback=None, **kw: list(fallback or []))
        no_infer.start()
        self.addCleanup(no_infer.stop)
        self.addCleanup(mlflow.set_tracking_uri, None)
        mlflow.set_tracking_uri(uri)
        mlflow.set_experiment("release-test")

        def register(model, sha):
            with mlflow.start_run():
                info = mlflow.sklearn.log_model(model, name="model")
            return mlflow.register_model(info.model_uri, "churn-model", tags={"git_sha": sha}).version

        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            champion = register(trained("good"), "f" * 40)
            bad = register(trained("bad"), "e" * 40)
            good = register(trained("good"), SHA)
            client = mlflow.MlflowClient()
            client.set_registered_model_alias("churn-model", "champion", champion)
            ns = harness.run_blocks(SLUG, [RELEASE_BLOCK], stubs=fake_pipeline("clean"), workdir=root)
            with self.assertRaises(SystemExit):
                ns["release"]("e" * 40)
            self.assertEqual(client.get_model_version_by_alias("churn-model", "champion").version, champion)
            self.assertEqual(ns["release"](SHA), str(good))
        self.assertEqual(client.get_model_version_by_alias("churn-model", "champion").version, good)
        self.assertNotEqual(bad, good)


@harness.requires("yaml")
class TestCiCdForMlWorkflows(unittest.TestCase):
    def test_ci_workflow(self):
        raw, wf = yaml_block("name: ml-ci")
        self.assertEqual(wf["permissions"], {"contents": "read"})
        job = wf["jobs"]["test"]
        self.assertIn("timeout-minutes", job)
        runs = [s["run"] for s in job["steps"] if "run" in s]
        self.assertTrue(any("--require-hashes" in r for r in runs))
        config = next(i for i, r in enumerate(runs) if "validate_pipeline_config" in r)
        tests = next(i for i, r in enumerate(runs) if r.startswith("pytest"))
        self.assertLess(config, tests, "the fail-fast config check must run before the test suite")
        audit = [r for r in runs if r.startswith("pip-audit")]
        self.assertEqual(len(audit), 1, "CI must audit the lockfile for known advisories")
        self.assertIn("-r requirements.txt", audit[0])
        self.assertIn("--disable-pip", audit[0])   # audit the hashed lockfile, don't re-resolve

    def test_example_pins_have_no_known_advisories(self):
        skill = harness.sl.load_skill(harness.REPO_ROOT / SLUG)
        (pins,) = [c for _, c in skill.code_blocks(lang="txt") if "requirements.in" in c]
        pinned = dict(re.findall(r"^([A-Za-z0-9_.-]+)==([0-9.]+)\s*$", pins, re.M))
        self.assertIn("pip-audit", pinned, "the audit tool belongs in the hashed lockfile too")
        for name, floor in MIN_ADVISORY_FREE.items():
            version = tuple(int(x) for x in pinned[name].split("."))
            self.assertGreaterEqual(version, floor, f"{name}=={pinned[name]} has published advisories")

    def test_release_workflow(self):
        raw, wf = yaml_block("name: ml-release")
        self.assertEqual(wf["permissions"], {"contents": "read"})
        self.assertIn("concurrency", wf)
        promote = wf["jobs"]["promote"]
        self.assertEqual(promote["needs"], "quality-gate")
        self.assertEqual(promote["environment"], "production")
        uses = [s.get("uses", "") for s in promote["steps"]]
        self.assertTrue(any(u.startswith("actions/setup-python") for u in uses),
                        "promote runs python, so it must set it up and install deps")
        self.assertTrue(any("pip install" in s.get("run", "") for s in promote["steps"]))
        self.assertNotIn("--stage", raw)  # MLflow registry stages are deprecated
        # the promote step must run a module the skill defines, and that module must score
        # the version before it moves the alias
        (cmd,) = [s["run"] for s in promote["steps"] if "--alias" in s.get("run", "")]
        module = re.search(r"python -m ([\w.]+)", cmd).group(1)
        src = harness.python_blocks(SLUG)[RELEASE_BLOCK]
        self.assertTrue(src.startswith(f"# {module.replace('.', '/')}.py"), (module, src[:60]))
        self.assertLess(src.index("holdout_f1(f\"models:/"), src.index("set_registered_model_alias"))
        for job in wf["jobs"].values():
            self.assertIn("timeout-minutes", job)
            for step in job["steps"]:
                self.assertNotIn("secrets.", step.get("run", ""), "secrets go through env:, not inline")


if __name__ == "__main__":
    unittest.main()
