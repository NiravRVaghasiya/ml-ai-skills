---
name: model-deployment
display_name: Model Deployment
description: >
  Use when the user wants to package a trained model and serve it behind an
  endpoint — wrapping it in an API, containerizing it, or standing up a
  serving process. Trigger phrases: "deploy this model", "serve my model as
  an API", "containerize this model", "set up a model endpoint", "put this
  model behind FastAPI". NOT for shrinking or quantizing the model itself
  (see model-optimization) or watching it after it's live (see ml-monitoring).
type: workflow
domain: mlops
level: intermediate
lifecycle: stable
risk_level: high
evidence_level: official-documentation
last_verified: 2026-10-28
capabilities:
  - model-serialization
  - rest-api-wrapping
  - container-image-build
  - readiness-liveness-probes
  - high-throughput-model-serving
requires:
conflicts:
related:
  - model-optimization
  - ml-monitoring
  - ci-cd-for-ml
inputs: A trained model artifact (weights or a fitted estimator) and the inference code/input schema needed to run it.
outputs: A containerized, health-checked serving endpoint that returns predictions for real HTTP requests.
version_constraints:
  - "fastapi>=0.100: lifespan= context manager, docs_url/redoc_url/openapi_url=None, Depends, Header, HTTPException, exception_handler(RequestValidationError) (executed via fastapi.testclient against fastapi 0.141.1 on 2026-10-27; 0.100.0 is the first release whose PyPI metadata allows pydantic 2, read on 2026-10-27; 0.100 itself not executed)"
  - "pydantic>=2.0: ConfigDict(allow_inf_nan=False, extra='forbid'), Field(min_length, max_length) on a list (executed against pydantic 2.13.4 on 2026-10-27)"
  - "torch>=1.13: torch.load(weights_only=True) (executed against torch 2.13.0+cpu on 2026-10-27; torch 2.13 refuses a __reduce__ payload with the default and with weights_only=True; the release that changed the default is not verified here)"
---

## Overview
Takes a trained model artifact from a notebook or training job to a running network
endpoint that answers real requests. Covers serialization, wrapping it in a serving API,
containerizing, and the readiness/health-check plumbing an orchestrator needs to run it
safely. The end state is a container that starts, reports when it's actually ready to
serve, and returns predictions for a real HTTP request — not just a model that "works"
in a script.

## Workflow
1. **Serialize the model in a serving-friendly format, and record its hash.** Prefer
   state/weights over a full pickled object when the framework supports it — pickling the
   whole estimator ties the artifact to the exact library version that produced it, and
   loading a pickle/joblib file **executes code from it**, so only load an artifact whose
   bytes you can prove are the ones you trained (the SHA-256 printed here is checked at
   load time in step 2).
   ```python
   import hashlib
   import joblib
   import torch

   joblib.dump(clf, "model.joblib")                     # scikit-learn / generic estimators
   torch.save(model.state_dict(), "model_state.pt")     # PyTorch — weights, not the object

   def sha256_of(path: str) -> str:
       h = hashlib.sha256()
       with open(path, "rb") as fh:
           for chunk in iter(lambda: fh.read(1 << 20), b""):
               h.update(chunk)
       return h.hexdigest()

   MODEL_SHA256 = sha256_of("model.joblib")   # store alongside the artifact in your registry
   print("model.joblib sha256:", MODEL_SHA256)

   # Loading PyTorch weights: weights_only=True refuses arbitrary pickled objects.
   model.load_state_dict(torch.load("model_state.pt", weights_only=True))
   ```
