"""Executes the python blocks of data-pipelines/SKILL.md.

Real libraries: pandas + pyarrow (parquet), SQLAlchemy 2 against a SQLite file
(standing in for the warehouse via ``FEATURES_DB_URL``), pandera (the quality gate).
The task callables are invoked directly with an Airflow-style ``context``.

Stubbed, and why:

* ``airflow`` — not installable alongside the shared example venv (it would
  replace fastapi/packaging/otel pins) and not supported on Windows. The stub
  ``DAG`` accepts exactly the keyword fields of ``airflow.sdk.definitions.dag.DAG``
  in apache-airflow-task-sdk 1.3.2 (read from the wheel; an attrs class, so an
  unknown keyword such as ``schedule_interval`` is a TypeError) and the stub
  operator records ``>>`` edges, auto-attaching a DAG-less task to its
  neighbour's DAG like ``DependencyMixin._set_relatives`` does there. Like
  ``BaseOperator.__init__`` (sdk/bases/operator.py), it merges the DAG's
  ``default_args`` only at construction, from ``dag=`` or the enclosing ``with
  DAG``; a task attached later by ``>>`` gets none (``DAG.add_task`` copies only
  start/end dates), so it keeps ``retries=0`` and no failure callback.
* ``feast`` — installing feast 0.67.0 would downgrade tenacity/prometheus-client
  in the shared venv. The stub accepts only the keyword names of the real
  constructors (read from the feast 0.67.0 wheel), so a renamed/removed argument
  in the skill fails the test; it does not reproduce feature-store behaviour.

Task contexts mirror what the Airflow task runner passes for a ``@daily`` run
that fires at ``RUN_AT`` (2026-04-01 00:00 UTC):

* Airflow 3 default (``CronTriggerTimetable``): ``ds`` == the trigger date and a
  zero-width data interval ending at the trigger time (task-sdk 1.3.2
  ``task_runner.py`` sets ``ds = logical_date.strftime("%Y-%m-%d")``; core 3.3.2
  ``config.yml`` ``create_cron_data_intervals`` defaults to False; ``timetables/trigger.py``
  builds the interval as ``(run_time - interval, run_time)`` with interval 0).
* Airflow 2 / ``create_cron_data_intervals=True`` (``CronDataIntervalTimetable``):
  ``ds`` == the interval START (the previous day), ``data_interval_end`` == trigger time.

Regression tests (each failed against the pre-fix skill, observed 2026-10-27):
``schedule_interval`` (removed in Airflow 3); ``con.execute("... %s", ...)`` on
the SQLAlchemy/sqlite3 connections pandas supports; a "30d" feature computed from
a single day of orders; the delete+insert not being atomic; an empty feature file
passing the quality gate; the deprecated top-level pandera import; undefined
``get_db_connection``/``notify_slack_on_failure``; a window keyed on ``ds`` that,
under Airflow 3, includes the still-arriving trigger day and stamps
``event_timestamp`` a day in the future; a Feast ``FileSource`` pointing at a file
the pipeline never wrote; the quality-gate task built outside the DAG context, so
it ran without the DAG's retries and failure callback (observed 2026-10-28).
"""
from __future__ import annotations

import ast
import logging
import os
import subprocess
import sys
import tempfile
import unittest
import warnings
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from examples import harness

SLUG = "data-pipelines"
# Block indices (document order): 0 DAG + tasks, 1 pandera gate, 2 feast definitions,
# 3 online read, 4 failure callback / default_args.
RUN_AT = datetime(2026, 4, 1, tzinfo=timezone.utc)   # when the @daily run fires
AS_OF = "2026-04-01"                                 # partition = window end (exclusive)
IN_PROGRESS_AMOUNT = 1000.0                          # orders already in on the trigger day


def airflow3_context(run_after=RUN_AT):
    """CronTriggerTimetable (Airflow 3 default): logical date == trigger time, zero-width interval."""
    return {"ds": run_after.strftime("%Y-%m-%d"), "logical_date": run_after,
            "data_interval_start": run_after, "data_interval_end": run_after}


