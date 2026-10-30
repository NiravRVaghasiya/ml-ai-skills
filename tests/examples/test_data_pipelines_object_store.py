"""data-pipelines/SKILL.md step 1 with FEATURE_PIPELINE_DATA_DIR set to an object-store URL.

The skill tells users to point ``FEATURE_PIPELINE_DATA_DIR`` at storage every worker can read
("s3://..."), and ``load()`` skips ``os.makedirs`` when the publish path contains ``://``.
``tests/examples/test_data_pipelines.py`` runs the tasks against a local directory only, so the
URL branch had no test. Here the step-1 block runs verbatim with the data dir set to an fsspec
``memory://`` URL, which, like an object store, has no directories to create. What is shared
with ``s3://`` is the skill's own logic (the ``"://"`` guard, the publish layout, overwrite on
retry, the published schema), NOT the I/O client: pandas 2.2 tries
``pyarrow.fs.FileSystem.from_uri`` first, which rejects ``memory://`` (so fsspec's memory
filesystem is used) but accepts ``s3://`` (so the skill's S3 reads/writes go through pyarrow's
native S3 client, with s3fs only as the fallback). What this does NOT show: any S3 client,
credentials/endpoint handling, S3 consistency, or IAM.
``tests/integration/test_s3_publish.py`` (opt-in, ``ML_AI_SKILLS_INTEGRATION_S3=1``) repeats
the same flow against a local moto S3 server, through pyarrow's S3 client (the default route)
and through the s3fs fallback, and asserts which client each skill call used.

Stubbed: ``airflow`` (a minimal ``DAG``/``PythonOperator`` that records ``>>``; this module only
calls the task callables, as the Airflow task runner would). Real: pandas, pyarrow, SQLAlchemy
against a SQLite file standing in for ``FEATURES_DB_URL``, fsspec's memory filesystem.

Regressions this would catch: dropping the ``"://" not in PUBLISH_DIR`` guard (``os.makedirs`` on
a URL creates a local ``memory:``/``s3:`` directory on POSIX, or raises on Windows), a publish
path that appends instead of overwriting on retry, or a published file missing the columns the
step-3 Feast ``FileSource`` reads.
"""
from __future__ import annotations

import os
import tempfile
import unittest
import uuid
import warnings
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

from examples import harness

SLUG = "data-pipelines"
RUN_AT = datetime(2026, 4, 1, tzinfo=timezone.utc)   # when the @daily run fires (Airflow 3 timetable)
AS_OF = "2026-04-01"                                 # partition = window end (exclusive)
FEAST_COLUMNS = {"customer_id", "event_timestamp", "orders_30d", "spend_30d"}   # step 3 FileSource + schema


def airflow3_context(run_after=RUN_AT):
    return {"ds": run_after.strftime("%Y-%m-%d"), "logical_date": run_after,
            "data_interval_start": run_after, "data_interval_end": run_after}


def airflow_stubs():
    """Just enough of airflow for the step-1 module body to execute (tasks are called directly)."""
    class DAG:
        def __init__(self, dag_id, **kwargs):
            self.dag_id, self.kwargs = dag_id, kwargs

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return None

    class PythonOperator:
        def __init__(self, task_id, python_callable, **kwargs):
            self.task_id, self.python_callable, self.downstream = task_id, python_callable, []

        def __rshift__(self, other):
            self.downstream.append(other.task_id)
            return other

    python_mod = harness.make_module("airflow.providers.standard.operators.python", PythonOperator=PythonOperator)
    return {
        "airflow": harness.make_module("airflow", DAG=DAG),
        "airflow.providers": harness.make_module("airflow.providers"),
        "airflow.providers.standard": harness.make_module("airflow.providers.standard"),
        "airflow.providers.standard.operators": harness.make_module("airflow.providers.standard.operators"),
        "airflow.providers.standard.operators.python": python_mod,
    }


def step1_index() -> int:
    hits = [i for i, code in enumerate(harness.python_blocks(SLUG)) if "def load(" in code and "PUBLISH_DIR" in code]
    if len(hits) != 1:
        raise AssertionError(f"{SLUG}: expected one python block defining load() and PUBLISH_DIR, found {hits}")
    return hits[0]


