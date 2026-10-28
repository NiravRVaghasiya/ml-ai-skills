"""Executable tests for ml-monitoring/SKILL.md.

All six python blocks run verbatim through harness.run_blocks in one temp
working directory. What is stubbed / injected, and why:

* ``holdout_reference.csv`` / ``ground_truth.csv`` — written by the test from a
  small synthetic distribution (the skill documents them as caller-provided).
  The production log is produced by calling the skill's own ``log_prediction``.
* ``evidently`` — real (0.7.x). Telemetry env vars are set as a belt-and-braces
  guard; evidently's telemetry lives in its UI/collector, which is not used.
* ``requests.post`` — patched with a recorder: alerts must never hit the network
  in tests. The real ``requests`` is still used to show the pre-fix default
  ``webhook_url=""`` raising ``MissingSchema`` (raised before any I/O).
* ``airflow`` — not installed in the example env; ``fake_airflow`` provides a
  ``DAG`` context manager and ``PythonOperator`` whose constructor accepts only
  keyword arguments present in Airflow 3's task-sdk ``DAG`` (``schedule``, not
  ``schedule_interval`` — read from apache-airflow-task-sdk 1.3.2 source). It
  is a signature check, not an Airflow integration test.
* ``monitoring`` — the DAG block imports the step 1-5 helpers from a
  ``monitoring`` module (as the skill tells readers to lay out their repo). The
  test builds that module from blocks 0-4's namespace and runs the DAG block in
  a *fresh* namespace, so a helper the DAG forgets to import fails as NameError.

Regression tests replay the pre-fix code (ml-monitoring/SKILL.md at commit
8328c60, quoted verbatim below) to demonstrate each defect.
"""
from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from examples import harness

SLUG = "ml-monitoring"
N = 300

ORIGINAL_LOGGER = '''
import os
import pandas as pd
from datetime import datetime, timezone

LOG_PATH = "prediction_log.csv"

def log_prediction(features: dict, prediction: float, model_version: str) -> None:
    row = {**features, "prediction": prediction, "model_version": model_version,
           "logged_at": datetime.now(timezone.utc).isoformat()}
    pd.DataFrame([row]).to_csv(
        LOG_PATH, mode="a", header=not os.path.exists(LOG_PATH), index=False,
    )
'''

ORIGINAL_EVIDENTLY_IMPORTS = '''
from evidently.report import Report
from evidently.metric_preset import DataDriftPreset
'''

ORIGINAL_PERF = '''
from sklearn.metrics import f1_score

def compute_delayed_performance(pred_log: pd.DataFrame, labels: pd.DataFrame) -> float:
    joined = pred_log.merge(labels, on="request_id", how="inner")
    return f1_score(joined["y_true"], joined["prediction"], average="macro")
'''

ORIGINAL_ALERT = '''
import requests

def alert_if_drifted(drift_share: float, threshold: float = 0.3, webhook_url: str = "") -> None:
    if drift_share > threshold:
        requests.post(webhook_url, json={
            "text": f":rotating_light: Data drift alert: {drift_share:.0%} of "
                    f"features drifted (threshold {threshold:.0%})."
        })
'''

ORIGINAL_DAG_KWARGS = dict(dag_id="model_drift_monitoring", schedule_interval="@daily", catchup=False)


def fake_airflow(registry: list):
    class DAG:
        def __init__(self, *, dag_id, schedule=None, start_date=None, catchup=None,
                     default_args=None, **unknown):
            if unknown:  # Airflow 3's DAG is an attrs class: unknown kwargs are a TypeError
                raise TypeError(f"DAG.__init__() got unexpected keyword arguments {sorted(unknown)}")
            self.dag_id, self.schedule, self.default_args, self.tasks = dag_id, schedule, default_args, []
            registry.append(self)

        def __enter__(self):
            DAG.current = self
            return self

        def __exit__(self, *exc):
            DAG.current = None
            return False

    class PythonOperator:
        def __init__(self, *, task_id, python_callable):
            self.task_id, self.python_callable = task_id, python_callable
            DAG.current.tasks.append(self)

    airflow = harness.make_module("airflow", DAG=DAG)
    operators = harness.make_module("airflow.operators")
    python = harness.make_module("airflow.operators.python", PythonOperator=PythonOperator)
    airflow.operators, operators.python = operators, python
    return {"airflow": airflow, "airflow.operators": operators, "airflow.operators.python": python}


