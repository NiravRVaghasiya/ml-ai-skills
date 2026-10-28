---
name: data-pipelines
display_name: Data Pipelines
description: >
  Use when the user wants to orchestrate a recurring ML data workflow — DAGs
  that extract/transform data on a schedule, compute and serve features
  consistently, or move data reliably between training and serving. Trigger
  phrases: "build a data pipeline", "orchestrate this ETL with Airflow", "set
  up a feature store", "schedule my feature computation job", "fix train/serve
  skew in my features". NOT for one-off cleaning/encoding of a single training
  dataset (see data-preprocessing) or creating new feature columns (see
  feature-engineering).
type: workflow
domain: mlops
level: intermediate
lifecycle: stable
risk_level: medium
evidence_level: official-documentation
last_verified: 2026-10-27
capabilities:
  - dag-orchestration
  - data-quality-gating
  - feature-store-materialization
  - train-serve-skew-prevention
  - pipeline-backfill-recovery
requires:
conflicts:
related:
  - data-preprocessing
  - feature-engineering
  - ml-monitoring
inputs: A recurring data source (table/feed) and a feature-computation definition that needs to run on a schedule and be served consistently offline and online.
outputs: A scheduled, validated, idempotent pipeline plus an offline/online feature store serving identical values to training and inference.
version_constraints:
  - "apache-airflow>=3.0: `DAG(schedule=...)` (no `schedule_interval`), `airflow.providers.standard.operators.python.PythonOperator`, `context[data_interval_end]` (equal to the fire time under the default CronTriggerTimetable, where ds is the fire date rather than the interval start), and the `airflow backfill create --dag-id --from-date --to-date` CLI (read from the apache-airflow-core 3.3.2 / task-sdk 1.3.2 / providers-standard 1.21.0 wheel sources on 2026-10-27; executed only against a stub DAG/operator and hand-built Airflow 2/3-style contexts)"
  - "pandera>=0.24: `pandera.pandas.DataFrameSchema/Column/Check` and `validate(lazy=True)` (executed against pandera 0.35.0 on 2026-10-27, where the top-level pandas API emits a FutureWarning; 0.24 is the first wheel that ships pandera/pandas.py, checked by inspecting the 0.23.1 and 0.24.0 wheels)"
  - "sqlalchemy>=2.0: `text()` bound parameters, `poolclass=NullPool`, and `engine.begin()` with pandas `read_sql`/`to_sql` (executed against SQLAlchemy 2.1.1 + pandas 2.2.3 on SQLite on 2026-10-27; a raw SQL string passed to `Connection.execute` fails there)"
  - "feast>=0.67: `Entity(join_keys=...)`, `FileSource(path=..., timestamp_field=...)`, `FeatureView(schema=[Field(...)], source=..., ttl=...)`, `get_online_features(features=..., entity_rows=...)` (keyword names read from the feast 0.67.0 wheel source on 2026-10-27; executed only against a stub; older 0.2x+ releases are believed compatible but not verified)"
---

## Overview
Covers the recurring, scheduled infrastructure that keeps ML features fresh and
consistent — not the one-time cleaning of a training set. Builds an orchestrated DAG
(Airflow) with a data-quality gate, then shows how a feature store (Feast) guarantees
the same feature-computation logic is used offline (training) and online (serving),
which is the single most common cause of silent production accuracy loss. The output is
a scheduled, validated, idempotent pipeline — not a notebook that someone re-runs by hand.