2. **Wrap it in a serving API.** A thin FastAPI app is enough for many low/medium-QPS use
   cases and gives you request validation via Pydantic — but only for the constraints you
   declare: feature count, finite values, and a size cap are checked explicitly below so a
   bad request is a 422, not a 500 or a silent wrong answer. The model is loaded once at
   startup and verified against its hash; if that fails the process exits instead of
   serving. Authenticate callers here or at a gateway in front of it — an unauthenticated
   prediction endpoint invites model extraction and abuse.
   ```python
   # app.py
   import hashlib
   import hmac
   import io
   import os
   from contextlib import asynccontextmanager

   import joblib
   import numpy as np
   from fastapi import Depends, FastAPI, Header, HTTPException, Request
   from fastapi.exceptions import RequestValidationError
   from fastapi.responses import JSONResponse
   from pydantic import BaseModel, ConfigDict, Field

   MODEL_PATH = os.environ.get("MODEL_PATH", "model.joblib")
   MAX_FEATURES = 4096   # hard cap on request size, independent of the model's input width
   state: dict = {}

   def load_verified_model(path: str, expected_sha256: str):
       with open(path, "rb") as fh:
           data = fh.read()
       digest = hashlib.sha256(data).hexdigest()
       if not expected_sha256 or not hmac.compare_digest(digest, expected_sha256.lower()):
           raise RuntimeError(f"refusing to load {path}: sha256 {digest} != MODEL_SHA256")
       # Deserialize the bytes that were hashed, not the path again: a file swapped after the
       # check would otherwise be unpickled unverified. joblib is pickle: verified bytes only.
       return joblib.load(io.BytesIO(data))

   @asynccontextmanager
   async def lifespan(app: FastAPI):
       token = os.environ.get("MODEL_API_TOKEN", "")
       if len(token) < 32:
           raise RuntimeError("MODEL_API_TOKEN must be set (>= 32 chars); refusing to serve unauthenticated")
       model = load_verified_model(MODEL_PATH, os.environ.get("MODEL_SHA256", ""))
       n_features = int(model.n_features_in_)
       probe_row = np.zeros((1, n_features))
       model.predict(probe_row)   # warm-up: fail at startup, not on the first request
       state.update(model=model, n_features=n_features, token=token, probe_row=probe_row)
       yield
       state.clear()

   # No public /docs, /redoc or /openapi.json: they would publish the API schema unauthenticated.
   app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)

   @app.exception_handler(RequestValidationError)
   async def validation_error(request: Request, exc: RequestValidationError):
       # The default 422 body echoes the rejected input: a NaN there is not JSON-serializable
       # (the client gets a 500 instead), and echoing payloads leaks them into client logs.
       errors = [{"loc": list(e["loc"]), "msg": e["msg"], "type": e["type"]} for e in exc.errors()]
       return JSONResponse(status_code=422, content={"detail": errors})

   def require_token(authorization: str = Header(default="")) -> None:
       token = state.get("token")
       scheme, _, credentials = authorization.partition(" ")   # HTTP auth schemes are case-insensitive
       if (not token or scheme.lower() != "bearer"
               or not hmac.compare_digest(credentials.strip().encode(), token.encode())):
           raise HTTPException(status_code=401, detail="missing or invalid bearer token",
                               headers={"WWW-Authenticate": "Bearer"})

   class PredictRequest(BaseModel):
       model_config = ConfigDict(allow_inf_nan=False, extra="forbid")   # reject NaN/inf and unknown keys
       features: list[float] = Field(min_length=1, max_length=MAX_FEATURES)

   @app.get("/livez")
   def livez():
       return {"status": "alive"}   # process is up; says nothing about the model

   @app.get("/readyz")
   def readyz():
       # uvicorn only accepts connections after lifespan startup, so "model loaded" alone is
       # always true here; a cheap self-check prediction makes readiness mean "can serve now".
       # Add checks for anything else /predict depends on (feature store, GPU) the same way.
       if "model" not in state:
           raise HTTPException(status_code=503, detail="model not loaded")
       try:
           state["model"].predict(state["probe_row"])
       except Exception as exc:   # report not-ready instead of a 500 the probe can't interpret
           raise HTTPException(status_code=503, detail=f"self-check failed: {type(exc).__name__}") from None
       return {"status": "ready", "n_features": state["n_features"]}

   @app.post("/predict", dependencies=[Depends(require_token)])
   def predict(req: PredictRequest):
       if len(req.features) != state["n_features"]:
           raise HTTPException(
               status_code=422,
               detail=f"expected {state['n_features']} features, got {len(req.features)}",
           )
       x = np.asarray(req.features, dtype=float).reshape(1, -1)
       pred = state["model"].predict(x)[0]
       return {"prediction": pred.item() if hasattr(pred, "item") else pred}   # int/float/str labels
   ```
3. **Containerize it.** Pin the base image by digest and install hash-pinned dependencies
   so the image behaves the same in beta and prod, and run the process as a non-root user.
   ```dockerfile
   # Resolve the digest once (`docker buildx imagetools inspect python:3.11-slim`) and pin it:
   # FROM python:3.11-slim@sha256:<digest>
   FROM python:3.11-slim
   ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
   WORKDIR /app
   COPY requirements.txt .
   # requirements.txt generated with hashes, e.g. `pip-compile --generate-hashes`
   RUN pip install --no-cache-dir --require-hashes -r requirements.txt
   # Code and model stay root-owned (read-only to the app user); the process runs unprivileged.
   COPY app.py model.joblib ./
   RUN useradd --system --uid 10001 --no-create-home appuser
   USER 10001
   EXPOSE 8080
   # 0.0.0.0 is needed *inside* the container so the published port reaches uvicorn;
   # control who can reach it with the port mapping / network policy, not here.
   CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8080", "--no-server-header", "--timeout-keep-alive", "5"]
   ```
   ```bash
   docker build -t churn-model:1.0 .
   # Bind to loopback for local testing; MODEL_API_TOKEN is passed through from your shell env.
   docker run --rm -p 127.0.0.1:8080:8080 --memory=1g --cpus=1 \
     --read-only --cap-drop=ALL --security-opt no-new-privileges \
     -e MODEL_SHA256="$MODEL_SHA256" -e MODEL_API_TOKEN \
     churn-model:1.0
   ```
