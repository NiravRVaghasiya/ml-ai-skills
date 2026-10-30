"""Executable tests for model-deployment/SKILL.md.

Block 0 (serialize + hash) and block 1 (app.py) run verbatim through
harness.run_blocks. What is stubbed / injected, and why:

* ``clf`` / ``model`` (the skill's documented inputs) are a tiny scikit-learn
  LogisticRegression and a torch Linear, injected via ``namespace=``.
* The FastAPI app is driven with ``fastapi.testclient.TestClient`` — in-process
  ASGI calls, no listening socket — so the lifespan (hash check, token check,
  warm-up) and every route run for real.
* ``MODEL_PATH`` / ``MODEL_SHA256`` / ``MODEL_API_TOKEN`` are set with
  ``mock.patch.dict(os.environ)``; the token is a random test value.

The Dockerfile/bash/yaml blocks cannot run here (no docker / cluster); the yaml
probe snippet is parsed and checked for distinct liveness/readiness paths, the
Dockerfile is checked for a non-root USER, and the step-7 canary manifest is checked
structurally (a second Deployment the Service shares). The two step-7 scripts
(canary-start / canary-promote) do run, under bash with ``kubectl`` replaced by a
shell function that records each call and fails the ones a test chooses, so the
abort paths are exercised: a canary that never gets ready, promotion without a
metric verdict (including both scripts pasted back to back), and a stable rollout
that fails readiness.

Regression tests replay the pre-fix app.py (model-deployment/SKILL.md at commit
8328c60, quoted verbatim in ORIGINAL_APP) to demonstrate each defect: wrong
feature count / empty list -> unhandled 500, NaN accepted, /health reporting
``model_loaded: true`` unconditionally, and no authentication at all. Step 7
described a metric-gated canary but its only command was ``kubectl set image`` on
the live Deployment, a full rolling update gated on readiness alone (observed
2026-10-28); ``test_rollout_step_is_a_real_canary`` fails on that version. The
first canary fix kept start and promote in one unconditional sequence, so a failed
canary or a skipped metric check still promoted; ``TestCanaryScripts`` fails on it.
"""
from __future__ import annotations

import hashlib
import os
import secrets
import shutil
import subprocess
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

from examples import harness

SLUG = "model-deployment"

ORIGINAL_APP = '''
from fastapi import FastAPI
from pydantic import BaseModel
import joblib
import numpy as np

app = FastAPI()
model = joblib.load("model.joblib")

class PredictRequest(BaseModel):
    features: list[float]

@app.get("/health")
def health():
    return {"status": "ok", "model_loaded": model is not None}

@app.post("/predict")
def predict(req: PredictRequest):
    x = np.array(req.features, dtype=float).reshape(1, -1)
    pred = model.predict(x)[0]
    return {"prediction": float(pred)}
'''


def fitted_inputs(string_labels: bool = False):
    import numpy as np
    import torch
    from sklearn.linear_model import LogisticRegression

    gen = np.random.default_rng(0)
    X = gen.normal(size=(80, 4))
    y = (X[:, 0] + X[:, 1] > 0).astype(int)
    if string_labels:
        y = np.where(y == 1, "churn", "stay")
    return {"clf": LogisticRegression().fit(X, y), "model": torch.nn.Linear(4, 2)}


def skill_block(lang: str, contains: str) -> str:
    skill = harness.sl.load_skill(harness.REPO_ROOT / SLUG)
    matches = [code for _, code in skill.code_blocks(lang=lang) if contains in code]
    assert len(matches) == 1, (lang, contains, len(matches))
    return matches[0]