def monitoring_module(ns: dict):
    """The ``monitoring.py`` the DAG imports, built from the step 1-5 blocks' namespace."""
    names = ("LOG_PATH", "NON_FEATURES", "alert_if_drifted", "drift_share_for", "send_alert")
    return harness.make_module("monitoring", **{n: ns[n] for n in names})


class _Recorder:
    def __init__(self, status=200):
        self.calls, self.status = [], status

    def __call__(self, url, json=None, timeout=None, **kw):
        import requests

        self.calls.append({"url": url, "json": json, "timeout": timeout})
        resp = requests.Response()
        resp.status_code, resp.url = self.status, url
        return resp


def frames(shift_a: float = 2.0, shift_score: float = 0.4, seed: int = 0):
    import numpy as np
    import pandas as pd

    g = np.random.default_rng(seed)
    ref = pd.DataFrame({
        "a": g.normal(0, 1, N), "b": g.normal(0, 1, N), "plan": g.choice(["free", "pro"], N),
        "score": g.uniform(0, 0.6, N),
    })
    ref["prediction"] = (ref["score"] > 0.5).astype(int)
    ref["y_true"] = ref["prediction"]
    cur = pd.DataFrame({
        "a": g.normal(shift_a, 1, N), "b": g.normal(0, 1, N), "plan": g.choice(["free", "pro"], N),
        "score": g.uniform(shift_score, 0.6 + shift_score, N).clip(0, 1),
    })
    return ref, cur


