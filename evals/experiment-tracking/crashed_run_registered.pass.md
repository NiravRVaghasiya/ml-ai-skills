Two things to fix: the query that picked the run, and the alias that now points at it.

1. **Selection.** `search_runs` ranked every run, including ones that crashed - a run that dies mid-training keeps whatever it logged last, here an f1 from a partial evaluation. Filter on status:

```python
runs = mlflow.search_runs(
    experiment_names=["churn-prediction"],
    filter_string="attributes.status = 'FINISHED'",
    order_by=["metrics.f1_macro DESC"],
    max_results=5,
)
best_run_id = runs.iloc[0]["run_id"]
```

2. **Registry.** Register the correct run (it becomes a new version) and move the alias to it - or point `@staging` back at the previous good version:

```python
client = MlflowClient()
good = mlflow.register_model(f"runs:/{best_run_id}/model", "churn-rf-classifier")
client.set_registered_model_alias("churn-rf-classifier", "staging", good.version)
```

Consumers loading `models:/churn-rf-classifier@staging` pick up the new version on their next load. The alias does not move by itself - deleting the failed run doesn't change what it points to - so do it explicitly, and tag the bad version (e.g. `client.set_model_version_tag(..., "invalid", "crashed run")`) so nobody re-promotes it. Aliases are only metadata: if anything already deployed or cached the bad version, roll that back separately. Don't use `transition_model_version_stage`; it has been deprecated since MLflow 2.9.