@harness.requires("fastapi", "httpx", "pydantic", "joblib", "numpy", "sklearn", "torch")
class TestModelDeploymentApp(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.workdir = Path(tmp.name)
        self.token = secrets.token_urlsafe(32)

    def build(self, string_labels=False, sha=None, token=None):
        ns = harness.run_blocks(SLUG, [0], namespace=fitted_inputs(string_labels), workdir=self.workdir)
        env = {
            "MODEL_PATH": str(self.workdir / "model.joblib"),
            "MODEL_SHA256": ns["MODEL_SHA256"] if sha is None else sha,
            "MODEL_API_TOKEN": self.token if token is None else token,
        }
        patcher = mock.patch.dict(os.environ, env)
        patcher.start()
        self.addCleanup(patcher.stop)
        ns.update(harness.run_blocks(SLUG, [1], workdir=self.workdir))
        return ns

    def client(self, ns):
        from fastapi.testclient import TestClient

        client = TestClient(ns["app"], raise_server_exceptions=False)
        client.__enter__()  # runs the lifespan (hash check, token check, warm-up)
        self.addCleanup(client.__exit__, None, None, None)
        return client

    def auth(self):
        return {"Authorization": f"Bearer {self.token}"}

    # ---------------------------------------------------------------- happy path
    def test_serialize_block_round_trips_torch_weights_and_hash(self):
        ns = self.build()
        self.assertEqual(ns["MODEL_SHA256"], ns["sha256_of"](str(self.workdir / "model.joblib")))
        self.assertTrue((self.workdir / "model_state.pt").exists())

    def test_ready_and_predict(self):
        ns = self.build()
        c = self.client(ns)
        self.assertEqual(c.get("/livez").status_code, 200)
        r = c.get("/readyz")
        self.assertEqual((r.status_code, r.json()["n_features"]), (200, 4))
        r = c.post("/predict", json={"features": [0.1, 5.2, 3.3, 0.0]}, headers=self.auth())
        self.assertEqual(r.status_code, 200, r.text)
        self.assertIn(r.json()["prediction"], (0, 1))

    def test_string_class_labels_are_returned(self):
        c = self.client(self.build(string_labels=True))
        r = c.post("/predict", json={"features": [1.0, 1.0, 0.0, 0.0]}, headers=self.auth())
        self.assertEqual(r.status_code, 200, r.text)
        self.assertIn(r.json()["prediction"], ("churn", "stay"))

    # ---------------------------------------------------------------- validation
    def test_bad_inputs_are_422_not_500(self):
        c = self.client(self.build())
        bad = [
            {"features": [1.0, 2.0]},                    # wrong count
            {"features": []},                            # empty
            {"features": [1.0] * 5000},                  # over MAX_FEATURES
            {"features": [1.0, 2.0, 3.0, 4.0], "x": 1},  # unknown key
        ]
        for body in bad:
            with self.subTest(body=str(body)[:40]):
                self.assertEqual(c.post("/predict", json=body, headers=self.auth()).status_code, 422)
        # NaN / Infinity: Python's json accepts these literals, so send them raw.
        for literal in ("NaN", "Infinity"):
            with self.subTest(literal=literal):
                r = c.post("/predict", content=f'{{"features": [{literal}, 1, 2, 3]}}',
                           headers={**self.auth(), "Content-Type": "application/json"})
                self.assertEqual(r.status_code, 422)

    def test_auth_required(self):
        c = self.client(self.build())
        body = {"features": [0.1, 5.2, 3.3, 0.0]}
        self.assertEqual(c.post("/predict", json=body).status_code, 401)
        self.assertEqual(c.post("/predict", json=body, headers={"Authorization": "Bearer nope"}).status_code, 401)
        self.assertEqual(c.post("/predict", json=body, headers={"Authorization": self.token}).status_code, 401)
        # Auth schemes are case-insensitive (RFC 9110 sec. 11.1): a lowercase scheme must still work.
        r = c.post("/predict", json=body, headers={"Authorization": f"bearer {self.token}"})
        self.assertEqual(r.status_code, 200, r.text)

    def test_schema_endpoints_are_not_public(self):
        c = self.client(self.build())
        for path in ("/docs", "/redoc", "/openapi.json"):
            with self.subTest(path=path):
                self.assertEqual(c.get(path).status_code, 404)

    # ---------------------------------------------------------------- readiness
    def test_readyz_503_before_startup_and_when_self_check_fails(self):
        from fastapi.testclient import TestClient

        ns = self.build()
        # Without entering the client, the lifespan never ran: nothing loaded -> not ready.
        self.assertEqual(TestClient(ns["app"]).get("/readyz").status_code, 503)
        c = self.client(ns)
        self.assertEqual(c.get("/readyz").status_code, 200)

        class Broken:
            def predict(self, x):
                raise MemoryError("simulated")

        good = ns["state"]["model"]
        ns["state"]["model"] = Broken()
        try:
            r = c.get("/readyz")
            self.assertEqual(r.status_code, 503, r.text)
            self.assertIn("MemoryError", r.json()["detail"])
            self.assertEqual(c.get("/livez").status_code, 200)  # liveness stays up: don't restart
        finally:
            ns["state"]["model"] = good

    # ---------------------------------------------------------------- integrity check
    def test_verified_bytes_are_what_gets_deserialized(self):
        """A file swapped between the hash check and the load must not be unpickled.

        Fails on the pre-review version, which hashed fh.read() and then called
        joblib.load(path), re-opening the (swapped) file."""
        import joblib

        ns = self.build()
        path = self.workdir / "model.joblib"
        real_sha256 = hashlib.sha256

        def swapping_sha256(data=b""):
            h = real_sha256(data)
            joblib.dump({"swapped": True}, path)  # attacker replaces the file after it was read
            return h

        load = ns["load_verified_model"]
        # build() merges namespaces, so patch the function's own globals (block 1's namespace).
        with mock.patch.dict(load.__globals__, {"hashlib": types.SimpleNamespace(sha256=swapping_sha256)}):
            loaded = load(str(path), ns["MODEL_SHA256"])
        self.assertEqual(joblib.load(path), {"swapped": True})  # the swap really happened
        self.assertEqual(type(loaded).__name__, "LogisticRegression")

    # ---------------------------------------------------------------- refuses to start
    def test_refuses_tampered_or_unpinned_artifact(self):
        from fastapi.testclient import TestClient

        for sha in ("0" * 64, ""):
            with self.subTest(sha=sha or "<unset>"):
                ns = self.build(sha=sha)
                with self.assertRaisesRegex(RuntimeError, "refusing to load"):
                    with TestClient(ns["app"]):
                        pass

    def test_refuses_missing_token(self):
        from fastapi.testclient import TestClient

        ns = self.build(token="")
        with self.assertRaisesRegex(RuntimeError, "MODEL_API_TOKEN"):
            with TestClient(ns["app"]):
                pass

    # ---------------------------------------------------------------- original defects
    def original_client(self):
        from fastapi.testclient import TestClient

        harness.run_blocks(SLUG, [0], namespace=fitted_inputs(), workdir=self.workdir)
        ns = {"__name__": "original_model_deployment_app"}
        cwd = os.getcwd()
        os.chdir(self.workdir)
        try:
            exec(compile(ORIGINAL_APP, "original app.py", "exec"), ns)  # noqa: S102 - pre-fix published code
        finally:
            os.chdir(cwd)
        return TestClient(ns["app"], raise_server_exceptions=False)

    def test_original_app_500s_on_bad_counts_and_accepts_nan_without_auth(self):
        c = self.original_client()
        self.assertEqual(c.post("/predict", json={"features": [1.0, 2.0]}).status_code, 500)
        self.assertEqual(c.post("/predict", json={"features": []}).status_code, 500)
        r = c.post("/predict", content='{"features": [NaN, 1, 2, 3]}',
                   headers={"Content-Type": "application/json"})
        self.assertNotEqual(r.status_code, 422)  # NaN reached the estimator instead of being rejected
        self.assertEqual(c.post("/predict", json={"features": [0.1, 5.2, 3.3, 0.0]}).status_code, 200)  # no auth
        self.assertTrue(c.get("/health").json()["model_loaded"])  # can never be false once imported


class _ReducePayload:
    """Pickles to a call of _record_payload: stands in for a malicious model file."""

    def __reduce__(self):
        return (_record_payload, ())


PAYLOAD_RAN: list = []


def _record_payload():
    PAYLOAD_RAN.append(True)
    return "payload"


class _EvilModel:
    """Injected as the skill's ``model``: its state_dict carries a code-executing payload,
    so block 0's own torch.save -> torch.load round trip exercises the skill's load call."""

    def state_dict(self):
        return {"w": _ReducePayload()}

    def load_state_dict(self, sd):
        self.loaded = sd


@harness.requires("torch", "joblib", "sklearn", "numpy")
class TestTorchLoadWeightsOnly(unittest.TestCase):
    def test_skill_load_refuses_code_execution(self):
        import pickle

        # Recent torch defaults to weights_only=True, so the source check guards older torch
        # where omitting the argument would execute the payload.
        self.assertIn('torch.load("model_state.pt", weights_only=True)', harness.python_blocks(SLUG)[0])
        PAYLOAD_RAN.clear()
        ns = {**fitted_inputs(), "model": _EvilModel()}
        with self.assertRaises(pickle.UnpicklingError):
            harness.run_blocks(SLUG, [0], namespace=ns)
        self.assertEqual(PAYLOAD_RAN, [])

    def test_weights_only_false_would_run_the_payload(self):
        # Control: the same block with weights_only=False executes the payload, so the test
        # above is actually sensitive to the skill's argument.
        PAYLOAD_RAN.clear()
        ns = {**fitted_inputs(), "model": _EvilModel()}
        harness.run_blocks(SLUG, [0], namespace=ns,
                           replace={0: {"weights_only=True)": "weights_only=False)"}})
        self.assertEqual(PAYLOAD_RAN, [True])


class TestModelDeploymentConfigBlocks(unittest.TestCase):
    # requires("yaml"), not a skipTest: PyYAML is pinned, so CI (REQUIRE env) must not skip these
    @harness.requires("yaml")
    def test_probes_use_distinct_liveness_and_readiness_paths(self):
        import yaml

        spec = yaml.safe_load(skill_block("yaml", "container spec snippet"))
        self.assertEqual(spec["readinessProbe"]["httpGet"]["path"], "/readyz")
        self.assertEqual(spec["livenessProbe"]["httpGet"]["path"], "/livez")
        self.assertTrue(spec["securityContext"]["runAsNonRoot"])

    @harness.requires("yaml")
    def test_rollout_step_is_a_real_canary(self):
        import yaml

        raw = skill_block("yaml", "kind: Deployment")
        self.assertTrue(harness.is_illustrative(raw))
        canary = yaml.safe_load(raw)
        labels = canary["spec"]["template"]["metadata"]["labels"]
        self.assertEqual(canary["spec"]["selector"]["matchLabels"], labels)
        self.assertEqual(labels, {"app": "churn-model", "track": "canary"})   # shared app label
        self.assertLessEqual(canary["spec"]["replicas"], 1)                    # a small slice
        (container,) = canary["spec"]["template"]["spec"]["containers"]
        self.assertEqual(container["readinessProbe"]["httpGet"]["path"], "/readyz")

        start = skill_block("bash", "churn-model-canary.yaml")
        promote = skill_block("bash", "CANARY_VERDICT")
        self.assertTrue(harness.is_illustrative(start) and harness.is_illustrative(promote))
        self.assertNotIn("set image", start, "starting the canary must not touch stable")
        self.assertLess(promote.index("CANARY_VERDICT"), promote.index("kubectl set image"))

    def test_dockerfile_runs_unprivileged_with_hash_pinned_deps(self):
        dockerfile = skill_block("dockerfile", "FROM python")
        user_lines = [ln for ln in dockerfile.splitlines() if ln.startswith("USER ")]
        self.assertTrue(user_lines and user_lines[-1].split()[1] not in ("root", "0"))
        self.assertIn("--require-hashes", dockerfile)
        self.assertLess(dockerfile.index("COPY app.py"), dockerfile.index("USER "))

    def test_docker_run_binds_loopback_and_torchserve_is_illustrative(self):
        run = skill_block("bash", "docker run")
        self.assertIn("-p 127.0.0.1:8080:8080", run)
        self.assertTrue(harness.is_illustrative(skill_block("bash", "torch-model-archiver")))


BASH = shutil.which("bash")
# Stands in for kubectl: records each call, fails any whose arguments match $KUBECTL_FAIL.
FAKE_KUBECTL = """kubectl() {
  printf '%s\\n' "$*" >> "$KUBECTL_LOG"
  case "$*" in $KUBECTL_FAIL) return 1 ;; esac
}
"""


def _needs_bash(cls):
    """Skip without bash locally; under ML_AI_SKILLS_REQUIRE_EXAMPLE_DEPS=1 (CI) fail instead."""
    if BASH:
        return cls
    if os.environ.get(harness.REQUIRE_ENV) == "1":
        def setUpClass(klass):  # noqa: N802 - unittest hook name
            raise AssertionError(f"{harness.REQUIRE_ENV}=1 but bash not found on PATH")
        cls.setUpClass = classmethod(setUpClass)
        return cls
    return unittest.skip("bash not found on PATH")(cls)


@_needs_bash
class TestCanaryScripts(unittest.TestCase):
    """Runs step 7's two bash blocks with a fake kubectl and checks what reaches stable."""

    def run_scripts(self, *blocks, fail="__never__", verdict=None):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        log = Path(tmp.name) / "kubectl.log"
        log.touch()
        script = Path(tmp.name) / "rollout.sh"
        script.write_bytes((FAKE_KUBECTL + "\n".join(blocks)).encode())
        env = {**os.environ, "KUBECTL_LOG": log.as_posix(), "KUBECTL_FAIL": fail}
        env.pop("CANARY_VERDICT", None)
        if verdict is not None:
            env["CANARY_VERDICT"] = verdict
        proc = subprocess.run([BASH, script.as_posix()], env=env, capture_output=True,
                              text=True, timeout=60)
        return proc.returncode, log.read_text().splitlines()

    @staticmethod
    def touches_stable(calls):
        return any(c.startswith("set image deployment/churn-model ")
                   or c.startswith("rollout status deployment/churn-model ") for c in calls)

    def setUp(self):
        self.start = skill_block("bash", "churn-model-canary.yaml")
        self.promote = skill_block("bash", "CANARY_VERDICT")

    def test_canary_that_never_gets_ready_aborts_before_stable(self):
        rc, calls = self.run_scripts(self.start, self.promote, verdict="pass",
                                     fail="rollout status deployment/churn-model-canary*")
        self.assertNotEqual(rc, 0)
        self.assertFalse(self.touches_stable(calls), calls)
        self.assertEqual(calls[-1], "delete deployment churn-model-canary")

    def test_start_alone_leaves_stable_untouched(self):
        rc, calls = self.run_scripts(self.start)
        self.assertEqual(rc, 0)
        self.assertEqual(calls[0], "apply -f churn-model-canary.yaml")
        self.assertFalse(self.touches_stable(calls), calls)
        self.assertNotIn("delete deployment churn-model-canary", calls)

    def test_no_promotion_without_a_metric_verdict(self):
        # Both blocks pasted back to back, with a healthy canary: still must stop.
        for verdict in (None, "", "fail"):
            with self.subTest(verdict=verdict):
                rc, calls = self.run_scripts(self.start, self.promote, verdict=verdict)
                self.assertNotEqual(rc, 0)
                self.assertFalse(self.touches_stable(calls), calls)

    def test_promotion_that_fails_readiness_rolls_back(self):
        rc, calls = self.run_scripts(self.promote, verdict="pass",
                                     fail="rollout status deployment/churn-model *")
        self.assertNotEqual(rc, 0)
        self.assertEqual(calls[-2:], ["rollout undo deployment/churn-model",
                                      "delete deployment churn-model-canary"])

    def test_passing_verdict_promotes_then_removes_the_canary(self):
        rc, calls = self.run_scripts(self.promote, verdict="pass")
        self.assertEqual(rc, 0)
        self.assertEqual(calls, [
            "set image deployment/churn-model churn-model=registry.example.com/churn-model:1.1",
            "rollout status deployment/churn-model --timeout=300s",
            "delete deployment churn-model-canary",
        ])


if __name__ == "__main__":
    unittest.main()