def airflow2_context():
    """CronDataIntervalTimetable (Airflow 2 default): ds == interval start, run fires at its end."""
    start = RUN_AT - timedelta(days=1)
    return {"ds": start.strftime("%Y-%m-%d"), "logical_date": start,
            "data_interval_start": start, "data_interval_end": RUN_AT}

# Keyword fields of airflow.sdk.definitions.dag.DAG.__init__ (apache-airflow-task-sdk 1.3.2).
AIRFLOW3_DAG_FIELDS = frozenset({
    "dag_id", "description", "default_args", "start_date", "end_date", "schedule",
    "template_searchpath", "template_undefined", "user_defined_macros", "user_defined_filters",
    "max_active_tasks", "max_active_runs", "max_consecutive_failed_dag_runs", "dagrun_timeout",
    "deadline", "sla_miss_callback", "catchup", "on_success_callback", "on_failure_callback",
    "doc_md", "params", "access_control", "is_paused_upon_creation", "jinja_environment_kwargs",
    "render_template_as_native_obj", "tags", "owner_links", "auto_register", "fail_fast",
    "allowed_run_types", "dag_display_name", "task_group", "disable_bundle_versioning",
    "rerun_with_latest_version",
})
# Keyword names of the real constructors/methods in feast 0.67.0.
FEAST_KWARGS = {
    "Entity": {"name", "join_keys", "value_type", "description", "tags", "owner"},
    "FileSource": {"path", "name", "event_timestamp_column", "file_format", "created_timestamp_column",
                   "field_mapping", "s3_endpoint_override", "description", "tags", "owner",
                   "timestamp_field", "connection_ref"},
    "FeatureView": {"name", "source", "sink_source", "schema", "entities", "ttl", "online", "offline",
                    "description", "tags", "owner", "org", "mode", "enable_validation", "version", "enabled"},
    "Field": {"name", "dtype", "description", "tags", "vector_index", "vector_length",
              "vector_search_metric"},
    "get_online_features": {"features", "entity_rows", "full_feature_names",
                            "include_feature_view_version_metadata"},
}


def _airflow_stubs():
    current = []

    class DAG:
        def __init__(self, dag_id, **kwargs):
            unknown = set(kwargs) - AIRFLOW3_DAG_FIELDS
            if unknown:
                raise TypeError(f"DAG.__init__() got an unexpected keyword argument {sorted(unknown)[0]!r}")
            self.dag_id, self.kwargs, self.tasks = dag_id, kwargs, {}

        def __enter__(self):
            current.append(self)
            return self

        def __exit__(self, *exc):
            current.pop()

    class PythonOperator:
        def __init__(self, task_id, python_callable, dag=None, **kwargs):
            self.task_id, self.python_callable = task_id, python_callable
            self.upstream, self.downstream, self.dag = set(), set(), None
            dag = dag if dag is not None else (current[-1] if current else None)
            # default_args are merged here or never (BaseOperator core defaults otherwise)
            merged = {**((dag.kwargs.get("default_args") or {}) if dag else {}), **kwargs}
            self.retries = merged.get("retries", 0)
            self.on_failure_callback = merged.get("on_failure_callback")
            if dag is not None:
                self._attach(dag)

        def _attach(self, dag):
            self.dag = dag
            dag.tasks[self.task_id] = self

        def __rshift__(self, other):
            dag = self.dag or other.dag
            for t in (self, other):
                if t.dag is None:
                    t._attach(dag)
            self.downstream.add(other.task_id)
            other.upstream.add(self.task_id)
            return other

    python_mod = harness.make_module("airflow.providers.standard.operators.python", PythonOperator=PythonOperator)
    legacy_mod = harness.make_module("airflow.operators.python", PythonOperator=PythonOperator)
    return {
        "airflow": harness.make_module("airflow", DAG=DAG),
        "airflow.providers": harness.make_module("airflow.providers"),
        "airflow.providers.standard": harness.make_module("airflow.providers.standard"),
        "airflow.providers.standard.operators": harness.make_module("airflow.providers.standard.operators"),
        "airflow.providers.standard.operators.python": python_mod,
        "airflow.operators": harness.make_module("airflow.operators"),
        "airflow.operators.python": legacy_mod,
    }


