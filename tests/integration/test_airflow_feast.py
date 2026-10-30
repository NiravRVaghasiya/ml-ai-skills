"""Opt-in: data-pipelines against real Apache Airflow 3 and a real local Feast repo.

The offline tests (tests/examples/test_data_pipelines.py) replace both with stubs that accept
the real keyword names but do not schedule anything or store features.

``ML_AI_SKILLS_INTEGRATION_AIRFLOW=1`` (needs apache-airflow >= 3 with the standard provider,
pandas, pyarrow, SQLAlchemy, pandera; Airflow does not support native Windows):
  steps 1 + 2 are written verbatim to a DAG file in a temporary ``AIRFLOW_HOME`` with a SQLite
  metadata DB; the DAG must import without errors, and ``airflow dags test`` must run
  extract -> transform -> validate -> load for logical date 2026-04-01 against a SQLite
  warehouse (``FEATURES_DB_URL``). That checks what the stubs cannot: the real task context
  carries the ``data_interval_end`` the window is anchored on (the partition must be
  ``as_of = 2026-04-01`` with exactly 30 complete days, 15 orders per customer), and the
  quality gate runs inside the DAG.

``ML_AI_SKILLS_INTEGRATION_FEAST=1`` (needs feast, pandas, pyarrow, SQLAlchemy):
  step 1's tasks publish one partition (as of today, UTC) to a local directory; step 3's
  definitions are written verbatim to ``feature_repo/features.py`` next to a local-provider
  ``feature_store.yaml`` (SQLite online store, file offline store); step 4's
  ``feast apply`` and ``feast materialize-incremental <now>`` run as subprocesses; step 5 runs
  verbatim and must return customer 42's values computed by the pipeline. That checks that a
  ``FileSource`` pointed at the publish *directory* is readable by the file offline store, and
  that the published schema matches the ``FeatureView``.

Not covered: Celery/Kubernetes executors, retries and the failure callback firing, Feast
remote registries, online stores other than SQLite.
"""
from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

from examples import harness
from examples import test_data_pipelines_object_store as pipeline
from integration import gate

SLUG = "data-pipelines"
TIMEOUT = 900


def run(argv, cwd, env, timeout=TIMEOUT):
    proc = subprocess.run(argv, cwd=cwd, env=env, capture_output=True, text=True, timeout=timeout)
    if proc.returncode != 0:
        raise AssertionError(f"{' '.join(argv)} -> {proc.returncode}\n{proc.stdout[-4000:]}\n{proc.stderr[-4000:]}")
    return proc


def airflow_3() -> str | None:
    from importlib.metadata import PackageNotFoundError, version

    try:
        v = version("apache-airflow")
    except PackageNotFoundError:
        return "apache-airflow is not installed"
    return None if int(v.split(".")[0]) >= 3 else f"apache-airflow {v} is installed; the skill targets Airflow 3"


@gate.opt_in("AIRFLOW", what="data-pipelines DAG under real Airflow 3 (`airflow dags test`)",
             modules=("airflow", "airflow.providers.standard", "pandas", "pyarrow", "sqlalchemy", "pandera"),
             precondition=airflow_3)
class TestDagUnderAirflow(unittest.TestCase):
    def test_dag_imports_and_one_run_writes_the_partition(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            home = Path(tmp)
            (home / "dags").mkdir()
            (home / "data").mkdir()
            blocks = harness.python_blocks(SLUG)
            step1 = pipeline.step1_index()
            gate_block = gate.block_index(SLUG, "feature_schema = pa.DataFrameSchema(")
            (home / "dags" / "feature_pipeline_dag.py").write_text(
                blocks[step1] + "\n\n" + blocks[gate_block], encoding="utf-8")
            db_url = f"sqlite:///{(home / 'warehouse.db').as_posix()}"
            pipeline.make_warehouse(db_url)
            env = {**os.environ, "AIRFLOW_HOME": str(home),
                   "AIRFLOW__CORE__DAGS_FOLDER": str(home / "dags"),
                   "AIRFLOW__CORE__LOAD_EXAMPLES": "False",
                   "AIRFLOW__DATABASE__SQL_ALCHEMY_CONN": f"sqlite:///{(home / 'airflow.db').as_posix()}",
                   "FEATURES_DB_URL": db_url, "FEATURE_PIPELINE_DATA_DIR": (home / "data").as_posix()}
            airflow = [sys.executable, "-m", "airflow"]
            run(airflow + ["db", "migrate"], home, env)
            errors = run(airflow + ["dags", "list-import-errors", "-o", "plain"], home, env)
            self.assertNotIn("feature_pipeline_dag.py", errors.stdout)
            run(airflow + ["dags", "test", "customer_feature_pipeline", "2026-04-01T00:00:00+00:00"], home, env)
            table = pipeline.feature_rows(db_url)
            self.assertEqual(set(table["as_of"]), {pipeline.AS_OF})
            self.assertEqual(sorted(table["orders_30d"]), [15, 15])
            self.assertTrue((home / "data" / "customer_features" / f"{pipeline.AS_OF}.parquet").exists())


FEATURE_STORE_YAML = """project: customer_features_it
registry: data/registry.db
provider: local
online_store:
  type: sqlite
  path: data/online_store.db
offline_store:
  type: file
entity_key_serialization_version: 3
"""


@gate.opt_in("FEAST", what="data-pipelines publish -> feast apply/materialize -> online read",
             modules=("feast", "pandas", "pyarrow", "sqlalchemy"), binaries=("feast",))
class TestFeastRoundTrip(unittest.TestCase):
    def test_published_partition_is_served_online(self):
        import pandas as pd

        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")   # inside the FeatureView's 30-day ttl
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            root = Path(tmp)
            data_dir = (root / "data").as_posix()
            (root / "data").mkdir()
            db_url = f"sqlite:///{(root / 'warehouse.db').as_posix()}"
            pipeline.make_warehouse(db_url, as_of=today, customers=(1, 42))
            env = {**os.environ, "FEATURES_DB_URL": db_url, "FEATURE_PIPELINE_DATA_DIR": data_dir}
            with mock.patch.dict(os.environ, env):
                ns = harness.run_blocks(SLUG, [pipeline.step1_index()], stubs=pipeline.airflow_stubs(), workdir=root)
                fire = pd.Timestamp(today, tz="UTC").to_pydatetime()
                context = {"ds": today, "logical_date": fire, "data_interval_start": fire, "data_interval_end": fire}
                for task in ("extract", "transform", "load"):
                    ns[task](**context)

                repo = root / "feature_repo"
                (repo / "data").mkdir(parents=True)
                (repo / "feature_store.yaml").write_text(FEATURE_STORE_YAML, encoding="utf-8")
                definitions = harness.python_blocks(SLUG)[gate.block_index(SLUG, "customer_source = FileSource(")]
                (repo / "features.py").write_text(definitions, encoding="utf-8")
                run(["feast", "apply"], repo, env)
                run(["feast", "materialize-incremental", datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")],
                    repo, env)
                online = harness.run_blocks(SLUG, [gate.block_index(SLUG, "store.get_online_features(")],
                                            workdir=root)["features"]
        self.assertEqual(online["customer_id"], [42])
        self.assertEqual(online["orders_30d"], [15])
        self.assertAlmostEqual(online["spend_30d"][0], 150.0, places=3)


if __name__ == "__main__":
    unittest.main()
