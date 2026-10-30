"""Executes the python blocks (and the reproduce CLI commands) of experiment-tracking/SKILL.md.

Real libraries: MLflow (tracking, autolog, registry, CLI) against a SQLite store in a
temp dir, scikit-learn, matplotlib (Agg backend). The skill assumes the caller already
has a train/test split, so a tiny ``make_classification`` split is injected as
``X_train``/``X_test``/``y_train``/``y_test``.

What is stubbed or replaced, and why:

* ``MLFLOW_TRACKING_URI`` is set to an absolute ``sqlite:///<tmp>/mlflow.db`` so the
  skill's env-var lookup (step 1) never touches a server or writes into the repo.
* ``mlflow.models.infer_pip_requirements`` is patched to return MLflow's own fallback
  list. Real inference scans every installed distribution's file list; on the Windows
  dev box that alone took ~100 s per logged model. Requirement inference is not under test.
* ``wandb`` is a fake module (the real SDK needs an account or spawns a background
  service). The fake mirrors wandb 0.31.0's ``Run.__exit__`` (read from the wheel
  source): it finishes the run with ``exit_code=1`` on an exception and re-raises.
* ``replace=`` only shrinks ``n_estimators`` 200 -> 10.

Slow, so opt-in: MLflow import, SQLite store migrations and autolog patching cost ~100 s
on the dev box, and MLflow is often installed in the default interpreter, so
``@harness.requires`` alone would not keep it out of the default suite. The class runs
when ``ML_AI_SKILLS_SLOW_EXAMPLES=1`` or ``ML_AI_SKILLS_REQUIRE_EXAMPLE_DEPS=1`` (the CI
examples job, which must not skip silently); otherwise it is skipped with that reason.

Regression tests (each failed against the pre-fix skill, observed 2026-10-27): step 2
logged a ``confusion_matrix.png`` it never created (FileNotFoundError); step 3 could
pick a FAILED run as "best"; step 4 used ``transition_model_version_stage``
(deprecated since MLflow 2.9.0) instead of an alias; the W&B block left the run
unfinished when logging raised. Observed 2026-10-28: the serialization gotcha called
cloudpickle MLflow's sklearn default, but on mlflow 3.16.1 only ``autolog`` defaults to
it (``log_model``/``save_model`` default to skops); the defaults are now checked against
``inspect.signature``, and the gotcha's skops advice is executed. On mlflow 3.17.2 (the
CI pin) the ``log_model``/``save_model`` signature default became ``None``, resolved at
call time, so the test also logs a model without the argument and checks what was written.
"""
from __future__ import annotations

import os
import shlex
import tempfile
import unittest
import warnings
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from examples import harness

SLUG = "experiment-tracking"
# Block indices (document order): 0 tracking URI, 1 instrumented run, 2 compare,
# 3 register, 4 W&B alternative.
FAST = {1: {'"n_estimators": 200': '"n_estimators": 10'}}


def _split():
    from sklearn.datasets import make_classification
    from sklearn.model_selection import train_test_split

    X, y = make_classification(n_samples=160, n_features=6, n_informative=4, random_state=0)
    X_train, X_test, y_train, y_test = train_test_split(X, y, test_size=0.25, random_state=0)
    return {"X_train": X_train, "X_test": X_test, "y_train": y_train, "y_test": y_test}


def _fake_wandb(fail_on_log=False):
    runs = []

    class Run:
        def __init__(self, **kwargs):
            self.init_kwargs, self.logged, self.exit_code = kwargs, [], None

        def log(self, data):
            if fail_on_log:
                raise RuntimeError("simulated network error while logging")
            self.logged.append(data)

        def finish(self, exit_code=0):
            self.exit_code = exit_code

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            self.finish(exit_code=1 if exc_type else 0)

    def init(**kwargs):
        runs.append(Run(**kwargs))
        return runs[-1]

    def module_log(data):  # module-level wandb.log -> the current run, as in the real SDK
        runs[-1].log(data)

    def image(path):
        if not os.path.exists(path):  # stricter than needed: catches a missing step-2 artifact
            raise FileNotFoundError(path)
        return SimpleNamespace(path=path)

    return harness.make_module("wandb", init=init, log=module_log, Image=image), runs


