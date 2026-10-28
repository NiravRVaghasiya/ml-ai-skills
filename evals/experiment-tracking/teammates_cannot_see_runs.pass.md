Nothing is broken - your runs only exist on your machine. With no tracking URI configured, MLflow logs to a local store in the directory you ran the script from: an `./mlruns` folder in older releases, a `sqlite:///mlflow.db` file in recent 3.x releases. Your `mlflow ui` reads that local store; your teammate's reads hers, which is empty. MLflow doesn't raise an error for this, so it just looks like the runs vanished.

To share runs, use a shared tracking server and point everyone at it:

```bash
export MLFLOW_TRACKING_URI=http://mlflow.internal:5000   # your team's tracking server
```

Prefer the environment variable over hard-coding `mlflow.set_tracking_uri(...)` in the script: the `mlflow` CLI only reads `MLFLOW_TRACKING_URI`, and the same script then works locally and in CI. The server's artifact store must be shared too (an object store such as S3 behind the server), otherwise metrics show up but models and plots point at paths on your disk.

Runs already logged locally won't appear on the server on their own - re-run the experiments you care about after switching (or migrate them with an export/import tool).
