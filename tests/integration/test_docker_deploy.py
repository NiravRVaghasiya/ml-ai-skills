"""Opt-in: model-deployment steps 1-3 and 6 as a real container.

Gate: ``ML_AI_SKILLS_INTEGRATION_DOCKER=1``. Needs the ``docker`` CLI talking to a daemon, network
access for ``docker build`` (base image and PyPI), and on the host: torch, scikit-learn, joblib
(step 1 runs here). Builds and removes one image, ``mlai-skills-it-<random>``; nothing is pushed.

What it tests, using the skill's own text:

* step 1 runs verbatim and writes ``model.joblib`` + its SHA-256; step 2's ``app.py`` block and
  step 3's Dockerfile are written out unchanged;
* ``requirements.txt`` is hash-pinned (the Dockerfile installs with ``--require-hashes``): it is
  compiled with ``pip-compile --generate-hashes`` *inside* the Dockerfile's base image, so the
  hashes and markers match that interpreter; scikit-learn and joblib are pinned to the host's
  versions because the pickle must be loaded by the version that wrote it;
* the container is started with step 3's hardening flags (``--read-only --cap-drop=ALL
  --security-opt no-new-privileges``, published on 127.0.0.1 only, on a free port), and
  step 6's smoke test passes: ``/readyz`` answers 200, an authenticated ``/predict`` answers 200,
  an unauthenticated one 401, and the process runs as uid 10001, not root;
* a wrong ``MODEL_SHA256`` makes the container exit instead of serving.

Not covered: digest-pinned base images, registries, image signing, multi-arch builds.
"""
from __future__ import annotations

import json
import secrets
import socket
import subprocess
import tempfile
import time
import unittest
import urllib.error
import urllib.request
import uuid
from pathlib import Path

from examples import harness
from integration import gate

SLUG = "model-deployment"
DOCKER_TIMEOUT = 1800   # first build pulls the base image and compiles requirements


def skill_block(lang: str, contains: str) -> str:
    skill = harness.sl.load_skill(harness.REPO_ROOT / SLUG)
    matches = [code for _, code in skill.code_blocks(lang=lang) if contains in code]
    if len(matches) != 1:
        raise AssertionError(f"{SLUG}: {len(matches)} {lang} blocks contain {contains!r}; expected 1")
    return matches[0]


def docker(*args: str, timeout: int = 120, check: bool = True) -> subprocess.CompletedProcess:
    proc = subprocess.run(["docker", *args], capture_output=True, text=True, timeout=timeout)
    if check and proc.returncode != 0:
        raise AssertionError(f"docker {' '.join(args)} -> {proc.returncode}\n{proc.stdout}\n{proc.stderr}")
    return proc


def daemon_reachable() -> str | None:
    proc = subprocess.run(["docker", "info", "--format", "{{.ServerVersion}}"], capture_output=True,
                          text=True, timeout=60)
    return None if proc.returncode == 0 else f"`docker info` failed: {proc.stderr.strip()}"


def free_loopback_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def http(method: str, url: str, body: dict | None = None, token: str | None = None) -> int:
    if not url.startswith("http://127.0.0.1:"):
        raise ValueError(f"refusing a non-loopback URL: {url}")
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method,  # noqa: S310 - loopback http only (checked above)
                                 headers={"Content-Type": "application/json"})
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:  # noqa: S310 - loopback http only (checked above)
            return resp.status
    except urllib.error.HTTPError as exc:
        return exc.code


@gate.opt_in("DOCKER", what="model-deployment image build, hardened run and smoke test",
             modules=("torch", "sklearn", "joblib", "numpy"), binaries=("docker",), precondition=daemon_reachable)
class TestDockerDeployment(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import joblib
        import sklearn

        from examples.test_model_deployment import fitted_inputs

        # cleanups are registered as each resource is acquired: unittest skips tearDownClass
        # when setUpClass raises (e.g. pip-compile or docker build fails), but runs these
        cls._tmp = tempfile.TemporaryDirectory()
        cls.addClassCleanup(cls._tmp.cleanup)
        cls.ctx = Path(cls._tmp.name)
        cls.ns = harness.run_blocks(SLUG, [0], namespace=fitted_inputs(), workdir=cls.ctx)
        (cls.ctx / "app.py").write_text(harness.python_blocks(SLUG)[1], encoding="utf-8")
        dockerfile = skill_block("dockerfile", "FROM python")
        (cls.ctx / "Dockerfile").write_text(dockerfile, encoding="utf-8")
        base = next(ln.split()[1] for ln in dockerfile.splitlines() if ln.startswith("FROM "))
        (cls.ctx / "requirements.in").write_text(
            "fastapi\nuvicorn\npydantic\nnumpy\n"
            f"scikit-learn=={sklearn.__version__}\njoblib=={joblib.__version__}\n", encoding="utf-8")
        docker("run", "--rm", "-v", f"{cls.ctx.as_posix()}:/w", "-w", "/w", base, "sh", "-c",
               "pip install --quiet pip-tools && pip-compile --quiet --generate-hashes "
               "-o requirements.txt requirements.in", timeout=DOCKER_TIMEOUT)
        cls.image = f"mlai-skills-it-{uuid.uuid4().hex[:12]}"
        cls.addClassCleanup(docker, "rmi", "-f", cls.image, check=False)   # also after a failed build
        docker("build", "-t", cls.image, str(cls.ctx), timeout=DOCKER_TIMEOUT)
        cls.token = secrets.token_urlsafe(32)

    def start(self, sha256: str) -> tuple[str, str]:
        port = free_loopback_port()
        cid = docker("run", "-d", "-p", f"127.0.0.1:{port}:8080", "--memory=1g", "--cpus=1",
                     "--read-only", "--cap-drop=ALL", "--security-opt", "no-new-privileges",
                     "-e", f"MODEL_SHA256={sha256}", "-e", f"MODEL_API_TOKEN={self.token}",
                     self.image).stdout.strip()
        self.addCleanup(docker, "rm", "-f", cid, check=False)
        return cid, f"http://127.0.0.1:{port}"

    def test_hardened_container_serves_and_smoke_test_passes(self):
        cid, base = self.start(self.ns["MODEL_SHA256"])
        deadline, status = time.monotonic() + 90, None
        while time.monotonic() < deadline:
            try:
                status = http("GET", f"{base}/readyz")
            except OSError:
                status = None
            if status == 200:
                break
            time.sleep(1)
        logs = docker("logs", cid, check=False)
        self.assertEqual(status, 200, f"readyz never answered 200\n{logs.stdout}\n{logs.stderr}")
        features = {"features": [0.1, 5.2, 3.3, 0.0]}   # step 6's request
        self.assertEqual(http("POST", f"{base}/predict", features, token=self.token), 200)
        self.assertEqual(http("POST", f"{base}/predict", features), 401)
        self.assertEqual(docker("exec", cid, "id", "-u").stdout.strip(), "10001")

    def test_wrong_model_hash_stops_the_container(self):
        cid, _ = self.start("0" * 64)
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            state = docker("inspect", "-f", "{{.State.Running}} {{.State.ExitCode}}", cid).stdout.split()
            if state[0] == "false":
                break
            time.sleep(1)
        self.assertEqual(state[0], "false", "container kept running with a wrong MODEL_SHA256")
        self.assertNotEqual(state[1], "0")
        self.assertIn("refusing to load", docker("logs", cid, check=False).stderr)


if __name__ == "__main__":
    unittest.main()
