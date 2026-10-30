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

``TestDsToAsOfMigration`` covers step 7 (KNOWN-ISSUES #25: the partition column
was renamed ``ds`` -> ``as_of`` with no migration). Its legacy table is what the
earlier skill wrote on Airflow 2: one partition per ``ds`` (= data_interval_start)
holding a one-day aggregate of ``order_date = ds`` and no ``as_of`` or
``event_timestamp`` column (reconstructed from ``git show 87d64df:data-pipelines/SKILL.md``).
"""
from __future__ import annotations

import ast
import contextlib
import io
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
# 3 online read, 4 failure callback / default_args, 5 ds -> as_of migration,
# 6 migration report + transition dual read.
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
            if os.environ.get(harness.REQUIRE_ENV) == "1":   # pinned in requirements-examples.txt
                self.fail(f"{harness.REQUIRE_ENV}=1 but pendulum is not installed")
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


LEGACY_DS = [f"2026-03-{d:02d}" for d in range(25, 32)]   # Airflow 2 runs firing 03-26 .. 04-01
CUSTOMERS = (1, 2, 3)


@harness.requires("pandas", "pyarrow", "sqlalchemy", "pandera")
class TestDsToAsOfMigration(unittest.TestCase):
    """Step 7: migrate a table written by the earlier ds-keyed skill to the as_of contract."""

    def setUp(self):
        from sqlalchemy import create_engine
        from sqlalchemy.pool import NullPool

        self._tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)  # sqlite file lock on Windows
        self.tmp = Path(self._tmp.name)
        self.db_url = f"sqlite:///{(self.tmp / 'warehouse.db').as_posix()}"
        env = mock.patch.dict(os.environ, {"FEATURES_DB_URL": self.db_url,
                                           "FEATURE_PIPELINE_DATA_DIR": self.tmp.as_posix()})
        env.start()
        self.addCleanup(env.stop)
        self.addCleanup(self._tmp.cleanup)
        self.engine = create_engine(self.db_url, poolclass=NullPool)
        self.addCleanup(self.engine.dispose)
        self._make_orders()
        self._make_legacy_table()
        self.ns = harness.run_blocks(SLUG, [5, 6], workdir=self.tmp)

    # -- fixtures -------------------------------------------------------------------------------
    def _exec(self, sql, params=None):
        from sqlalchemy import text

        with self.engine.begin() as con:
            con.execute(text(sql), params or {})

    def _make_orders(self, rows=None):
        import pandas as pd

        if rows is None:   # every customer orders once a day for 40 days; amounts differ per day
            days = pd.date_range(end="2026-03-31", periods=40, freq="D").strftime("%Y-%m-%d")
            rows = [{"order_id": i * 10 + c, "customer_id": c, "amount": float(1 + 3 * i + c),
                     "order_date": d} for i, d in enumerate(days) for c in CUSTOMERS]
        with self.engine.begin() as con:
            pd.DataFrame(rows).to_sql("orders", con, index=False, if_exists="replace")

    def _legacy_partition(self, ds, first_day=None):
        """What the earlier skill's extract/transform produced: orders of the single day ds."""
        import pandas as pd

        orders = pd.read_sql("SELECT * FROM orders", self.engine)
        day = orders["order_date"].str[:10]
        window = orders[(day >= (first_day or ds)) & (day <= ds)]
        return window.groupby("customer_id").agg(
            orders_30d=("order_id", "count"), spend_30d=("amount", "sum")).reset_index().assign(ds=ds)

    def _make_legacy_table(self, ds_column="ds TEXT"):
        import pandas as pd

        self._exec("DROP TABLE IF EXISTS customer_features")
        self._exec("CREATE TABLE customer_features (customer_id INTEGER, orders_30d INTEGER, "
                   f"spend_30d REAL, {ds_column})")
        with self.engine.begin() as con:
            pd.concat([self._legacy_partition(ds) for ds in LEGACY_DS]).to_sql(
                "customer_features", con, if_exists="append", index=False)

    def _table(self):
        import pandas as pd

        order = "ds, as_of, customer_id" if "as_of" in self._columns() else "ds, customer_id"
        return pd.read_sql(f"SELECT * FROM customer_features ORDER BY {order}", self.engine)  # noqa: S608

    def _columns(self):
        from sqlalchemy import inspect

        return [c["name"] for c in inspect(self.engine).get_columns("customer_features")]

    def _migrate(self, **kwargs):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            plan = self.ns["migrate_ds_to_as_of"](self.engine, **kwargs)
        return out.getvalue(), plan

    def _run_dag(self, context):
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", FutureWarning)
            dag_ns = harness.run_blocks(SLUG, [0, 1], stubs=_airflow_stubs(), workdir=self.tmp)
        for name in ("extract", "transform", "validate", "load"):
            dag_ns[name](**context)
        return dag_ns

    def _assert_report_clean(self, rows_per_ds=None):
        report = self.ns["migration_report"](self.engine, rows_per_ds)
        self.assertEqual(report, {"unmigrated_ds": [], "wrong_as_of": [], "duplicate_windows": [],
                                  "missing_as_of": [], "row_count_mismatch": {}})

    # -- migration ------------------------------------------------------------------------------
    def test_dry_run_is_the_default_and_changes_nothing(self):
        import pandas as pd

        before, columns = self._table(), self._columns()
        out, plan = self._migrate()
        self.assertIn("dry run: nothing changed", out)
        self.assertEqual(len(plan["ddl"]), 2)
        self.assertIn("PLAN ALTER TABLE customer_features ADD COLUMN as_of VARCHAR(10)", out)
        self.assertIn("PLAN ds=2026-03-25 -> as_of=2026-03-26: 3 rows", out)
        self.assertEqual(plan["updates"], [(ds, self.ns["legacy_as_of"](ds)) for ds in LEGACY_DS])
        self.assertEqual(plan["updates"][-1], ("2026-03-31", "2026-04-01"))
        self.assertEqual(plan["blockers"], [])
        self.assertNotIn("BLOCKED", out)
        self.assertEqual(self._columns(), columns)
        pd.testing.assert_frame_equal(self._table(), before)

    def test_not_null_or_keyed_ds_blocks_the_migration(self):
        import pandas as pd
        import sqlalchemy.exc

        for ds_column, blocker in (("ds TEXT NOT NULL", "ds is NOT NULL"),
                                   ("ds TEXT, PRIMARY KEY (customer_id, ds)", "ds is in the primary key")):
            with self.subTest(ds_column):
                self._make_legacy_table(ds_column)
                before, columns = self._table(), self._columns()
                out, plan = self._migrate()
                self.assertIn(blocker, plan["blockers"])
                self.assertIn(f"BLOCKED {blocker}", out)
                with self.assertRaisesRegex(ValueError, "nothing changed.*nullable"):
                    self._migrate(dry_run=False)
                self.assertEqual(self._columns(), columns)
                pd.testing.assert_frame_equal(self._table(), before)
        # Why it blocks: with ds NOT NULL, the columns added by hand and the new DAG's load fails.
        self._make_legacy_table("ds TEXT NOT NULL")
        _, plan = self._migrate()
        for stmt in plan["ddl"]:
            self._exec(stmt)
        with self.assertRaisesRegex(sqlalchemy.exc.IntegrityError, "NOT NULL"):
            self._run_dag(airflow3_context())
        # The documented remedy (on SQLite, a rebuild without the constraint) unblocks it.
        self._exec("CREATE TABLE relaxed AS SELECT * FROM customer_features")   # copies no constraint
        self._exec("DROP TABLE customer_features")
        self._exec("ALTER TABLE relaxed RENAME TO customer_features")
        out, plan = self._migrate(dry_run=False)
        self.assertEqual(plan["blockers"], [])
        self._run_dag(airflow3_context())
        self.assertEqual(len(self._table().query("as_of == @AS_OF")), 3)
        self._assert_report_clean()

    def test_apply_preserves_rows_and_maps_each_ds_to_its_window_end(self):
        import pandas as pd

        before = self._table()
        _, plan = self._migrate(dry_run=False)
        after = self._table()
        self.assertEqual(set(self._columns()), {"customer_id", "orders_30d", "spend_30d", "ds",
                                                 "as_of", "event_timestamp"})
        pd.testing.assert_frame_equal(after[before.columns], before)       # every row, unchanged
        expected = (pd.to_datetime(after["ds"]) + pd.Timedelta(days=1)).dt.strftime("%Y-%m-%d")
        self.assertEqual(after["as_of"].tolist(), expected.tolist())
        # event_timestamp == as_of 00:00 UTC (SQLite stores the UTC wall time without an offset)
        self.assertTrue((pd.to_datetime(after["event_timestamp"]) == pd.to_datetime(after["as_of"])).all())
        self._assert_report_clean(plan["rows_per_ds"])

    def test_rerun_is_idempotent(self):
        import pandas as pd

        self._migrate(dry_run=False)
        once = self._table()
        out, plan = self._migrate(dry_run=False)
        self.assertEqual((plan["ddl"], plan["updates"], plan["skipped"]), ([], [], []))
        self.assertNotIn("PLAN", out)
        pd.testing.assert_frame_equal(self._table(), once)
        self._assert_report_clean()

    def test_row_count_check_compares_whole_partitions(self):
        self._migrate(dry_run=False)
        # a straggler appended to an already-migrated partition: only it is updated, but the
        # snapshot counts the whole partition, which is what migration_report compares against
        self._exec("INSERT INTO customer_features (customer_id, orders_30d, spend_30d, ds) "
                   "VALUES (4, 1, 9.0, '2026-03-28')")
        out, plan = self._migrate()
        self.assertEqual(plan["updates"], [("2026-03-28", "2026-03-29")])
        self.assertIn("PLAN ds=2026-03-28 -> as_of=2026-03-29: 1 rows", out)
        self.assertEqual(plan["rows_per_ds"], {"2026-03-28": 4})
        self._migrate(dry_run=False)
        self._assert_report_clean(plan["rows_per_ds"])
        # ...and the check is not vacuous: a row lost after the snapshot is reported
        self._exec("DELETE FROM customer_features WHERE customer_id = 4")
        report = self.ns["migration_report"](self.engine, plan["rows_per_ds"])
        self.assertEqual(report["row_count_mismatch"], {"2026-03-28": (4, 3)})

    def test_mapping_check_tells_which_window_the_legacy_rows_hold(self):
        import pandas as pd

        matches = self.ns["legacy_window_matches"]
        for ds in LEGACY_DS:
            self.assertTrue(matches(self.engine, ds, days=1), ds)          # the old one-day window
            self.assertFalse(matches(self.engine, ds, days=30), ds)        # not a 30-day feature
            self.assertFalse(matches(self.engine, ds, offset=pd.Timedelta(0), days=1), ds)
        # A customised legacy pipeline with a real 30-day window (ds-29 .. ds inclusive, correct
        # on Airflow 2) is recognised as one: its rows can be relabelled without recomputing.
        self._exec("DELETE FROM customer_features WHERE ds = :ds", {"ds": "2026-03-31"})
        with self.engine.begin() as con:
            self._legacy_partition("2026-03-31", first_day="2026-03-02").to_sql(
                "customer_features", con, if_exists="append", index=False)
        self.assertTrue(matches(self.engine, "2026-03-31", days=30))
        self.assertFalse(matches(self.engine, "2026-01-01", days=30))      # no stored rows

    # -- the new DAG against the migrated table -------------------------------------------------
    def test_new_dag_before_migration_fails_and_leaves_the_table_alone(self):
        import pandas as pd
        import pandas.errors
        import sqlalchemy.exc

        before = self._table()
        with self.assertRaises((sqlalchemy.exc.DBAPIError, pandas.errors.DatabaseError)) as cm:
            self._run_dag(airflow3_context())       # load's DELETE ... WHERE as_of: no such column
        self.assertIn("as_of", str(cm.exception))
        pd.testing.assert_frame_equal(self._table(), before)

    def test_new_dag_overwrites_the_migrated_partition_of_the_same_window(self):
        # Window alignment: legacy ds=03-31 (Airflow 2 interval [03-31, 04-01)) maps to as_of=04-01,
        # the partition the new DAG writes for data_interval_end=04-01 on either Airflow version.
        _, plan = self._migrate(dry_run=False)
        for context in (airflow2_context(), airflow3_context(), airflow3_context()):   # + a retry
            self._run_dag(context)
            table = self._table()
            part = table[table["as_of"] == AS_OF]
            self.assertEqual(len(part), 3)
            self.assertTrue(part["ds"].isna().all(), "legacy rows of the window must be replaced")
            self.assertEqual(part["orders_30d"].tolist(), [30, 30, 30])
            self.assertEqual(sorted(table["ds"].dropna().unique()), LEGACY_DS[:-1])  # others kept
        self._assert_report_clean()

    def test_window_is_half_open_and_anchored_on_utc_midnight(self):
        self._migrate(dry_run=False)
        rows = [  # (order_date, amount) for customer 1, around both window edges
            ("2026-03-01 23:59:59", 1.0), ("2026-03-02 00:00:00", 2.0),
            ("2026-03-31 23:59:59.999999", 4.0), ("2026-04-01 00:00:00", 8.0)]
        self._make_orders([{"order_id": i, "customer_id": 1, "amount": a, "order_date": d}
                           for i, (d, a) in enumerate(rows)])
        dag_ns = self._run_dag(airflow3_context())
        part = self._table().query("as_of == @AS_OF")
        self.assertEqual((part["orders_30d"].tolist(), part["spend_30d"].tolist()), ([2], [6.0]))
        # data_interval_end in any offset is converted to UTC before flooring to midnight
        window_end = dag_ns["window_end"]
        plus2 = timezone(timedelta(hours=2))
        for end, as_of in ((datetime(2026, 4, 1, 2, 0, tzinfo=plus2), "2026-04-01"),     # 00:00 UTC
                           (datetime(2026, 4, 1, 1, 59, tzinfo=plus2), "2026-03-31"),    # 23:59 UTC
                           (datetime(2026, 3, 29, 1, 0, tzinfo=timezone.utc), "2026-03-29")):
            self.assertEqual(f"{window_end({'data_interval_end': end}):%Y-%m-%d}", as_of, end)

    def test_late_arriving_order_lands_in_its_window_on_rerun(self):
        self._migrate(dry_run=False)
        self._run_dag(airflow3_context())
        first = self._table().query("as_of == @AS_OF").set_index("customer_id")
        self._exec("INSERT INTO orders (order_id, customer_id, amount, order_date) "
                   "VALUES (9999, 1, 500.0, '2026-03-31')")                     # arrived late
        self._run_dag(airflow3_context())                                       # re-run / backfill
        part = self._table().query("as_of == @AS_OF").set_index("customer_id")
        self.assertEqual(len(part), 3)
        self.assertEqual(part.loc[1, "orders_30d"], first.loc[1, "orders_30d"] + 1)
        self.assertAlmostEqual(part.loc[1, "spend_30d"], first.loc[1, "spend_30d"] + 500.0)
        self.assertEqual(part.loc[2, "spend_30d"], first.loc[2, "spend_30d"])
        # ...and not in the previous window, whose exclusive end is 2026-03-31 00:00 UTC
        self._run_dag(airflow3_context(RUN_AT - timedelta(days=1)))
        prev = self._table().query("as_of == '2026-03-31'").set_index("customer_id")
        self.assertEqual(prev.loc[1, "orders_30d"], 30)
        self._assert_report_clean()

    # -- transition -----------------------------------------------------------------------------
    def test_dual_read_dedups_new_migrated_and_legacy_rows(self):
        import pandas as pd

        self._migrate(dry_run=False)
        self._run_dag(airflow3_context())                     # new DAG writes as_of=04-01
        legacy_0401 = self._legacy_partition("2026-04-01")    # empty: no orders on 04-01
        self.assertTrue(legacy_0401.empty)
        # A rolled-back old DAG writes ds=03-30 again (maps to as_of=03-31) and, twice, a ds=04-01
        # row (maps to 04-02); none of these have as_of yet.
        self._exec("DELETE FROM customer_features WHERE ds = '2026-03-30'")
        for _ in range(2):
            self._exec("INSERT INTO customer_features (customer_id, orders_30d, spend_30d, ds) "
                       "VALUES (1, 1, 7.0, '2026-04-01')")
        with self.engine.begin() as con:
            self._legacy_partition("2026-03-30").to_sql("customer_features", con, if_exists="append",
                                                        index=False)
        read = self.ns["read_customer_features"]
        out = read(self.engine, "2026-03-26", "2026-04-03")
        self.assertFalse(out.duplicated(["customer_id", "as_of"]).any())
        self.assertEqual(sorted(out["as_of"].unique()),
                         [f"2026-03-{d}" for d in range(26, 32)] + ["2026-04-01", "2026-04-02"])
        self.assertEqual(out.query("as_of == '2026-04-01'")["orders_30d"].tolist(), [30, 30, 30])  # new wins
        self.assertEqual(out.query("as_of == '2026-03-31'")["orders_30d"].tolist(), [1, 1, 1])     # mapped
        self.assertEqual(out.query("as_of == '2026-04-02'")[["customer_id", "spend_30d"]].values.tolist(),
                         [[1, 7.0]])                                     # exact duplicate dropped
        self.assertEqual(str(out["event_timestamp"].dt.tz), "UTC")
        self.assertTrue((out["event_timestamp"] == pd.to_datetime(out["as_of"]).dt.tz_localize("UTC")).all())
        # half-open range on as_of
        self.assertEqual(sorted(read(self.engine, "2026-03-27", "2026-03-28")["as_of"].unique()), ["2026-03-27"])
        # ...and on legacy rows not migrated yet: ds=03-30 maps to the start (in), ds=04-01 to the end (out)
        edge = read(self.engine, "2026-03-31", "2026-04-02")
        self.assertEqual(sorted(edge["as_of"].unique()), ["2026-03-31", "2026-04-01"])
        self.assertEqual(edge.query("as_of == '2026-03-31'")["orders_30d"].tolist(), [1, 1, 1])
        # the migration picks the stragglers up on its next run, and the read is unchanged
        _, plan = self._migrate(dry_run=False)
        self.assertEqual(plan["updates"], [("2026-03-30", "2026-03-31"), ("2026-04-01", "2026-04-02")])
        self.assertEqual(self.ns["migration_report"](self.engine, plan["rows_per_ds"])["row_count_mismatch"], {})
        pd.testing.assert_frame_equal(read(self.engine, "2026-03-26", "2026-04-03"), out)
        # two different values for one window is a conflict, not something to pick from silently
        self._exec("INSERT INTO customer_features (customer_id, orders_30d, spend_30d, ds) "
                   "VALUES (1, 1, 8.0, '2026-04-01')")
        with self.assertRaisesRegex(ValueError, "conflicting rows"):
            read(self.engine, "2026-04-01", "2026-04-04")

    def test_columns_added_but_not_filled_leave_a_second_copy_until_migrated(self):
        # Gotcha: running the new DAG after only the DDL keeps the legacy copy of its window.
        _, plan = self._migrate()
        for stmt in plan["ddl"]:
            self._exec(stmt)
        self._run_dag(airflow3_context())
        table = self._table()
        self.assertEqual(len(table[table["as_of"] == AS_OF]), 3)
        self.assertEqual(len(table[(table["ds"] == "2026-03-31") & table["as_of"].isna()]), 3)
        out, plan = self._migrate(dry_run=False)
        self.assertEqual(plan["skipped"], [("2026-03-31", AS_OF)])
        self.assertIn("SKIP ds=2026-03-31", out)
        self.assertNotIn("2026-03-31", plan["rows_per_ds"])   # skipped: no rows expected to migrate
        self._assert_report_clean(plan["rows_per_ds"])        # superseded legacy rows are not "unmigrated"
        read = self.ns["read_customer_features"](self.engine, AS_OF, "2026-04-02")
        self.assertEqual(read["orders_30d"].tolist(), [30, 30, 30])


if __name__ == "__main__":
    unittest.main()