4. **Give the orchestrator a real readiness signal.** Distinguish "process is up"
   (`/livez`) from "can serve a prediction right now" (`/readyz`, which runs the step 2
   self-check prediction) so traffic isn't routed to a container that will fail every
   request, and a failed readiness check pulls the pod out of rotation instead of
   restarting it. Keep the readiness check cheap: as a rule of thumb, if one prediction
   takes a meaningful fraction of `timeoutSeconds`, check a cached result instead.
   ```yaml
   # kubernetes container spec snippet
   readinessProbe:
     httpGet:
       path: /readyz
       port: 8080
     initialDelaySeconds: 5
     periodSeconds: 10
     timeoutSeconds: 2
     failureThreshold: 3
   livenessProbe:
     httpGet:
       path: /livez
       port: 8080
     initialDelaySeconds: 15
     periodSeconds: 20
     timeoutSeconds: 2
     failureThreshold: 3
   securityContext:
     runAsNonRoot: true
     readOnlyRootFilesystem: true
     allowPrivilegeEscalation: false
   ```
5. **For higher-throughput deep learning models, consider a dedicated model server**
   instead of hand-rolling batching/threading in FastAPI. Check the server's maintenance
   status first: the TorchServe README carries a "Limited Maintenance" notice (no planned
   bug fixes or security patches), so for new deployments prefer an actively maintained
   server such as NVIDIA Triton Inference Server or KServe. If you do use TorchServe, an
   eager-mode `state_dict` needs the architecture file and a handler matching your input:
   ```bash
   # illustrative: needs a TorchServe install, a model.py defining the network, and a custom handler
   torch-model-archiver --model-name churn-net --version 1.0 \
     --model-file model.py --serialized-file model_state.pt \
     --handler churn_handler.py --export-path model_store

   # Recent TorchServe releases enable token authorization by default; keep it enabled.
   torchserve --start --ncs --model-store model_store --models churn-net=churn-net.mar
   ```
6. **Smoke test the live endpoint** before calling it deployed — readiness first, then one
   authenticated prediction, failing loudly on any non-2xx response or a hang.
   ```bash
   curl -sf --max-time 5 http://127.0.0.1:8080/readyz
   curl -sf --max-time 5 -X POST http://127.0.0.1:8080/predict \
     -H "Content-Type: application/json" \
     -H "Authorization: Bearer $MODEL_API_TOKEN" \
     -d '{"features": [0.1, 5.2, 3.3, 0.0]}'
   ```