## Workflow
1. **Define the DAG: extract → transform → load.** Each task is independently retryable.
   Written for Airflow 3 (`schedule_interval` was removed there; `schedule` works from
   Airflow 2.4) with SQLAlchemy 2 for database access. The window is anchored on
   `data_interval_end` (the time the scheduled run fires), not on `ds`, whose meaning
   changed in Airflow 3 (see Gotchas).
   ```python
   # feature_pipeline_dag.py
   import os
   from datetime import datetime, timedelta

   import pandas as pd
   from airflow import DAG
   from airflow.providers.standard.operators.python import PythonOperator  # Airflow 2: airflow.operators.python
   from sqlalchemy import create_engine, text
   from sqlalchemy.pool import NullPool

   # Intermediate files must be on storage every worker can read (s3://..., a shared volume):
   # with the Celery/Kubernetes executors, extract and transform can run on different hosts.
   DATA_DIR = os.environ.get("FEATURE_PIPELINE_DATA_DIR", "/tmp")
   PUBLISH_DIR = f"{DATA_DIR}/customer_features"   # what the Feast FileSource reads (step 3)

   def get_db_engine():
       # credentials come from the environment / a secrets backend, never from the DAG file;
       # NullPool: a short-lived task should close its connection, not park it in a pool
       return create_engine(os.environ["FEATURES_DB_URL"], poolclass=NullPool)

   def window_end(context) -> pd.Timestamp:
       # Exclusive end of the 30-day window: midnight UTC of the run's data_interval_end, which
       # is the scheduled fire time on Airflow 3 (CronTriggerTimetable) and Airflow 2
       # (CronDataIntervalTimetable) alike. Flooring to midnight keeps a late retry or manual
       # run from pulling in the half-loaded current day. Assumes the DAG timezone is UTC.
       end = context.get("data_interval_end")
       if end is None:   # Airflow 3 runs triggered without a logical date have no data interval
           raise ValueError("run has no data interval; trigger it with a logical date")
       return pd.Timestamp(end).tz_convert("UTC").normalize()

   def extract(**context):
       end = window_end(context)
       as_of = end.strftime("%Y-%m-%d")
       df = pd.read_sql(
           # bound parameters (portable across drivers); half-open [end - 30 days, end) is
           # exactly 30 complete days for a DATE or a TIMESTAMP order_date column
           text("SELECT customer_id, order_id, amount, order_date FROM orders "
                "WHERE order_date >= :start AND order_date < :end"),
           con=get_db_engine(),
           params={"start": (end - pd.Timedelta(days=30)).strftime("%Y-%m-%d"), "end": as_of},
       )
       df.to_parquet(f"{DATA_DIR}/orders_{as_of}.parquet")

   def transform(**context):
       end = window_end(context)
       df = pd.read_parquet(f"{DATA_DIR}/orders_{end:%Y-%m-%d}.parquet")
       features = df.groupby("customer_id").agg(
           orders_30d=("order_id", "count"),
           spend_30d=("amount", "sum"),
       ).reset_index()
       # the feature store keys on it (step 3): stamp the window's end, when the values become
       # known, so point-in-time joins never hand an earlier label orders from inside the window
       features["event_timestamp"] = end
       features.to_parquet(f"{DATA_DIR}/features_{end:%Y-%m-%d}.parquet")

   def load(**context):
       as_of = window_end(context).strftime("%Y-%m-%d")
       features = pd.read_parquet(f"{DATA_DIR}/features_{as_of}.parquet")
       # delete-then-insert by partition in ONE transaction: a retry can't duplicate rows, and a
       # failed insert rolls back the delete instead of leaving the partition empty (see Gotchas).
       # customer_features is created by your schema migrations, not by this task.
       with get_db_engine().begin() as con:
           con.execute(text("DELETE FROM customer_features WHERE as_of = :as_of"), {"as_of": as_of})
           features.assign(as_of=as_of).to_sql(
               "customer_features", con=con, if_exists="append", index=False)
       # publish for the feature store: one file per partition, so a retry overwrites it
       if "://" not in PUBLISH_DIR:   # object stores have no directories to create
           os.makedirs(PUBLISH_DIR, exist_ok=True)
       features.to_parquet(f"{PUBLISH_DIR}/{as_of}.parquet", index=False)

   default_args = {"retries": 3, "retry_delay": timedelta(minutes=5)}

   with DAG(
       dag_id="customer_feature_pipeline",
       schedule="@daily",
       start_date=datetime(2026, 1, 1),
       catchup=False,
       default_args=default_args,
   ) as dag:
       t1 = PythonOperator(task_id="extract", python_callable=extract)
       t2 = PythonOperator(task_id="transform", python_callable=transform)
       t3 = PythonOperator(task_id="load", python_callable=load)
       t1 >> t2 >> t3
   ```