@harness.requires("evidently", "pandas", "numpy", "sklearn", "requests")
class TestMlMonitoringBlocks(unittest.TestCase):
    """The drifted scenario runs once per class (evidently reports are the slow part)."""

    @classmethod
    def setUpClass(cls):
        cls._env = mock.patch.dict(os.environ, {"EVIDENTLY_DISABLE_TELEMETRY": "1", "DO_NOT_TRACK": "1"})
        cls._env.start()
        os.environ.pop("DRIFT_ALERT_WEBHOOK_URL", None)
        try:
            cls.ns, cls.workdir, cls.dags, cls._tmp = cls.run_skill()
        except BaseException:
            cls._env.stop()
            raise

    @classmethod
    def tearDownClass(cls):
        cls._tmp.cleanup()
        cls._env.stop()

    @staticmethod
    def in_dir(workdir, fn, *a, **kw):
        cwd = os.getcwd()
        os.chdir(workdir)
        try:
            return fn(*a, **kw)
        finally:
            os.chdir(cwd)

    def in_workdir(self, fn, *a, **kw):
        return self.in_dir(self.workdir, fn, *a, **kw)

    @classmethod
    def run_skill(cls, shift_a=2.0, shift_score=0.4):
        import pandas as pd

        tmp = tempfile.TemporaryDirectory()
        workdir = Path(tmp.name)
        try:
            ref, cur = frames(shift_a, shift_score)
            ref.to_csv(workdir / "holdout_reference.csv", index=False)
            ns = harness.run_blocks(SLUG, [0], workdir=workdir)
            ids = []
            for row in cur.to_dict("records"):
                score = row.pop("score")
                ids.append(cls.in_dir(workdir, ns["log_prediction"], row, int(score > 0.5), score, "v3"))
            labels = pd.DataFrame({"request_id": ids, "y_true": (cur["score"] > 0.55).astype(int)})
            labels.to_csv(workdir / "ground_truth.csv", index=False)
            ns = harness.run_blocks(SLUG, [1, 2, 3, 4], namespace=ns, workdir=workdir)
            dags: list = []
            harness.run_blocks(SLUG, [5], workdir=workdir,
                               stubs={**fake_airflow(dags), "monitoring": monitoring_module(ns)})
        except BaseException:
            tmp.cleanup()   # setUpClass failures skip tearDownClass; don't leak the directory
            raise
        return ns, workdir, dags, tmp

    # ---------------------------------------------------------------- end to end
    def test_drift_detected_and_reported(self):
        ns = self.ns
        self.assertAlmostEqual(ns["drift_share"], 1 / 3)  # only "a" of a, b, plan shifted
        self.assertTrue(ns["pred_drift_detected"])
        self.assertTrue((self.workdir / "data_drift_report.html").exists())
        self.assertTrue((self.workdir / "prediction_drift_report.html").exists())
        self.assertIsNotNone(ns["current_f1"])
        self.assertEqual(ns["feature_cols"], ["a", "b", "plan"])

    def test_no_drift_on_same_distribution(self):
        ns, _, _, tmp = self.run_skill(shift_a=0.0, shift_score=0.0)
        self.addCleanup(tmp.cleanup)
        self.assertEqual(ns["drift_share"], 0.0)
        self.assertFalse(ns["pred_drift_detected"])

    def test_drift_inputs_are_validated(self):
        ns = self.ns
        ref, cur = ns["reference"], ns["current"]
        with self.assertRaisesRegex(ValueError, "lacks reference columns"):
            ns["drift_share_for"](ref, cur.drop(columns=["b"]), ["a", "b"])
        with self.assertRaisesRegex(ValueError, "rows per window"):
            ns["drift_share_for"](ref, cur.head(10), ["a"])
        with self.assertRaisesRegex(ValueError, "collide"):
            ns["log_prediction"]({"score": 1.0}, 1, 0.9, "v3")

    def test_delayed_performance_guards(self):
        import pandas as pd

        ns = self.ns
        log, labels = ns["pred_log"], ns["ground_truth"]
        self.assertIsNone(ns["compute_delayed_performance"](log, labels.head(5)))
        with self.assertRaises(pd.errors.MergeError):  # duplicated label rows
            ns["compute_delayed_performance"](log, pd.concat([labels, labels.head(1)]))

    def test_alerting(self):
        ns = self.ns
        rec = _Recorder()
        with mock.patch.object(ns["requests"], "post", rec):
            self.assertFalse(ns["alert_if_drifted"](0.1, webhook_url="https://hooks.invalid/x"))
            self.assertEqual(rec.calls, [])
            self.assertTrue(ns["alert_if_drifted"](0.5, webhook_url="https://hooks.invalid/x"))
            self.assertEqual(rec.calls[-1]["timeout"], 10.0)
            with self.assertRaisesRegex(RuntimeError, "not configured"):
                ns["alert_if_drifted"](0.5)
            with self.assertRaises(ValueError):
                ns["alert_if_drifted"](float("nan"))
        with mock.patch.object(ns["requests"], "post", _Recorder(status=500)):
            with self.assertRaises(ns["requests"].HTTPError):
                ns["alert_if_drifted"](0.5, webhook_url="https://hooks.invalid/x")

    def test_dag_task_runs_the_checks(self):
        ns = self.ns
        (dag,) = self.dags
        self.assertEqual(dag.schedule, "@daily")
        (task,) = dag.tasks
        rec = _Recorder()
        with mock.patch.object(ns["requests"], "post", rec), \
                mock.patch.dict(os.environ, {"DRIFT_ALERT_WEBHOOK_URL": "https://hooks.invalid/x"}):
            self.in_workdir(task.python_callable)
        self.assertEqual(len(rec.calls), 2)  # one feature-drift and one prediction-drift alert

    def test_dag_file_is_self_contained(self):
        """The DAG block alone, with no ``monitoring`` module, must fail at import (DAG parse)
        time, not with a NameError inside the task days later. Fails on the pre-review
        version, whose helper import was commented out: the block imported cleanly and
        the task raised NameError: LOG_PATH."""
        with self.assertRaises(ModuleNotFoundError):
            harness.run_blocks(SLUG, [5], workdir=self.workdir, stubs=fake_airflow([]))

    def test_reference_scores_come_from_held_out_rows(self):
        """Pre-fix, both reference reads were ``train_reference.csv`` ("features + model
        outputs on them"). On undrifted traffic, a random forest's in-sample scores make the
        skill's own score-drift check fire; scores on held-out rows do not (observed
        2026-10-28 with evidently 0.7.23, matching the audit's 5,000-row repro)."""
        import re

        import pandas as pd
        from sklearn.datasets import make_classification
        from sklearn.ensemble import RandomForestClassifier

        reads = [m for b in harness.python_blocks(SLUG) for m in re.findall(r'read_csv\("([^"]+)"\)', b)]
        self.assertEqual(reads.count("holdout_reference.csv"), 2, reads)   # step 2 and the DAG
        self.assertNotIn("train_reference.csv", reads)

        n = 600
        X, y = make_classification(3 * n, 10, n_informative=5, flip_y=0.1, random_state=0)
        clf = RandomForestClassifier(50, random_state=0).fit(X[:n], y[:n])

        def scores(rows):
            return pd.DataFrame({"score": clf.predict_proba(rows)[:, 1]})

        production = scores(X[2 * n:])                                   # same distribution
        drift = self.ns["drift_share_for"]
        self.assertGreater(drift(scores(X[:n]), production, ["score"]), 0)       # fit rows
        self.assertEqual(drift(scores(X[n:2 * n]), production, ["score"]), 0.0)  # held out

    # ---------------------------------------------------------------- original defects
    def fresh_dir(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        return Path(tmp.name)

    def test_original_csv_logger_misaligns_reordered_features(self):
        import pandas as pd

        ns, d = {}, self.fresh_dir()
        exec(ORIGINAL_LOGGER, ns)  # noqa: S102 - pre-fix published code
        self.in_dir(d, ns["log_prediction"], {"a": 1.0, "b": 99.0}, 0.2, "v1")
        self.in_dir(d, ns["log_prediction"], {"b": 99.0, "a": 1.0}, 0.2, "v1")
        df = pd.read_csv(d / "prediction_log.csv")
        self.assertEqual(df["a"].tolist(), [1.0, 99.0])  # second row's "b" landed under "a"

    def test_original_join_key_was_never_logged(self):
        import pandas as pd

        ns, d = {"pd": pd}, self.fresh_dir()
        exec(ORIGINAL_LOGGER, ns)  # noqa: S102
        exec(ORIGINAL_PERF, ns)  # noqa: S102
        self.in_dir(d, ns["log_prediction"], {"a": 1.0}, 0.7, "v1")
        log = pd.read_csv(d / "prediction_log.csv")
        with self.assertRaises(KeyError):
            ns["compute_delayed_performance"](log, pd.DataFrame({"request_id": ["r1"], "y_true": [1]}))
        log["request_id"] = ["r1"]  # even with a key, a float "prediction" breaks F1
        with self.assertRaisesRegex(ValueError, "continuous"):
            ns["compute_delayed_performance"](log, pd.DataFrame({"request_id": ["r1"], "y_true": [1]}))

    def test_original_evidently_imports_fail_on_0_7(self):
        import evidently

        if not evidently.__version__.startswith("0.7"):
            self.skipTest("checks the 0.7 API break specifically")
        with self.assertRaises(ModuleNotFoundError):
            exec(ORIGINAL_EVIDENTLY_IMPORTS, {})  # noqa: S102

    def test_original_alert_default_url_raises_and_has_no_timeout(self):
        import requests

        ns = {}
        exec(ORIGINAL_ALERT, ns)  # noqa: S102
        with self.assertRaises(requests.exceptions.MissingSchema):
            ns["alert_if_drifted"](0.9)
        rec = _Recorder()
        with mock.patch.object(requests, "post", rec):
            ns["alert_if_drifted"](0.9, webhook_url="https://hooks.invalid/x")
        self.assertIsNone(rec.calls[0]["timeout"])

    def test_original_dag_kwargs_rejected_by_airflow3_signature(self):
        dag_cls = fake_airflow([])["airflow"].DAG
        with self.assertRaises(TypeError):
            dag_cls(**ORIGINAL_DAG_KWARGS)


if __name__ == "__main__":
    unittest.main()
