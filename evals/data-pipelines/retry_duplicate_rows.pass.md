No - the retry is not the bug, the load is. `if_exists="append"` makes the task non-idempotent: any second run of the same date adds the rows again. Retries are normal Airflow behaviour (a worker dying is exactly what they are for), so `retries=0` would just turn the next transient failure into a missing day. `SELECT DISTINCT` in the training query hides the duplicates for one consumer while the table stays wrong for everyone else (and silently merges rows that are legitimately identical).

Make the load idempotent: replace the partition, in one transaction.

```python
def load(**context):
    as_of = window_end(context).strftime("%Y-%m-%d")
    features = pd.read_parquet(f"{DATA_DIR}/features_{as_of}.parquet")
    with get_db_engine().begin() as con:            # one transaction: commit or roll back together
        con.execute(text("DELETE FROM customer_features WHERE as_of = :as_of"), {"as_of": as_of})
        features.assign(as_of=as_of).to_sql("customer_features", con=con, if_exists="append", index=False)
```

The delete and the insert must share the transaction: if the insert fails, the delete rolls back instead of leaving the partition empty. (An upsert/MERGE keyed on `(customer_id, as_of)` is the equivalent on warehouses that support it.)

Then repair the damage: clear and re-run the 2026-08-03 load task - with the new code it replaces the duplicated partition. Add a guard so this can't recur silently: a unique constraint on `(customer_id, as_of)`, or a data-quality check that fails when `customer_id` is duplicated within a partition.