2. **Add a data-quality gate between transform and load.** Fail loudly on a schema
   violation instead of loading garbage.
   ```python
   # pip install pandera
   import pandera.pandas as pa   # pandera >= 0.24; the top-level pandas API now emits a FutureWarning
   from pandera.pandas import Column, Check

   feature_schema = pa.DataFrameSchema({
       "customer_id": Column(int, nullable=False),
       "orders_30d": Column(int, Check.ge(0)),
       "spend_30d": Column(float, Check.ge(0)),
   })

   def validate(**context):
       as_of = window_end(context).strftime("%Y-%m-%d")
       df = pd.read_parquet(f"{DATA_DIR}/features_{as_of}.parquet")
       if df.empty:   # zero rows passes every column check, so test it explicitly
           raise ValueError(f"no feature rows as of {as_of}: upstream extract returned nothing")
       feature_schema.validate(df, lazy=True)   # raises SchemaErrors with a full report

   # same DAG file. Pass dag=dag (or create it inside the `with DAG(...)` block): default_args
   # (retries, the step-6 failure callback) are merged only when an operator is constructed
   # in a DAG; a task that `>>` attaches later joins the DAG with retries=0 and no callback.
   validate_task = PythonOperator(task_id="validate", python_callable=validate, dag=dag)
   t2 >> validate_task >> t3   # load (t3) now waits on the gate
   ```
3. **Define features once, in a feature store, instead of twice.** This is what prevents
   train/serve skew — the same `FeatureView` backs both batch training and online lookup.
   ```python
   # feature_repo/features.py
   import os
   from datetime import timedelta

   from feast import Entity, FeatureView, Field, FileSource
   from feast.types import Int64, Float32

   customer = Entity(name="customer_id", join_keys=["customer_id"])

   customer_source = FileSource(
       # the directory load() publishes to (PUBLISH_DIR in step 1): one parquet file per
       # partition with customer_id, event_timestamp and the feature columns. Set
       # FEATURE_PIPELINE_DATA_DIR to storage the feature server can read too (s3://...).
       path=f"{os.environ['FEATURE_PIPELINE_DATA_DIR']}/customer_features",
       timestamp_field="event_timestamp",
   )

   customer_features = FeatureView(
       name="customer_features",
       entities=[customer],
       ttl=timedelta(days=30),
       schema=[
           Field(name="orders_30d", dtype=Int64),
           Field(name="spend_30d", dtype=Float32),
       ],
       source=customer_source,
   )
   ```
4. **Materialize offline features into the online store.**
   ```bash
   # pip install feast
   feast apply                                                     # register definitions
   feast materialize-incremental "$(date -u +%Y-%m-%dT%H:%M:%S)"    # offline -> online
   ```
5. **Read the same features at inference time.** No re-implementing the aggregation logic
   in the serving path.
   ```python
   from feast import FeatureStore

   store = FeatureStore(repo_path="feature_repo/")
   features = store.get_online_features(
       features=["customer_features:orders_30d", "customer_features:spend_30d"],
       entity_rows=[{"customer_id": 42}],
   ).to_dict()
   ```