def _feast_stubs(calls):
    def recorder(kind):
        def make(*args, **kwargs):
            unknown = set(kwargs) - FEAST_KWARGS[kind]
            if args or unknown:
                raise TypeError(f"{kind}: positional args {args} / unknown keywords {sorted(unknown)}")
            calls.append((kind, kwargs))
            return SimpleNamespace(kind=kind, **kwargs)
        return make

    class FeatureStore:
        def __init__(self, repo_path):
            self.repo_path = repo_path

        def get_online_features(self, **kwargs):
            recorder("get_online_features")(**kwargs)
            rows = kwargs["entity_rows"]
            values = {f.split(":")[1]: [None] * len(rows) for f in kwargs["features"]}
            return SimpleNamespace(to_dict=lambda: {"customer_id": [r["customer_id"] for r in rows], **values})

    feast = harness.make_module(
        "feast", Entity=recorder("Entity"), FeatureView=recorder("FeatureView"), Field=recorder("Field"),
        FileSource=recorder("FileSource"), FeatureStore=FeatureStore)
    types_mod = harness.make_module("feast.types", Int64="Int64", Float32="Float32", Float64="Float64")
    return {"feast": feast, "feast.types": types_mod}


@harness.requires("pandas", "pyarrow", "sqlalchemy", "pandera")
class TestDataPipelinesSkill(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)  # sqlite file lock on Windows
        self.tmp = Path(self._tmp.name)
        self.db_url = f"sqlite:///{(self.tmp / 'warehouse.db').as_posix()}"
        env = mock.patch.dict(os.environ, {"FEATURES_DB_URL": self.db_url,
                                           "FEATURE_PIPELINE_DATA_DIR": self.tmp.as_posix()})
        env.start()
        self.addCleanup(env.stop)
        self.addCleanup(self._tmp.cleanup)
        self._make_warehouse()

    def _make_warehouse(self, amounts=None):
        import pandas as pd
        from sqlalchemy import create_engine, text

        # 40 complete days ending the day before the run fires, one order/day alternating
        # between 2 customers, plus two orders already in on the trigger day (still arriving).
        days = pd.date_range(end="2026-03-31", periods=40, freq="D").strftime("%Y-%m-%d")
        rows = [{"order_id": i, "customer_id": 1 + i % 2, "amount": float(amounts[i] if amounts else 10.0),
                 "order_date": d} for i, d in enumerate(days)]
        rows += [{"order_id": 100 + c, "customer_id": c, "amount": IN_PROGRESS_AMOUNT, "order_date": AS_OF}
                 for c in (1, 2)]
        engine = create_engine(self.db_url)
        with engine.begin() as con:
            con.execute(text("DROP TABLE IF EXISTS orders"))
            con.execute(text("DROP TABLE IF EXISTS customer_features"))
            pd.DataFrame(rows).to_sql("orders", con, index=False)
            con.execute(text("CREATE TABLE customer_features (customer_id INTEGER, orders_30d INTEGER, "
                             "spend_30d REAL, event_timestamp TIMESTAMP, as_of TEXT)"))
        engine.dispose()
        self.days = list(days)

    def _features_table(self):
        import pandas as pd
        from sqlalchemy import create_engine

        engine = create_engine(self.db_url)
        try:
            return pd.read_sql("SELECT * FROM customer_features ORDER BY customer_id", engine)
        finally:
            engine.dispose()

    def _dag_ns(self, indices=(0, 1)):
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            ns = harness.run_blocks(SLUG, list(indices), stubs=_airflow_stubs(), workdir=self.tmp)
        self.caught = [w for w in caught if issubclass(w.category, FutureWarning)]
        return ns

    def _run_tasks(self, ns, *names, context=None):
        for name in names:
            ns[name](**(context or airflow3_context()))

    def test_dag_wiring_with_airflow3_signature(self):
        ns = self._dag_ns()
        dag = ns["dag"]
        self.assertEqual(dag.kwargs["schedule"], "@daily")
        self.assertFalse(dag.kwargs["catchup"])
        self.assertEqual(set(dag.tasks), {"extract", "transform", "validate", "load"})
        self.assertIn("validate", dag.tasks["load"].upstream)        # the gate blocks the load
        self.assertIn("transform", dag.tasks["validate"].upstream)

    def test_every_task_gets_the_dags_retries_and_failure_callback(self):
        # Step 6's default_args, put where it says (in place of step 1's), must reach every
        # task -- above all the quality gate, the task whose failure most needs a human.
        step6 = harness.python_blocks(SLUG)[4]
        step1_args = 'default_args = {"retries": 3, "retry_delay": timedelta(minutes=5)}\n'
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", FutureWarning)
            ns = harness.run_blocks(SLUG, [0, 1], stubs=_airflow_stubs(), workdir=self.tmp,
                                    replace={0: {step1_args: step6}})
        cb = ns["notify_on_failure"]
        tasks = ns["dag"].tasks
        self.assertEqual(set(tasks), {"extract", "transform", "validate", "load"})
        for task_id, task in tasks.items():
            self.assertEqual(task.retries, 3, task_id)
            self.assertIs(task.on_failure_callback, cb, task_id)

    def test_pipeline_end_to_end_computes_a_real_30_day_window(self):
        ns = self._dag_ns()
        self._run_tasks(ns, "extract", "transform", "validate", "load")
        table = self._features_table()
        # the 30 complete days before the run fires; the trigger day's orders are excluded
        self.assertEqual(table["orders_30d"].sum(), 30)
        self.assertEqual(sorted(table["orders_30d"]), [15, 15])
        self.assertAlmostEqual(table["spend_30d"].sum(), 300.0)
        self.assertEqual(set(table["as_of"]), {AS_OF})

    def test_airflow3_trigger_day_is_not_in_the_window(self):
        # Airflow 3: ds == the day the run fires, whose orders are still arriving. A window
        # ending at ds (inclusive) covers ~29 days and changes if a retry runs later that day.
        import pandas as pd

        ns = self._dag_ns()
        self._run_tasks(ns, "extract")
        orders = pd.read_parquet(self.tmp / f"orders_{AS_OF}.parquet")
        dates = sorted(set(pd.to_datetime(orders["order_date"]).dt.strftime("%Y-%m-%d")))
        self.assertNotIn(AS_OF, dates)
        self.assertEqual((dates[0], dates[-1], len(dates)), ("2026-03-02", "2026-03-31", 30))

    def test_window_is_identical_on_airflow2_and_airflow3_and_any_trigger_time(self):
        import pandas as pd

        outputs = []
        for ctx in (airflow3_context(), airflow2_context(),
                    airflow3_context(RUN_AT + timedelta(hours=14, minutes=37))):  # late manual run
            for stale in self.tmp.glob("*.parquet"):   # each context must produce its own file
                stale.unlink()
            ns = self._dag_ns()
            self._run_tasks(ns, "extract", "transform", context=ctx)
            outputs.append(pd.read_parquet(self.tmp / f"features_{AS_OF}.parquet"))
        for other in outputs[1:]:
            pd.testing.assert_frame_equal(outputs[0], other)

    def test_pendulum_interval_end_is_accepted(self):
        # Airflow passes pendulum DateTimes (task-sdk 1.3.2 requires pendulum>=3.1.0).
        if harness.missing_modules("pendulum"):
            self.skipTest("pendulum not installed")
        import pendulum

        ns = self._dag_ns()
        self._run_tasks(ns, "extract", "transform",
                        context=airflow3_context(pendulum.datetime(2026, 4, 1, tz="UTC")))
        self.assertTrue((self.tmp / f"features_{AS_OF}.parquet").exists())

    def test_run_without_a_data_interval_fails_clearly(self):
        # Airflow 3 runs triggered without a logical date carry neither ds nor a data interval
        # (task-sdk 1.3.2 task_runner.py only sets them when logical_date is not None).
        ns = self._dag_ns()
        with self.assertRaisesRegex(ValueError, "data interval"):
            ns["extract"]()

    def test_load_is_idempotent_on_retry(self):
        ns = self._dag_ns()
        self._run_tasks(ns, "extract", "transform", "load", "load")  # Airflow retry of load
        self.assertEqual(len(self._features_table()), 2)

    def test_failed_insert_rolls_back_the_partition_delete(self):
        import pandas as pd

        ns = self._dag_ns()
        self._run_tasks(ns, "extract", "transform", "load")
        before = self._features_table()
        path = self.tmp / f"features_{AS_OF}.parquet"
        bad = pd.read_parquet(path).assign(unexpected_column=1)    # schema drift -> INSERT fails
        bad.to_parquet(path)
        import pandas.errors
        import sqlalchemy.exc
        # The INSERT of an unknown column fails at the DB layer. pandas 2.2 lets SQLAlchemy's
        # DBAPIError through; pandas 3.1 wraps it in pandas.errors.DatabaseError ("Execution
        # failed on sql ..."). Either way engine.begin() must roll back the DELETE.
        with self.assertRaises((sqlalchemy.exc.DBAPIError, pandas.errors.DatabaseError)) as cm:
            ns["load"](**airflow3_context())
        self.assertIn("unexpected_column", str(cm.exception))
        after = self._features_table()
        self.assertEqual(len(after), len(before), "partition must survive a failed insert")

    def test_quality_gate_rejects_negative_spend(self):
        import pandera.errors

        self._make_warehouse(amounts=[-5.0] * 40)
        ns = self._dag_ns()
        self._run_tasks(ns, "extract", "transform")
        with self.assertRaises(pandera.errors.SchemaErrors):
            ns["validate"](**airflow3_context())

    def test_quality_gate_rejects_empty_feature_file(self):
        import pandas as pd

        ns = self._dag_ns()
        empty = pd.DataFrame({"customer_id": pd.Series(dtype="int64"),
                              "orders_30d": pd.Series(dtype="int64"),
                              "spend_30d": pd.Series(dtype="float64")})
        empty.to_parquet(self.tmp / f"features_{AS_OF}.parquet")
        with self.assertRaises(ValueError):
            ns["validate"](**airflow3_context())

    @staticmethod
    def _pandera_futurewarnings(src):
        # pandera 0.35.0 warns once per PROCESS, on the first top-level DataFrameSchema(...)
        # (pandera/_pandas_deprecated.py). In-process, whichever test built a schema first
        # would hide it, so probe in a fresh interpreter with the opt-out env var removed.
        probe = ("import sys, warnings\n"
                 "with warnings.catch_warnings(record=True) as caught:\n"
                 "    warnings.simplefilter('always')\n"
                 "    exec(compile(sys.stdin.read(), 'block', 'exec'), {})\n"
                 "print(sum(issubclass(w.category, FutureWarning) and 'pandera' in str(w.message)"
                 " for w in caught))\n")
        env = {k: v for k, v in os.environ.items() if k != "DISABLE_PANDERA_IMPORT_WARNING"}
        out = subprocess.run([sys.executable, "-c", probe], input=src, capture_output=True,
                             text=True, timeout=120, env=env)
        if out.returncode != 0:
            raise AssertionError(out.stderr)
        return int(out.stdout.strip().splitlines()[-1])

    def test_pandera_import_is_not_deprecated(self):
        # The block's imports + its schema construction, i.e. the code that triggers the warning
        # (the rest of the block needs the step-1 DAG objects).
        block = harness.python_blocks(SLUG)[1]
        stmts = [ast.get_source_segment(block, n) for n in ast.parse(block).body
                 if isinstance(n, (ast.Import, ast.ImportFrom))
                 or (isinstance(n, ast.Assign) and [getattr(t, "id", "") for t in n.targets] == ["feature_schema"])]
        self.assertEqual(sum(s.startswith("feature_schema") for s in stmts), 1)
        skill_src = "\n".join(stmts)
        # control: the same schema behind the deprecated top-level import does warn
        deprecated_src = "import pandera as pa\nfrom pandera import Column, Check\n" + next(
            s for s in stmts if s.startswith("feature_schema"))
        self.assertGreater(self._pandera_futurewarnings(deprecated_src), 0)
        self.assertEqual(self._pandera_futurewarnings(skill_src), 0)

    def test_feast_definitions_use_real_keyword_names(self):
        calls = []
        stubs = {**_airflow_stubs(), **_feast_stubs(calls)}
        ns = harness.run_blocks(SLUG, [2, 3], stubs=stubs, workdir=self.tmp)
        kinds = [k for k, _ in calls]
        self.assertEqual(kinds.count("Field"), 2)
        for kind in ("Entity", "FileSource", "FeatureView", "get_online_features"):
            self.assertIn(kind, kinds)
        fv = dict(calls)["FeatureView"]
        self.assertEqual({f.name for f in fv["schema"]}, {"orders_30d", "spend_30d"})
        self.assertEqual(dict(calls)["FileSource"]["timestamp_field"], "event_timestamp")
        self.assertEqual(ns["features"]["customer_id"], [42])

    def test_transform_output_has_the_feast_timestamp_column(self):
        import pandas as pd

        ns = self._dag_ns()
        self._run_tasks(ns, "extract", "transform")
        out = pd.read_parquet(self.tmp / f"features_{AS_OF}.parquet")
        self.assertIn("event_timestamp", out.columns)
        self.assertEqual(str(out["event_timestamp"].dt.tz), "UTC")
        # stamped at the window's (exclusive) end == when the run fires: the values are known
        # then, and `feast materialize-incremental <now>` in step 4 already includes the row
        self.assertTrue((out["event_timestamp"] == pd.Timestamp(RUN_AT)).all())

    def test_feast_source_reads_what_load_publishes(self):
        import pyarrow.parquet as pq

        ns = self._dag_ns()
        self._run_tasks(ns, "extract", "transform", "validate", "load", "load")   # with a retry
        calls = []
        harness.run_blocks(SLUG, [2], stubs=_feast_stubs(calls), workdir=self.tmp)
        path = dict(calls)["FileSource"]["path"]
        self.assertEqual(Path(path), self.tmp / "customer_features")
        # Feast's file offline store opens the path with pyarrow ParquetDataset / dask
        # read_parquet (feast 0.67.0 file_source.py, dask.py); both accept a directory.
        table = pq.ParquetDataset(path).read().to_pandas()
        self.assertEqual(len(table), 2, "a retried load must overwrite its file, not add one")
        self.assertTrue({"customer_id", "event_timestamp", "orders_30d", "spend_30d"} <= set(table.columns))

    def test_failure_callback_is_defined_and_logs(self):
        ns = harness.run_blocks(SLUG, [0, 4], stubs=_airflow_stubs(), workdir=self.tmp)
        cb = ns["default_args"]["on_failure_callback"]
        ctx = {"task_instance": SimpleNamespace(task_id="load"), **airflow3_context(),
               "exception": RuntimeError("boom")}
        with self.assertLogs("airflow.task", level=logging.ERROR) as logs:
            cb(ctx)
        self.assertIn("load", logs.output[0])
        self.assertIn(AS_OF, logs.output[0])

    def test_backfill_command_uses_airflow3_cli(self):
        bash = [code for _, code in harness.sl.load_skill(harness.REPO_ROOT / SLUG).code_blocks(lang="bash")]
        commands = [line.split("#")[0].strip() for b in bash for line in b.splitlines()]
        backfill = [c for c in commands if c.startswith("airflow ")]
        self.assertEqual(len(backfill), 1)
        # `airflow backfill create --dag-id/--from-date/--to-date` per apache-airflow-core 3.3.2 cli_config.py
        self.assertTrue(backfill[0].startswith("airflow backfill create "))
        for flag in ("--dag-id", "--from-date", "--to-date"):
            self.assertIn(flag, backfill[0])


if __name__ == "__main__":
    unittest.main()