@harness.slow("~100 s of MLflow setup")
@harness.requires("mlflow", "sklearn", "matplotlib", "pandas")
class TestExperimentTrackingSkill(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import matplotlib

        matplotlib.use("Agg")
        import mlflow

        cls.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)  # sqlite file may stay locked on Windows
        cls.workdir = Path(cls.tmp.name)
        # With a sqlite:/// store MLflow's default artifact root is the RELATIVE ./mlruns,
        # so any log_model outside run_blocks would write into the caller's cwd (the repo).
        cls._old_cwd = os.getcwd()
        os.chdir(cls.workdir)
        cls.uri = f"sqlite:///{(cls.workdir / 'mlflow.db').as_posix()}"
        cls.env = mock.patch.dict(os.environ, {"MLFLOW_TRACKING_URI": cls.uri, "MLFLOW_DISABLE_AGENT_HINT": "1"})
        cls.env.start()
        cls.no_infer = mock.patch("mlflow.models.infer_pip_requirements",
                                  lambda path, flavor, fallback=None, **kw: list(fallback or []))
        cls.no_infer.start()

        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            ns = harness.run_blocks(SLUG, [0], namespace=_split(), workdir=cls.workdir)
            # A crashed run that logged a better-looking metric before dying (status FAILED).
            try:
                with mlflow.start_run(run_name="crashed"):
                    mlflow.log_metric("f1_macro", 1.0)
                    raise RuntimeError("simulated OOM")
            except RuntimeError:
                pass
            cls.ns = harness.run_blocks(SLUG, [1, 2, 3], namespace=ns, replace=FAST, workdir=cls.workdir)
        cls.warnings = [str(w.message) for w in caught]

    @classmethod
    def tearDownClass(cls):
        import mlflow

        mlflow.sklearn.autolog(disable=True)
        while mlflow.active_run():
            mlflow.end_run()
        cls.no_infer.stop()
        cls.env.stop()
        mlflow.set_tracking_uri(None)
        os.chdir(cls._old_cwd)  # before cleanup: Windows cannot delete the cwd
        cls.tmp.cleanup()

    def test_tracking_uri_comes_from_environment(self):
        import mlflow

        self.assertEqual(mlflow.get_tracking_uri(), self.uri)

    def test_run_logs_params_metrics_and_confusion_matrix(self):
        import mlflow

        run = mlflow.get_run(self.ns["run"].info.run_id)
        self.assertEqual(run.info.status, "FINISHED")
        self.assertEqual(run.data.params["n_estimators"], "10")
        self.assertEqual(run.data.tags["dataset_version"], "v3")
        for key in ("accuracy", "f1_macro"):
            self.assertIn(key, run.data.metrics)
        artifacts = {a.path for a in mlflow.MlflowClient().list_artifacts(run.info.run_id)}
        self.assertIn("confusion_matrix.png", artifacts)

    def test_best_run_ignores_failed_runs(self):
        import mlflow

        # The hazard: without the status filter the crashed run ranks first.
        unfiltered = mlflow.search_runs(experiment_names=["churn-prediction"],
                                        order_by=["metrics.f1_macro DESC"], max_results=1)
        self.assertEqual(unfiltered.iloc[0]["status"], "FAILED")
        runs = self.ns["runs"]
        self.assertEqual(self.ns["best_run_id"], self.ns["run"].info.run_id)
        self.assertNotIn("crashed", set(runs.get("tags.mlflow.runName", [])))

    def test_best_model_registered_under_alias_not_deprecated_stage(self):
        import mlflow

        client = mlflow.MlflowClient()
        mv = client.get_model_version_by_alias("churn-rf-classifier", "staging")
        self.assertEqual(mv.version, self.ns["result"].version)
        self.assertEqual(mv.run_id, self.ns["best_run_id"])
        self.assertFalse([w for w in self.warnings if "transition_model_version_stage" in w])
        # Our own freshly-logged model: trusted, so unpickling it here is fine.
        model = mlflow.pyfunc.load_model("models:/churn-rf-classifier@staging")
        self.assertEqual(len(model.predict(self.ns["X_test"][:3])), 3)

    def test_serialization_defaults_match_the_gotcha(self):
        import inspect

        import mlflow
        import mlflow.sklearn

        def default(fn):
            return inspect.signature(fn).parameters["serialization_format"].default

        text = (harness.REPO_ROOT / SLUG / "SKILL.md").read_text(encoding="utf-8")
        gotcha = " ".join(text.split("**Loading an sklearn model logged as")[1].split("\n- ")[0].split())
        log_default = default(mlflow.sklearn.log_model)
        self.assertEqual(log_default, default(mlflow.sklearn.save_model))
        shown = "None" if log_default is None else f'"{log_default}"'  # None: resolved at call time
        self.assertIn(f"`save_model` default to `serialization_format={shown}`", gotcha)
        self.assertIn(f'`mlflow.sklearn.autolog` defaults to `"{default(mlflow.sklearn.autolog)}"`', gotcha)
        # The format log_model actually writes with no argument (outside Databricks)
        from sklearn.linear_model import LogisticRegression

        clf = LogisticRegression(max_iter=200).fit(self.ns["X_train"], self.ns["y_train"])
        with mlflow.start_run(run_name="default-format"):
            logged = mlflow.sklearn.log_model(clf, name="model")
        written = mlflow.models.get_model_info(logged.model_uri).flavors["sklearn"]["serialization_format"]
        self.assertEqual(written, "skops")
        if log_default is None:
            self.assertIn(f'which resolves to `"{written}"`', gotcha)
        # ...and step 2's autologged model really is a pickle, as the gotcha says
        info = mlflow.models.get_model_info("models:/churn-rf-classifier@staging")
        self.assertEqual(info.flavors["sklearn"]["serialization_format"], default(mlflow.sklearn.autolog))

    def test_gotcha_skops_advice_for_tree_models(self):
        import mlflow
        import mlflow.sklearn
        from sklearn.ensemble import RandomForestClassifier

        split = _split()
        clf = RandomForestClassifier(n_estimators=5, random_state=0)
        try:
            # autolog(serialization_format="skops"): the forest's Tree type is untrusted, so
            # the run's logged model ends up FAILED, with only a warning (no exception)
            mlflow.sklearn.autolog(log_models=True, serialization_format="skops", silent=True)
            with mlflow.start_run() as run:
                clf.fit(split["X_train"], split["y_train"])
            models = mlflow.search_logged_models(
                experiment_ids=[run.info.experiment_id],
                filter_string=f"source_run_id = '{run.info.run_id}'", output_format="list")
            self.assertEqual([str(m.status) for m in models], ["FAILED"])
        finally:
            mlflow.sklearn.autolog(disable=True)
        # the gotcha's alternative: explicit log_model with the Tree type trusted
        with mlflow.start_run():
            info = mlflow.sklearn.log_model(clf, name="model", serialization_format="skops",
                                            skops_trusted_types=["sklearn.tree._tree.Tree"])
        flavor = mlflow.models.get_model_info(info.model_uri).flavors["sklearn"]
        self.assertEqual(flavor["serialization_format"], "skops")
        self.assertEqual(flavor["skops_trusted_types"], ["sklearn.tree._tree.Tree"])  # stored in MLmodel
        self.assertEqual(len(mlflow.pyfunc.load_model(info.model_uri).predict(split["X_test"][:3])), 3)

    def test_reproduce_cli_commands_run(self):
        from click.testing import CliRunner
        from mlflow.cli import cli

        bash = [code for _, code in harness.sl.load_skill(harness.REPO_ROOT / SLUG).code_blocks(lang="bash")]
        commands = [line.split("#")[0].strip() for line in bash[0].splitlines()]
        commands = [c for c in commands if c.startswith("mlflow ")]
        self.assertEqual(len(commands), 3)
        self.assertTrue(any("MLFLOW_TRACKING_URI" in line for line in bash[0].splitlines()),
                        "CLI ignores set_tracking_uri(); the block must export the store URI")
        old = os.getcwd()
        os.chdir(self.workdir)
        try:
            for cmd in commands:
                argv = shlex.split(cmd.replace("<best_run_id>", self.ns["best_run_id"]))[1:]
                result = CliRunner().invoke(cli, argv, env={"MLFLOW_TRACKING_URI": self.uri})
                self.assertEqual(result.exit_code, 0, f"{cmd}: {result.output}\n{result.exception!r}")
            out = self.workdir / "reproduced"
            self.assertTrue((out / "confusion_matrix.png").exists())
            # MLflow 3: the model + its environment come from the models:/ URI, not the run.
            for name in ("MLmodel", "requirements.txt", "python_env.yaml"):
                self.assertTrue((out / "model" / name).exists(), name)
        finally:
            os.chdir(old)

    def test_wandb_block_logs_and_finishes(self):
        wandb, runs = _fake_wandb()
        harness.run_blocks(SLUG, [4], namespace=dict(self.ns), stubs={"wandb": wandb}, workdir=self.workdir)
        self.assertEqual(len(runs), 1)
        self.assertEqual(runs[0].exit_code, 0)
        logged = {k for d in runs[0].logged for k in d}
        self.assertEqual(logged, {"accuracy", "f1_macro", "confusion_matrix"})

    def test_wandb_run_is_finished_as_failed_when_logging_raises(self):
        wandb, runs = _fake_wandb(fail_on_log=True)
        with self.assertRaises(RuntimeError):
            harness.run_blocks(SLUG, [4], namespace=dict(self.ns), stubs={"wandb": wandb}, workdir=self.workdir)
        self.assertEqual(runs[0].exit_code, 1)


if __name__ == "__main__":
    unittest.main()