7. **Roll out gradually and keep the previous version one command away.** Never overwrite
   the current model or reuse an image tag: deploy the new version alongside the old one,
   send it a small slice of traffic (canary), compare latency, error rate, and prediction
   distribution against the current model (see ml-monitoring), and ramp up only while they
   stay within thresholds you defined before starting. `kubectl set image` on the live
   Deployment is *not* a canary: it replaces every pod as soon as the new ones pass
   `/readyz`, and checks no metric. With plain Kubernetes, run the canary as a second
   Deployment behind the same Service. The stable Deployment's selector must include
   `track: stable` so the two selectors never overlap; a Deployment's selector is
   immutable, so if yours lacks it, recreate that Deployment once before the first canary.
   ```yaml
   # illustrative: churn-model-canary.yaml. Service churn-model selects only app: churn-model,
   # so it balances across stable and canary pods. Use the stable pod spec, with the new image.
   apiVersion: apps/v1
   kind: Deployment
   metadata:
     name: churn-model-canary
   spec:
     replicas: 1    # beside 9 stable replicas: about 1 request in 10 (pod ratio, not exact)
     selector:
       matchLabels: {app: churn-model, track: canary}
     template:
       metadata:
         labels: {app: churn-model, track: canary}
       spec:
         containers:
           - name: churn-model
             image: registry.example.com/churn-model:1.1
             ports: [{containerPort: 8080}]
             readinessProbe: {httpGet: {path: /readyz, port: 8080}, periodSeconds: 10}
             # ...the same env/secret refs, livenessProbe and securityContext as stable
   ```
   Start the canary with one script and promote with another. Run each as a script
   (`bash canary-start.sh`); pasted into an interactive shell, its `exit` closes the shell.
   ```bash
   # illustrative: canary-start.sh, needs the cluster above. Never touches the stable Deployment.
   set -euo pipefail
   kubectl apply -f churn-model-canary.yaml
   if ! kubectl rollout status deployment/churn-model-canary --timeout=180s; then
     kubectl delete deployment churn-model-canary   # canary never became ready: abort
     exit 1
   fi
   ```
   Stop here. Compare canary and stable (split the metrics by the `track` label) over the
   window you agreed on. To abort, run `kubectl delete deployment churn-model-canary`; the
   stable Deployment was never touched. Promote only after every metric stayed within its
   threshold. The promote script refuses to run unless you, or the pipeline's analysis job,
   set `CANARY_VERDICT=pass`:
   ```bash
   # illustrative: canary-promote.sh, run only after the metric comparison passed
   set -euo pipefail
   if [ "${CANARY_VERDICT:-}" != pass ]; then
     echo "refusing to promote: set CANARY_VERDICT=pass after the metric comparison" >&2
     exit 1
   fi
   kubectl set image deployment/churn-model churn-model=registry.example.com/churn-model:1.1
   if ! kubectl rollout status deployment/churn-model --timeout=300s; then
     kubectl rollout undo deployment/churn-model     # readiness failure: back to the old pods
     kubectl delete deployment churn-model-canary    # and take the new version out of traffic
     exit 1
   fi
   kubectl delete deployment churn-model-canary
   ```
   Shadow traffic (a mirrored copy of each request whose response is discarded) needs a
   proxy or service mesh that can mirror requests, such as an Istio `VirtualService`
   `mirror`. Argo Rollouts or Flagger automate the canary ramp and the metric analysis.

## Gotchas
- **Pickling the full estimator/model object** (rather than weights/state) couples the
  artifact to the exact library version and Python version that trained it — a routine
  dependency bump on the serving side can make the artifact unpicklable in prod.
- **`joblib.load` / `pickle.load` / `torch.load(weights_only=False)` run code embedded in
  the file.** Treat a model artifact like an executable: load only artifacts whose hash
  matches the one recorded at training time (step 2 refuses to start otherwise), keep
  `torch.load(..., weights_only=True)` explicit (it is the default in recent PyTorch
  releases, but not in older ones), and for
  scikit-learn consider `skops.io`, which loads only types you explicitly trust.
- **No health check distinct from a liveness check** means an orchestrator can report
  "healthy" for a container whose model failed to load — traffic gets routed and every
  request 500s until someone notices.
- **No input validation on the API surface** (accepting a raw, unchecked list of floats)
  produces failure modes that are not a clean 4xx: a wrong feature *count* or an empty list
  surfaces as an unhandled 500 from the estimator, `NaN`/`Infinity` (which Python's JSON
  parser accepts) flow straight into the model, and a client sending features in the wrong
  *order* (same count) gets a silent wrong prediction. Even with `allow_inf_nan=False`,
  FastAPI's default 422 body echoes the rejected `NaN`, which the JSON encoder refuses, so
  the client sees a 500 — hence step 2's custom validation handler. Step 2 checks count,
  finiteness, and size; a positional list still cannot detect a reordering, so where
  feasible use named fields (one Pydantic field per feature) for feature identity. `max_length` is applied
  only after the whole JSON body is parsed — cap request body size at the proxy/gateway too.
- **Baking environment-specific config or secrets into the image** instead of injecting
  them at runtime breaks "build once, promote everywhere" and often leaks credentials into
  image layers. Env vars are visible to anyone who can `docker inspect` the container or
  read the pod spec; for real secrets (like `MODEL_API_TOKEN`) prefer your orchestrator's
  secret store mounted as a file or injected from a secret reference.
- **Unpinned base images/dependencies** (`FROM python:3.11` instead of a pinned digest,
  or unpinned `requirements.txt`) mean the "same" image can behave differently on rebuild
  weeks later.

## References
- [FastAPI docs](https://fastapi.tiangolo.com/) — request/response models, validation, async endpoints.
- [TorchServe docs](https://pytorch.org/serve/) — PyTorch model server; read its maintenance notice before adopting it.
- [PyTorch `torch.load` docs](https://pytorch.org/docs/stable/generated/torch.load.html) — `weights_only` semantics and the pickle-execution warning.
- [Docker: Building best practices](https://docs.docker.com/build/building/best-practices/) — image layering, pinning, and size guidance.
