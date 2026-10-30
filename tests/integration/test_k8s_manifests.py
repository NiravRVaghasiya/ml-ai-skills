"""Opt-in: model-deployment's Kubernetes YAML validated by a real API server, without changing it.

Gate: ``ML_AI_SKILLS_INTEGRATION_K8S=1`` plus ``ML_AI_SKILLS_K8S_CONTEXT=<context>``, which must
name the kubeconfig context to use (a disposable cluster such as kind or minikube). Every call
passes ``--context`` explicitly and is ``kubectl apply --dry-run=server --validate=strict``: the
API server runs admission and schema validation and persists nothing. Optional
``ML_AI_SKILLS_K8S_NAMESPACE`` (default ``default``). Needs ``kubectl`` and PyYAML.

What it tests:

* step 7's canary Deployment is accepted as written (selector matches the template labels, the
  probe fields exist in the apps/v1 schema);
* step 4's probe + securityContext snippet, placed into a minimal stable Deployment with the
  ``track: stable`` selector step 7 requires, is accepted with strict field validation, so a
  misspelled field (``readOnlyRootFileSystem``) or a field at the wrong level would fail here.
  Note the snippet's ``securityContext`` is the container-level one.

Not covered: the canary-start/canary-promote scripts against a cluster (they mutate it; the
offline tests run them with a recording fake kubectl), pod scheduling, image pulls, probes
actually passing.
"""
from __future__ import annotations

import json
import os
import subprocess
import unittest

from examples import harness
from integration import gate

SLUG = "model-deployment"
CONTEXT_ENV = "ML_AI_SKILLS_K8S_CONTEXT"


def skill_yaml(contains: str) -> str:
    skill = harness.sl.load_skill(harness.REPO_ROOT / SLUG)
    matches = [code for _, code in skill.code_blocks(lang="yaml") if contains in code]
    if len(matches) != 1:
        raise AssertionError(f"{SLUG}: {len(matches)} yaml blocks contain {contains!r}; expected 1")
    return matches[0]


def context_named() -> str | None:
    ctx = os.environ.get(CONTEXT_ENV, "")
    if not ctx:
        return f"{CONTEXT_ENV} is not set; name the disposable cluster's kubeconfig context explicitly"
    proc = subprocess.run(["kubectl", "--context", ctx, "version", "-o", "json"], capture_output=True,
                          text=True, timeout=60)
    if proc.returncode != 0:
        return f"`kubectl --context {ctx} version` failed: {proc.stderr.strip()}"
    return None


@gate.opt_in("K8S", what="server-side dry-run of model-deployment manifests on a named disposable context",
             modules=("yaml",), binaries=("kubectl",), precondition=context_named)
class TestManifestsAgainstApiServer(unittest.TestCase):
    def dry_run(self, manifest: dict) -> dict:
        ctx = os.environ[CONTEXT_ENV]
        ns = os.environ.get("ML_AI_SKILLS_K8S_NAMESPACE", "default")
        proc = subprocess.run(
            ["kubectl", "--context", ctx, "--namespace", ns, "apply", "--dry-run=server",
             "--validate=strict", "-o", "json", "-f", "-"],
            input=json.dumps(manifest), capture_output=True, text=True, timeout=120)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        return json.loads(proc.stdout)

    def test_canary_deployment_is_accepted(self):
        import yaml

        canary = yaml.safe_load(skill_yaml("kind: Deployment"))
        out = self.dry_run(canary)
        self.assertEqual(out["spec"]["selector"]["matchLabels"], {"app": "churn-model", "track": "canary"})

    def test_probe_and_security_snippet_fits_a_stable_deployment(self):
        import yaml

        snippet = yaml.safe_load(skill_yaml("container spec snippet"))
        container = {"name": "churn-model", "image": "registry.example.com/churn-model:1.0",
                     "ports": [{"containerPort": 8080}], **snippet}
        labels = {"app": "churn-model", "track": "stable"}
        stable = {"apiVersion": "apps/v1", "kind": "Deployment", "metadata": {"name": "churn-model"},
                  "spec": {"replicas": 9, "selector": {"matchLabels": labels},
                           "template": {"metadata": {"labels": labels}, "spec": {"containers": [container]}}}}
        out = self.dry_run(stable)
        (served,) = out["spec"]["template"]["spec"]["containers"]
        self.assertEqual(served["readinessProbe"]["httpGet"]["path"], "/readyz")
        self.assertEqual(served["livenessProbe"]["httpGet"]["path"], "/livez")
        self.assertTrue(served["securityContext"]["readOnlyRootFilesystem"])


if __name__ == "__main__":
    unittest.main()