def make_warehouse(db_url: str, as_of: str = AS_OF, customers: tuple[int, int] = (1, 2)) -> None:
    """40 complete days of orders before ``as_of`` (1 per day, alternating between two customers),
    plus orders already in on the trigger day ``as_of`` itself (outside the window)."""
    import pandas as pd
    from sqlalchemy import create_engine, text

    last_day = pd.Timestamp(as_of) - pd.Timedelta(days=1)
    days = pd.date_range(end=last_day, periods=40, freq="D").strftime("%Y-%m-%d")
    rows = [{"order_id": i, "customer_id": customers[i % 2], "amount": 10.0, "order_date": d}
            for i, d in enumerate(days)]
    rows += [{"order_id": 1000 + c, "customer_id": c, "amount": 1000.0, "order_date": as_of} for c in customers]
    engine = create_engine(db_url)
    try:
        with engine.begin() as con:
            pd.DataFrame(rows).to_sql("orders", con, index=False)
            con.execute(text("CREATE TABLE customer_features (customer_id INTEGER, orders_30d INTEGER, "
                             "spend_30d REAL, event_timestamp TIMESTAMP, as_of TEXT)"))
    finally:
        engine.dispose()


def feature_rows(db_url: str):
    import pandas as pd
    from sqlalchemy import create_engine

    engine = create_engine(db_url)
    try:
        return pd.read_sql("SELECT * FROM customer_features ORDER BY customer_id", engine)
    finally:
        engine.dispose()


class ObjectStoreFlowMixin:
    """The checks shared with tests/integration/test_s3_publish.py; subclasses set data_dir/fs."""

    data_dir: str
    workdir: Path
    db_url: str

    def object_names(self, fs, prefix):
        raise NotImplementedError

    def run_pipeline(self):
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", FutureWarning)
            ns = harness.run_blocks(SLUG, [step1_index()], stubs=airflow_stubs(), workdir=self.workdir)
        for task in ("extract", "transform", "load"):
            ns[task](**airflow3_context())
        return ns

    def check_flow(self, fs, read_parquet):
        # the tasks read FEATURES_DB_URL at call time, the data dir at module (DAG file) load
        env = mock.patch.dict(os.environ, {"FEATURES_DB_URL": self.db_url,
                                           "FEATURE_PIPELINE_DATA_DIR": self.data_dir})
        env.start()
        self.addCleanup(env.stop)
        ns = self.run_pipeline()
        self.assertEqual(ns["DATA_DIR"], self.data_dir)
        self.assertEqual(ns["PUBLISH_DIR"], f"{self.data_dir}/customer_features")
        names = self.object_names(fs, self.data_dir)
        self.assertIn(f"orders_{AS_OF}.parquet", names)
        self.assertIn(f"features_{AS_OF}.parquet", names)
        self.assertEqual(self.object_names(fs, ns["PUBLISH_DIR"]), [f"{AS_OF}.parquet"])
        published = read_parquet(f"{ns['PUBLISH_DIR']}/{AS_OF}.parquet")
        self.assertEqual(set(published.columns), FEAST_COLUMNS)
        self.assertEqual(sorted(published["orders_30d"]), [15, 15])   # 30 complete days, trigger day excluded
        self.assertAlmostEqual(float(published["spend_30d"].sum()), 300.0)
        # nothing was created on local disk for the URL (the makedirs guard)
        scheme = self.data_dir.split("://", 1)[0]
        self.assertEqual([p.name for p in self.workdir.iterdir() if p.name.startswith(scheme)], [])
        self.assertFalse(Path(f"{scheme}:").exists())

        # a retry of load() overwrites the published object and the table partition
        ns["load"](**airflow3_context())
        self.assertEqual(self.object_names(fs, ns["PUBLISH_DIR"]), [f"{AS_OF}.parquet"])
        table = feature_rows(self.db_url)
        self.assertEqual(len(table), 2)
        self.assertEqual(set(table["as_of"]), {AS_OF})


@harness.requires("pandas", "pyarrow", "sqlalchemy", "fsspec")
class TestPublishToObjectStoreUrl(ObjectStoreFlowMixin, unittest.TestCase):
    def setUp(self):
        import fsspec

        self._tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)   # sqlite lock on Windows
        self.addCleanup(self._tmp.cleanup)
        self.workdir = Path(self._tmp.name)
        self.db_url = f"sqlite:///{(self.workdir / 'warehouse.db').as_posix()}"
        make_warehouse(self.db_url)
        self.fs = fsspec.filesystem("memory")
        self.root = f"mlai-pipeline-{uuid.uuid4().hex}"   # unique: the memory fs is process-global
        self.data_dir = f"memory://{self.root}"
        self.addCleanup(lambda: self.fs.exists(self.root) and self.fs.rm(self.root, recursive=True))
        old_cwd = os.getcwd()
        os.chdir(self.workdir)                    # a relative makedirs("memory://...") would land here
        self.addCleanup(os.chdir, old_cwd)

    def object_names(self, fs, prefix):
        path = prefix.split("://", 1)[1]
        return sorted(p.rsplit("/", 1)[-1] for p in fs.ls(path, detail=False) if not fs.isdir(p))

    def test_extract_transform_load_publish_through_an_object_store_url(self):
        import pandas as pd

        self.check_flow(self.fs, pd.read_parquet)


if __name__ == "__main__":
    unittest.main()
