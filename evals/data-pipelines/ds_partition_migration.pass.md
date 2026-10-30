No - a plain `RENAME COLUMN` relabels the rows with the wrong date. The old DAG ran on Airflow 2, where `ds` is `data_interval_start`, so a `@daily` row keyed `ds = 2026-03-31` covers the interval that ended on 2026-04-01. In the new contract that window is `as_of = 2026-04-01`: for legacy rows `as_of = ds + 1 day` (on Airflow 3's default timetable the offset would be 0, so treat it as an assumption). The old versions also aggregated only `order_date = ds`, so their "30d" columns hold one-day figures.

Do it as a migration, in this order:

1. List every reader of `customer_features.ds` (training queries, the Feast source, dashboards).
2. Run `migrate_ds_to_as_of(engine)` as a dry run and review the planned DDL and per-partition updates. Check the offset with `legacy_window_matches` on a few partitions: it recomputes the window from `orders` and compares it with the stored rows. If the rows match the one-day window, recompute the history with a backfill instead of training on relabelled values.
3. Pause the DAG. The new DAG's `load()` inserts rows without `ds`, so if the plan prints `BLOCKED`, make `ds` nullable and drop any primary key on it first (e.g. `ALTER TABLE customer_features ALTER COLUMN ds DROP NOT NULL` on Postgres). Then apply with `dry_run=False`: it adds `as_of` and `event_timestamp`, fills them on legacy rows, and deletes nothing.
4. Switch readers to the dual read (`read_customer_features`) so new, migrated and legacy rows are all visible during the transition.
5. Only now deploy the new DAG, unpause and backfill. If your Feast `FileSource` used `timestamp_field="ds"`, change it to `event_timestamp`: on Airflow 2, `ds` is a day before the values were known, so a point-in-time join on it leaks up to a day of later orders.
6. When `migration_report` is clean, add the unique index on `(customer_id, as_of)` and, after the rollback window, stop reading `ds`.