6. **Backfill deliberately, and route failures to a human.**
   ```bash
   # Airflow 3 (Airflow 2.x: airflow dags backfill customer_feature_pipeline --start-date ... --end-date ...)
   airflow backfill create --dag-id customer_feature_pipeline --from-date 2026-08-01 --to-date 2026-08-15
   ```
   ```python
   import logging

   def notify_on_failure(context):
       ti = context["task_instance"]
       # forward to Slack/PagerDuty with a provider notifier; keep webhook URLs in an Airflow
       # Connection or secrets backend, not in the DAG file
       logging.getLogger("airflow.task").error(
           "task %s failed for the run ending %s: %r",
           ti.task_id, context.get("data_interval_end"), context.get("exception"))

   # replaces default_args from step 1 — it must be defined *before* the `with DAG(...)` block,
   # and it reaches only tasks built inside that block or with dag=dag (as validate is, step 2)
   default_args = {
       "retries": 3,
       "retry_delay": timedelta(minutes=5),
       "on_failure_callback": notify_on_failure,
   }
   ```

## Gotchas
- **Train/serve skew** — computing a feature one way in the offline batch job and a
  subtly different way in the online request path (different window, different null
  handling) silently degrades the model. A feature store exists specifically to make the
  transformation logic identical in both places; hand-rolled duplicate code paths reliably
  drift apart.
- **Non-idempotent load tasks.** A `load` task that blindly appends on every retry (no
  delete-by-partition or upsert) duplicates rows the moment Airflow retries a failed task
  — and retries are the default behavior, not the exception. Run the delete and the
  insert in one transaction (`engine.begin()`); otherwise a failed insert leaves the
  partition deleted until the retry succeeds.
- **`ds` changed meaning in Airflow 3.** With a cron `schedule` such as `"@daily"`,
  Airflow 3 defaults to `CronTriggerTimetable`: the logical date, and so `ds`, is the
  moment the run fires (`[scheduler] create_cron_data_intervals = False`). On Airflow 2
  (`CronDataIntervalTimetable`) it was the start of the interval that had just ended.
  A window such as `order_date BETWEEN ds-29 AND ds` is correct on Airflow 2. On
  Airflow 3 it takes in the day that is still loading, so a retry later that day gives
  different numbers. Its `ds + 1 day` timestamp also sits a day in the future, after
  `materialize-incremental <now>`. Anchor windows on `data_interval_end` and make the
  end exclusive, as step 1 does.
- **Task-local files don't survive a distributed executor.** Writing intermediates to
  `/tmp` works with the LocalExecutor and breaks with Celery/Kubernetes workers, where
  the next task may run on another host and fail with "file not found". Use object
  storage or a shared volume, keyed by `ds` so retries overwrite rather than append.
- **No data-quality gate between extract and load** means a silent upstream schema
  change (renamed column, sudden null spike, a partner feed dropping a field) flows
  straight into the model instead of failing the pipeline where it's cheap to catch.
- **Tasks created outside the DAG context lose `default_args`.** Airflow merges the
  DAG's `default_args` when an operator is constructed, from `dag=` or the enclosing
  `with DAG(...)`. A task built after the block and attached only by `>>` still joins
  the DAG, but with `retries=0` and no `on_failure_callback`, so it fails without
  retrying and without alerting anyone. Pass `dag=dag` or build it inside the block.
- **`catchup=True` on a DAG with a long `start_date` history** triggers a burst of
  backfill runs the instant it's turned on or unpaused — default to `catchup=False` and
  backfill explicitly and intentionally when you actually need historical runs.
- **Feature TTLs set carelessly** — too long, and inactive entities keep serving stale
  features indefinitely; too short, and legitimately sparse entities get nulls at serving
  time. Tune `ttl` to the feature's real staleness tolerance, not a default.

## References
- [Apache Airflow docs](https://airflow.apache.org/docs/apache-airflow/stable/index.html) — DAGs, scheduling, retries, backfills.
- [Feast docs](https://docs.feast.dev/) — feature store concepts, offline/online stores, materialization.
- [Pandera docs](https://pandera.readthedocs.io/en/stable/) — dataframe schema validation for the quality gate.
