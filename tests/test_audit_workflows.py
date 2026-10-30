"""Tests for scripts/audit_workflows.py - the GitHub Actions workflow audit.

Each bad fixture is the good fixture with exactly one defect, so a test proves
both that the defect is caught and that nothing else in the file is flagged.
"""
from __future__ import annotations

import configparser
import contextlib
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

from testutil import REPO_ROOT
from testutil import sl as tu_sl
from test_eval_router import synthetic_benchmark

sys.path.insert(0, str(REPO_ROOT / "scripts"))
import audit_workflows as aw  # noqa: E402

try:
    import yaml
except ImportError:  # pragma: no cover - exercised in the stdlib-only CI job
    yaml = None

SHA = "11d5960a326750d5838078e36cf38b85af677262"
SHA2 = "a26af69be951a213d495a4c3e4e4022e16d87065"

GOOD = textwrap.dedent(f"""\
    name: ci
    # comment line
    on:
      push:
        branches: [main]
      pull_request:
    permissions:
      contents: read
    concurrency:
      group: ci-${{{{ github.ref }}}}
    env:
      TITLE: ${{{{ github.event.pull_request.title }}}}   # env: is the safe way to pass event data
    jobs:
      test:
        name: "Test: unit"
        runs-on: ubuntu-latest
        timeout-minutes: 10
        steps:
          - uses: actions/checkout@{SHA} # v4.4.0
            with:
              persist-credentials: false
          - uses: actions/setup-python@{SHA2} # v5.6.0
            with:
              python-version: "3.13"
          - name: Run
            env:
              PR_TITLE: ${{{{ github.event.pull_request.title }}}}
              TOKEN: ${{{{ secrets.GITHUB_TOKEN }}}}
            run: |
              echo "title: $PR_TITLE"   # not a comment inside a block scalar: '#'
              python -m unittest
          - run: echo "${{{{ github.event_name }}}} ${{{{ matrix.python }}}}"
          - uses: ./.github/actions/local
      reusable:
        uses: octo/repo/.github/workflows/w.yml@{SHA} # v1.0.0
""")


def audit(text: str) -> list[aw.Finding]:
    return aw.audit_text(text, "wf.yml")


def codes(text: str) -> list[str]:
    return sorted(f.code for f in audit(text))


def mutate(old: str, new: str, text: str = GOOD) -> str:
    assert text.count(old) == 1, (old, text.count(old))
    return text.replace(old, new)


class TestGoodWorkflow(unittest.TestCase):
    def test_good_fixture_is_clean(self):
        self.assertEqual(audit(GOOD), [])

    def test_repo_workflows_are_clean(self):
        files = sorted((REPO_ROOT / ".github" / "workflows").glob("*.y*ml"))
        self.assertTrue(files)
        for f in files:
            with self.subTest(file=f.name):
                self.assertEqual([x for x in aw.audit_file(f)], [])


class TestDefects(unittest.TestCase):
    def assertOnly(self, text: str, code: str, level: str = "ERROR") -> aw.Finding:
        found = audit(text)
        self.assertEqual([f.code for f in found], [code], found)
        self.assertEqual(found[0].level, level)
        return found[0]

    def test_tag_pinned_action(self):
        f = self.assertOnly(mutate(f"actions/setup-python@{SHA2} # v5.6.0", "actions/setup-python@v5"),
                            "unpinned-action")
        self.assertEqual(f.line, GOOD.splitlines().index(f"      - uses: actions/setup-python@{SHA2} # v5.6.0") + 1)

    def test_short_sha_and_branch_refs_are_unpinned(self):
        for ref in (SHA[:12], "main", SHA.upper()):
            with self.subTest(ref=ref):
                self.assertOnly(mutate(f"setup-python@{SHA2}", f"setup-python@{ref}"), "unpinned-action")

    def test_reusable_workflow_must_be_pinned(self):
        self.assertOnly(mutate(f"w.yml@{SHA} # v1.0.0", "w.yml@v1"), "unpinned-action")

    def test_unpinned_docker_action(self):
        self.assertOnly(mutate("uses: ./.github/actions/local", "uses: docker://alpine:3.20"), "unpinned-action")
        digest = "a" * 64
        self.assertEqual(audit(mutate("uses: ./.github/actions/local", f"uses: docker://alpine@sha256:{digest}")), [])

    def test_missing_version_comment_is_a_warning(self):
        self.assertOnly(mutate(f"setup-python@{SHA2} # v5.6.0", f"setup-python@{SHA2}"),
                        "missing-version-comment", "WARN")

    def test_missing_top_level_permissions(self):
        self.assertOnly(mutate("permissions:\n  contents: read\n", ""), "missing-permissions")

    def test_write_all(self):
        self.assertOnly(mutate("permissions:\n  contents: read\n", "permissions: write-all\n"),
                        "excessive-permissions")

    def test_null_or_empty_top_level_permissions_is_missing(self):
        # `permissions:` with no value is YAML null, i.e. the repository default - not "none".
        for replacement in ("permissions:\n", "permissions: ''\n", "permissions: \"\"\n"):
            with self.subTest(replacement=replacement):
                self.assertOnly(mutate("permissions:\n  contents: read\n", replacement), "missing-permissions")
        self.assertEqual(audit(mutate("permissions:\n  contents: read\n", "permissions: {}\n")), [])

    def test_unknown_scope_and_null_job_permissions_are_invalid(self):
        self.assertOnly(mutate("  contents: read\n", "  contentz: read\n"), "invalid-permissions")
        self.assertOnly(mutate("    timeout-minutes: 10\n", "    timeout-minutes: 10\n    permissions:\n"),
                        "invalid-permissions")

    def test_top_level_write_scope(self):
        self.assertOnly(mutate("  contents: read\n", "  contents: write\n"), "excessive-permissions")

    def test_job_level_write_in_pr_workflow(self):
        text = mutate("    timeout-minutes: 10\n",
                      "    timeout-minutes: 10\n    permissions:\n      id-token: write\n")
        self.assertOnly(text, "excessive-permissions")

    def test_job_level_write_allowed_when_prs_cannot_trigger(self):
        text = mutate("  pull_request:\n", "")
        text = mutate("    timeout-minutes: 10\n",
                      "    timeout-minutes: 10\n    permissions:\n      contents: write\n", text)
        self.assertEqual(audit(text), [])

    def test_pull_request_target(self):
        self.assertOnly(mutate("  pull_request:\n", "  pull_request_target:\n"), "pull-request-target")

    def test_event_data_in_run_is_script_injection(self):
        for expr in ("github.event.pull_request.title", "github.head_ref", "github.event.issue.body",
                     "toJSON(github.event)", "github['event']['comment']['body']"):
            with self.subTest(expr=expr):
                text = mutate('echo "title: $PR_TITLE"', f'echo "title: ${{{{ {expr} }}}}"')
                f = self.assertOnly(text, "script-injection")
                self.assertIn("env:", f.message)

    def test_context_names_are_matched_case_insensitively(self):
        # GitHub resolves context/property names case-insensitively, so these are event data too.
        for expr in ("GitHub.Event.issue.title", "GITHUB.EVENT.issue.title", "github.HEAD_REF",
                     "Github['Event']['comment']['body']"):
            with self.subTest(expr=expr):
                self.assertOnly(mutate('echo "title: $PR_TITLE"', f'echo "${{{{ {expr} }}}}"'), "script-injection")

    def test_whole_github_context_is_tainted_but_string_literals_are_not(self):
        for expr in ("toJSON(github)", "github[format('{0}', 'event')]"):
            with self.subTest(expr=expr):
                self.assertOnly(mutate('echo "title: $PR_TITLE"', f'echo "${{{{ {expr} }}}}"'), "script-injection")
        # 'github-actions[bot]' is a string literal, not the github context.
        self.assertEqual(audit(mutate('echo "title: $PR_TITLE"',
                                      "echo \"${{ github.actor == 'github-actions[bot]' }}\"")), [])

    def test_env_indirection_is_script_injection(self):
        # env: is only safe when the shell reads "$VAR"; ${{ env.X }} splices the value into
        # the script text exactly like ${{ github.event.* }} does.
        workflow_level = mutate('echo "title: $PR_TITLE"', 'echo "${{ env.TITLE }}"')
        step_level = mutate('echo "title: $PR_TITLE"', 'echo "${{ env.pr_title }}"')  # case-insensitive
        whole_env = mutate('echo "title: $PR_TITLE"', "echo '${{ toJSON(env) }}'")
        job_level = mutate("    timeout-minutes: 10\n",
                           "    timeout-minutes: 10\n    env:\n      COPY: ${{ env.TITLE }}\n")  # transitive
        job_level = mutate('echo "title: $PR_TITLE"', 'echo "${{ env[\'COPY\'] }}"', job_level)
        for name, text in (("workflow", workflow_level), ("step", step_level), ("toJSON(env)", whole_env),
                           ("job, transitive", job_level)):
            with self.subTest(scope=name):
                f = self.assertOnly(text, "script-injection")
                self.assertIn("carries event data", f.message)

    def test_env_reference_without_event_data_is_fine(self):
        text = mutate("          TOKEN: ${{ secrets.GITHUB_TOKEN }}\n",
                      "          TOKEN: ${{ secrets.GITHUB_TOKEN }}\n          SAFE: constant\n")
        self.assertEqual(audit(mutate('echo "title: $PR_TITLE"', 'echo "${{ env.SAFE }}"', text)), [])

    def test_step_and_job_outputs_in_run_are_a_warning(self):
        for expr in ("steps.meta.outputs.title", "needs.build.outputs.sha", "steps['meta'].outputs['title']"):
            with self.subTest(expr=expr):
                self.assertOnly(mutate('echo "title: $PR_TITLE"', f'echo "${{{{ {expr} }}}}"'),
                                "indirect-expression", "WARN")

    def test_ref_names_in_run_are_a_warning(self):
        # Branch/tag names may contain $ ( ) ` ; and are chosen by whoever pushes the ref.
        for expr in ("github.ref_name", "github.ref", "github.base_ref", "GitHub.Ref_Name", "github['ref_name']"):
            with self.subTest(expr=expr):
                f = self.assertOnly(mutate('echo "title: $PR_TITLE"', f'echo "${{{{ {expr} }}}}"'),
                                    "ref-injection", "WARN")
                self.assertIn("env:", f.message)
        # ...also via an env: entry that copies one (workflow-level here).
        text = mutate("env:\n  TITLE:", "env:\n  BRANCH: ${{ github.ref_name }}\n  TITLE:")
        f = self.assertOnly(mutate('echo "title: $PR_TITLE"', 'echo "${{ env.BRANCH }}"', text), "ref-injection",
                            "WARN")
        self.assertIn("env BRANCH carries a ref name", f.message)
        # github.ref_type is branch|tag, not a name; env: + "$VAR" is the safe pattern.
        self.assertEqual(audit(mutate('echo "title: $PR_TITLE"', 'echo "${{ github.ref_type }}"')), [])
        self.assertEqual(audit(text), [])

    def test_github_script_injection(self):
        text = mutate("      - uses: ./.github/actions/local\n",
                      f"      - uses: actions/github-script@{SHA} # v7.0.1\n"
                      "        with:\n"
                      "          script: |\n"
                      "            console.log(\"${{ github.event.comment.body }}\")\n")
        self.assertOnly(text, "script-injection")

    def test_privileged_triggers_are_a_warning(self):
        for trigger in ("  workflow_run:\n    workflows: [ci]\n    types: [completed]\n", "  issue_comment:\n",
                        "  issues:\n"):
            with self.subTest(trigger=trigger.split(":")[0].strip()):
                self.assertOnly(mutate("  pull_request:\n", trigger), "privileged-trigger", "WARN")

    def test_checkout_of_event_ref_in_privileged_workflow(self):
        checkout = "          persist-credentials: false\n"
        ref = "          persist-credentials: false\n          ref: ${{ github.event.workflow_run.head_sha }}\n"
        text = mutate("  pull_request:\n", "  workflow_run:\n    workflows: [ci]\n")
        self.assertEqual(codes(mutate(checkout, ref, text)), ["privileged-trigger", "untrusted-checkout"])
        # The same ref under plain pull_request is the normal (unprivileged) PR checkout.
        self.assertEqual(audit(mutate(checkout, ref)), [])

    def test_checkout_without_persist_credentials_false(self):
        for replacement in ("", "            persist-credentials: true\n"):
            with self.subTest(replacement=replacement):
                text = mutate("        with:\n          persist-credentials: false\n",
                              ("        with:\n" + replacement) if replacement else "")
                self.assertOnly(text, "checkout-credentials")

    def test_missing_timeout(self):
        self.assertOnly(mutate("    timeout-minutes: 10\n", ""), "missing-timeout")

    def test_secrets_in_pr_workflow(self):
        self.assertOnly(mutate("secrets.GITHUB_TOKEN", "secrets.NPM_TOKEN"), "secrets-in-pr-workflow")
        self.assertOnly(mutate("secrets.GITHUB_TOKEN", "secrets['NPM_TOKEN']"), "secrets-in-pr-workflow")
        self.assertOnly(mutate("secrets.GITHUB_TOKEN", "SECRETS.NPM_TOKEN"), "secrets-in-pr-workflow")
        f = self.assertOnly(mutate("secrets.GITHUB_TOKEN", "toJSON(secrets)"), "secrets-in-pr-workflow")
        self.assertIn("whole secrets context", f.message)
        self.assertEqual(audit(mutate("secrets.GITHUB_TOKEN", "Secrets.github_token")), [])

    def test_top_level_secret_in_pr_workflow(self):
        self.assertOnly(mutate("  TITLE: ${{ github.event.pull_request.title }}", "  TOKEN: ${{ secrets.DEPLOY }}"),
                        "secrets-in-pr-workflow")

    def test_secrets_inherit_in_pr_workflow(self):
        text = mutate("w.yml@" + SHA + " # v1.0.0\n", "w.yml@" + SHA + " # v1.0.0\n    secrets: inherit\n")
        self.assertOnly(text, "secrets-in-pr-workflow")

    def test_secrets_fine_without_pr_trigger(self):
        text = mutate("  pull_request:\n", "")
        self.assertEqual(audit(mutate("secrets.GITHUB_TOKEN", "secrets.NPM_TOKEN", text)), [])

    def test_continue_on_error_on_a_job_or_step(self):
        for old, new in (("    timeout-minutes: 10\n", "    timeout-minutes: 10\n    continue-on-error: true\n"),
                         ("      - name: Run\n", "      - name: Run\n        continue-on-error: true\n"),
                         ("      - name: Run\n", "      - name: Run\n        continue-on-error: ${{ matrix.x }}\n")):
            with self.subTest(new=new.strip().splitlines()[-1]):
                f = self.assertOnly(mutate(old, new), "continue-on-error", "WARN")
                self.assertEqual(GOOD.count("\n", 0, GOOD.index(old) + len(old)) + 1, f.line)
        self.assertEqual(audit(mutate("      - name: Run\n", "      - name: Run\n        continue-on-error: false\n")),
                         [])

    def test_swallowed_failure(self):
        for cmd in ("python -m unittest || true", "python -m unittest || :", "python -m unittest || exit 0",
                    "(make test || true) && echo done", "python -m unittest || echo failed",
                    'python -m unittest || printf "failed\\n"', "python -m unittest || echo failed; exit 0",
                    "python -m unittest || echo failed; exit", "set +e", "set -x +e"):
            with self.subTest(cmd=cmd):
                f = self.assertOnly(mutate("python -m unittest\n", cmd + "\n"), "swallowed-failure", "WARN")
                self.assertEqual(f.line, GOOD.splitlines().index("          python -m unittest") + 1)
        # Recording the code is the fix; text in quotes or comments is not code.
        for cmd in ('python -m unittest || code=$?', "echo '|| true'", 'echo "a || true"', "true  # x || true",
                    "python -m unittest || { echo failed; exit 1; }", 'python -m unittest || echo "::error::x"; exit 1',
                    'python -m unittest || { echo failed; exit "$code"; }', "python -m unittest || echo failed >&2; false",
                    "set +e\n          python -m unittest\n          code=$?",
                    'set +e\n          python -m unittest\n          echo "exit_code=$?" >> "$GITHUB_OUTPUT"'):
            with self.subTest(ok=cmd):
                self.assertEqual(audit(mutate("python -m unittest\n", cmd + "\n")), [])
        # `set +e` with no exit status read afterwards is flagged on the `set` line.
        f = self.assertOnly(mutate("python -m unittest\n", "set +e\n          python -m unittest\n"),
                            "swallowed-failure", "WARN")
        self.assertIn("set +e", f.message)

    def test_pipe_without_pipefail(self):
        piped = mutate("python -m unittest\n", "python -m unittest 2>&1 | tee unit.log\n")
        f = self.assertOnly(piped, "pipe-without-pipefail", "WARN")
        self.assertEqual(f.line, GOOD.splitlines().index("          python -m unittest") + 1)
        # pipefail set later than the pipe does not help.
        self.assertOnly(mutate("python -m unittest 2>&1 | tee unit.log\n",
                               "python -m unittest 2>&1 | tee unit.log\n          set -o pipefail\n", piped),
                        "pipe-without-pipefail", "WARN")

    def test_pipe_with_pipefail_or_without_a_real_pipe_is_fine(self):
        pipe = "python -m unittest 2>&1 | tee unit.log\n"
        cases = {
            "set -euo pipefail": "set -euo pipefail\n          " + pipe,
            "set -o errexit -o pipefail": "set -o errexit -o pipefail\n          " + pipe,
            "or-list": "python -m unittest || exit 1\n",
            "quoted": "grep -E 'a|b' x.log; echo \"x | y\"\n",
            "comment": "true  # a | b\n",
            "heredoc": "python - <<'PY'\n          print(1 | 2)\n          PY\n",
            "case patterns": 'case "$x" in\n            a|b) echo one ;;\n          esac\n',
            "noclobber": "echo x >| out.txt\n",
        }
        for label, new in cases.items():
            with self.subTest(case=label):
                self.assertEqual(audit(mutate("python -m unittest\n", new)), [])
        # `shell: bash` runs with -eo pipefail; pwsh and python shells are not modelled.
        for shell in ("bash", "pwsh", "python", "bash --noprofile --norc -eo pipefail {0}"):
            with self.subTest(shell=shell):
                text = mutate("python -m unittest\n", pipe)
                self.assertEqual(audit(mutate("      - name: Run\n", f"      - name: Run\n        shell: {shell}\n",
                                              text)), [])
        defaults = mutate("    timeout-minutes: 10\n", "    timeout-minutes: 10\n    defaults:\n      run:\n"
                          "        shell: bash\n", mutate("python -m unittest\n", pipe))
        self.assertEqual(audit(defaults), [])
        windows = mutate("    runs-on: ubuntu-latest\n", "    runs-on: windows-latest\n", mutate("python -m unittest\n", pipe))
        self.assertEqual(audit(windows), [])
        # `shell: sh` has no pipefail default: still checked.
        self.assertOnly(mutate("      - name: Run\n", "      - name: Run\n        shell: sh\n",
                               mutate("python -m unittest\n", pipe)), "pipe-without-pipefail", "WARN")


class TestParser(unittest.TestCase):
    def test_structure(self):
        doc = aw.parse_yaml(GOOD)
        steps = doc["jobs"]["test"]["steps"]
        self.assertEqual(doc["jobs"]["test"]["name"], "Test: unit")
        self.assertEqual(doc["on"]["push"]["branches"], ["main"])
        self.assertIsNone(doc["on"]["pull_request"])
        self.assertEqual(steps[0]["with"]["persist-credentials"], "false")
        self.assertTrue(steps[2]["run"].startswith('echo "title: $PR_TITLE"   # not a comment'))
        self.assertTrue(steps[2]["run"].endswith("python -m unittest\n"))
        self.assertEqual(doc.lines["jobs"], GOOD.splitlines().index("jobs:") + 1)

    def test_quoted_scalars_and_flow(self):
        doc = aw.parse_yaml("a: 'it''s # not a comment'\nb: \"x\\ty\"\nc: {d: [1, 'two', {e: f}]}\n")
        self.assertEqual(doc["a"], "it's # not a comment")
        self.assertEqual(doc["b"], "x\ty")
        self.assertEqual(doc["c"], {"d": ["1", "two", {"e": "f"}]})

    def test_sequence_at_parent_indent_and_nested_sequences(self):
        doc = aw.parse_yaml("k:\n- a\n- - b\n  - c\n- d: 1\n  e: 2\n")
        self.assertEqual(doc["k"], ["a", ["b", "c"], {"d": "1", "e": "2"}])

    def test_fails_closed_on_unsupported_yaml(self):
        for text, code in (("a: &x 1\nb: *x\n", "unsupported-yaml"), ("a: 1\n---\nb: 2\n", "unsupported-yaml"),
                           ("a: !!str 1\n", "unsupported-yaml"), ("a: 1\na: 2\n", "parse-error"),
                           ("a:\n\tb: 1\n", "parse-error"), ("a: [1, 2\n", "parse-error"),
                           ("a: 1\n  b: 2\n", "parse-error"), ("a: 'multi\n  line'\n", "unsupported-yaml")):
            with self.subTest(text=text):
                found = audit(text)
                self.assertEqual([f.code for f in found], [code], found)

    def test_uses_count_cross_check_catches_misreads(self):
        # A `uses:` hidden where the structural walk does not look (here: under a non-step key)
        # must not pass silently.
        text = mutate("    timeout-minutes: 10\n", "    timeout-minutes: 10\n    services:\n      uses: x/y@v1\n")
        self.assertIn("parse-error", codes(text))

    @unittest.skipUnless(yaml, "PyYAML not installed (optional cross-check)")
    def test_parity_with_pyyaml_on_repo_workflows(self):
        def norm(x):
            if isinstance(x, dict):
                return {("on" if k is True else str(k)): norm(v) for k, v in x.items()}
            if isinstance(x, list):
                return [norm(v) for v in x]
            return None if x is None else (str(x).lower() if isinstance(x, bool) else str(x))

        files = [*sorted((REPO_ROOT / ".github" / "workflows").glob("*.y*ml")), REPO_ROOT / ".github" / "dependabot.yml"]
        for f in files:
            with self.subTest(file=f.name):
                text = f.read_text(encoding="utf-8")
                self.assertEqual(norm(aw.parse_yaml(text)), norm(yaml.safe_load(text)))
        self.assertEqual(norm(aw.parse_yaml(GOOD)), norm(yaml.safe_load(GOOD)))


class TestCli(unittest.TestCase):
    def run_main(self, *args: str) -> tuple[int, str]:
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
            code = aw.main(list(args))
        return code, out.getvalue()

    def test_exit_codes_and_json(self):
        with tempfile.TemporaryDirectory() as tmp:
            good, warn, bad = Path(tmp, "good.yml"), Path(tmp, "warn.yml"), Path(tmp, "bad.yml")
            good.write_text(GOOD, encoding="utf-8")
            warn.write_text(mutate(f"setup-python@{SHA2} # v5.6.0", f"setup-python@{SHA2}"), encoding="utf-8")
            bad.write_text(mutate("    timeout-minutes: 10\n", ""), encoding="utf-8")
            self.assertEqual(self.run_main(str(good))[0], 0)
            self.assertEqual(self.run_main(str(warn))[0], 0)
            self.assertEqual(self.run_main("--strict", str(warn))[0], 1)
            code, out = self.run_main("--json", str(bad))
            self.assertEqual(code, 1)
            report = json.loads(out)
            self.assertTrue(report["failed"])
            self.assertEqual([f["code"] for f in report["findings"]], ["missing-timeout"])

    def test_default_scans_repo_workflows(self):
        code, out = self.run_main("--strict")
        self.assertEqual(code, 0, out)
        self.assertIn("0 error(s), 0 warning(s)", out)


# --------------------------------------------------------------------------- the repo's own workflow scripts
# Not audit tests: these execute the inline scripts of ci.yml / release.yml locally (no GitHub
# Actions), so the gate logic they implement is tested like any other code.

WORKFLOWS = REPO_ROOT / ".github" / "workflows"


def workflow_step(workflow: str, job: str, name_prefix: str) -> dict:
    doc = aw.parse_yaml((WORKFLOWS / workflow).read_text(encoding="utf-8"))
    steps = [s for s in doc["jobs"][job]["steps"] if str(s.get("name", "")).startswith(name_prefix)]
    assert len(steps) == 1, (workflow, job, name_prefix, len(steps))
    return steps[0]


def _git(root: Path, *args: str) -> None:
    subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@example.invalid", "-c", "commit.gpgsign=false",
                    "-c", "core.autocrlf=false", "-C", str(root), *args], check=True, capture_output=True, timeout=120)


class TestConcurrency(unittest.TestCase):
    def test_push_runs_are_grouped_per_commit(self):
        # release.yml needs a successful push run for the exact tagged commit; a shared per-ref
        # group would let a newer push displace an older commit's pending run.
        doc = aw.parse_yaml((WORKFLOWS / "ci.yml").read_text(encoding="utf-8"))
        group = doc["concurrency"]["group"]
        self.assertIn("github.event_name == 'push' && github.sha || github.ref", group)


NEW_SKILL = textwrap.dedent("""\
    ---
    name: zz-quuxel-frobnication
    display_name: Quuxel Zorble Frobnication
    description: >
      Use when the user wants to frobnicate quuxel zorbles. Trigger phrases:
      "quuxel zorble frobnication", "zorble quuxing", "frobnicate quuxels".
      NOT for anything else (see computer-vision).
    type: reference
    domain: specialized
    level: advanced
    lifecycle: stable
    risk_level: low
    evidence_level: heuristic
    last_verified: 2026-10-01
    capabilities: [quuxel-frobnication, zorble-quuxing]
    requires:
    conflicts:
    related: [computer-vision]
    inputs: Quuxels.
    outputs: Frobnicated zorbles.
    ---

    ## Overview
    Quuxel zorble frobnication: zorble quuxing and quuxel frobnication.

    ## Key Concepts
    - Zorbles.

    ## Gotchas
    - a
    - b
    - c

    ## References
    - [a](https://example.com/a) - reason
    - [b](https://example.com/b) - reason
    """)

ORACLE_ROUTER = textwrap.dedent('''\
    """Test stand-in for scripts/router.py (tests/test_audit_workflows.py TestRoutingGateScript):
    answers every benchmark query with that case's labels, read from this tree's benchmark, so
    the gate's verdicts depend only on what a scenario changes, not on the real router."""
    import json
    from pathlib import Path

    BENCH = Path(__file__).resolve().parents[1] / "evals" / "routing" / "benchmark.json"
    _LABELS = {}


    class Hit:
        def __init__(self, slug):
            self.slug, self.score, self.reasons = slug, 1.0, ["oracle"]


    def route(task_text, top_k=5, min_score=None):
        if not _LABELS:
            for case in json.loads(BENCH.read_text(encoding="utf-8"))["cases"]:
                _LABELS[case["query"]] = case["acceptable"] + case["expected"]
        return "workflow", [Hit(s) for s in _LABELS.get(task_text, [])][:top_k]
    ''')

SABOTAGE = textwrap.dedent("""

    _route_unsabotaged = route


    def route(task_text, top_k=5, min_score=None):  # test sabotage: drop about a third of the queries' hits
        intent, hits = _route_unsabotaged(task_text, top_k=top_k, min_score=min_score)
        return intent, (hits if sum(map(ord, task_text)) % 3 else [])
    """)


def _posix_bash() -> str | None:
    bash = shutil.which("bash")
    if bash is None or "system32" in bash.lower():  # WSL's launcher, not a POSIX bash for this tree
        return None
    return bash


@unittest.skipUnless(shutil.which("git") and _posix_bash(), "git and a POSIX bash are needed")
class TestRoutingGateScript(unittest.TestCase):
    """ci.yml evals-offline 'Routing benchmark (test split) must not regress', run with bash in a
    temp git repo the way the runner does: parent commit = this tree's skills and scripts with a
    synthetic benchmark (tests/test_eval_router.synthetic_benchmark, which meets every coverage
    rule) and an oracle router, HEAD = the change under test, ROUTING_BASE_SHA = the base commit.
    The gate's own logic is unit-tested in tests/test_router_gate.py; these tests pin the wiring
    (the step really runs `eval_router.py --gate` against the base it is given) and the label
    carry-over scenarios end to end, independently of the real benchmark's state."""

    @classmethod
    def setUpClass(cls):
        cls.step = workflow_step("ci.yml", "evals-offline", "Routing benchmark")
        cls._template_dir = tempfile.mkdtemp(prefix="routing-gate-parent-")
        tmp = Path(cls._template_dir)
        cls.parent = tmp / "parent"
        ignore = shutil.ignore_patterns("__pycache__")
        for d in tu_sl.iter_skill_dirs(REPO_ROOT):
            shutil.copytree(d, cls.parent / d.name, ignore=ignore)
        for rel in ("scripts", "schema"):
            shutil.copytree(REPO_ROOT / rel, cls.parent / rel, ignore=ignore)
        (cls.parent / "scripts" / "router.py").write_text(ORACLE_ROUTER, encoding="utf-8", newline="\n")
        routing = cls.parent / "evals" / "routing"
        routing.mkdir(parents=True)
        bench = synthetic_benchmark(tu_sl.load_all_skills(REPO_ROOT))
        (routing / "benchmark.json").write_text(json.dumps(bench, indent=2) + "\n", encoding="utf-8", newline="\n")
        cls.run_eval_router(cls.parent, "--write-baseline", "evals/routing/baseline.json")
        _git(cls.parent, "init", "-q")
        _git(cls.parent, "add", "-A")
        _git(cls.parent, "commit", "-q", "-m", "parent")
        cls.parent_sha = cls.rev(cls.parent, "HEAD")
        # `python` in the step is the interpreter running these tests.
        shim = tmp / "bin"
        shim.mkdir()
        (shim / "python").write_bytes(f'#!/usr/bin/env bash\nexec "{Path(sys.executable).as_posix()}" "$@"\n'.encode())
        (shim / "python").chmod(0o755)
        cls.path = str(shim) + os.pathsep + os.environ.get("PATH", "")
        (tmp / "step.sh").write_text(str(cls.step["run"]), encoding="utf-8", newline="\n")

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls._template_dir, ignore_errors=True)

    @staticmethod
    def run_eval_router(root: Path, *args: str) -> None:
        r = subprocess.run([sys.executable, "scripts/eval_router.py", *args], cwd=root, capture_output=True,
                           text=True, encoding="utf-8", timeout=600)
        if r.returncode != 0:
            raise AssertionError(f"eval_router.py {' '.join(args)} exited {r.returncode}:\n{r.stdout}{r.stderr}")

    @staticmethod
    def rev(repo: Path, ref: str) -> str:
        return subprocess.run(["git", "-C", str(repo), "rev-parse", ref], capture_output=True, text=True,
                              check=True, timeout=60).stdout.strip()

    def setUp(self):
        self._tmp = tempfile.mkdtemp(prefix="routing-gate-")
        self.root = Path(self._tmp) / "repo"
        _git(Path(self._tmp), "clone", "-q", "--no-hardlinks", str(self.parent), str(self.root))

    def tearDown(self):
        shutil.rmtree(self._tmp, ignore_errors=True)

    @property
    def bench_path(self) -> Path:
        return self.root / "evals" / "routing" / "benchmark.json"

    def read_bench(self) -> dict:
        return json.loads(self.bench_path.read_text(encoding="utf-8"))

    def write_bench(self, bench: dict) -> None:
        self.bench_path.write_text(json.dumps(bench, indent=2) + "\n", encoding="utf-8", newline="\n")

    def add_skill_with_cases(self) -> None:
        (self.root / "zz-quuxel-frobnication").mkdir()
        (self.root / "zz-quuxel-frobnication" / "SKILL.md").write_text(NEW_SKILL, encoding="utf-8", newline="\n")
        bench = self.read_bench()
        for i, split in enumerate(("dev", "test"), 1):
            bench["cases"].append({"id": f"zz-00{i}", "query": f"Frobnicate these quuxel zorbles, batch {i}.",
                                   "category": "typical", "split": split, "family": f"zz-00{i}",
                                   "expected": ["zz-quuxel-frobnication"], "acceptable": [], "forbidden": [],
                                   "notes": "", "labellers": ["fixture"], "adjudication": "single-labeller"})
        self.write_bench(bench)

    def sabotage_router(self) -> None:
        with open(self.root / "scripts" / "router.py", "a", encoding="utf-8", newline="\n") as fh:
            fh.write(SABOTAGE)

    def rebaseline(self) -> None:
        self.run_eval_router(self.root, "--write-baseline", "evals/routing/baseline.json")

    def gate(self, base: str | None = None) -> tuple[int, str, str]:
        """Commit the scenario, then run the step with ROUTING_BASE_SHA = `base` (default: the
        parent commit, what the base step resolves for a single-commit push or PR)."""
        _git(self.root, "add", "-A")
        _git(self.root, "commit", "-q", "--allow-empty", "-m", "change")
        summary = Path(self._tmp) / "summary.md"
        summary.unlink(missing_ok=True)
        env = {k: v for k, v in os.environ.items() if k != "ROUTING_BASE_SHA"}
        env.update(PATH=self.path, RUNNER_TEMP=self._tmp, GITHUB_STEP_SUMMARY=str(summary), PYTHONUTF8="1",
                   ROUTING_BASE_SHA=self.parent_sha if base is None else base)
        r = subprocess.run([_posix_bash(), "--noprofile", "--norc", "-e", str(Path(self._template_dir) / "step.sh")],
                           cwd=self.root, env=env, capture_output=True, text=True, encoding="utf-8", timeout=600)
        return r.returncode, summary.read_text(encoding="utf-8") if summary.exists() else "", r.stdout + r.stderr

    def test_step_runs_the_gate_script_with_the_resolved_base(self):
        run = str(self.step["run"])
        self.assertNotIn("<<", run)  # one gate implementation: no inline copy of it
        self.assertRegex(run, r'python scripts/eval_router\.py --gate --base-sha "\$ROUTING_BASE_SHA"')
        # resolve_base uses an already-resolved base as is only when no --event-name is given
        self.assertNotIn("--event-name", run)
        self.assertIn("steps.base.outputs.sha", str(self.step["env"]["ROUTING_BASE_SHA"]))
        upload = workflow_step("ci.yml", "evals-offline", "Upload reports")
        self.assertEqual(str(upload.get("if")).strip(), "always()")
        for report in ("router-report.json", "router-report-parent.json"):
            self.assertIn(report, str(upload["with"]["path"]))

    def test_unchanged_tree_passes_all_three_checks(self):
        code, summary, log = self.gate()
        self.assertEqual(code, 0, log)
        self.assertIn("- integrity: pass", summary)
        self.assertIn("this commit's router vs this commit's baseline: pass", summary)
        self.assertRegex(summary, r"vs parent \w+'s benchmark \+ baseline: pass")
        self.assertTrue((self.root / "router-report.json").is_file())
        self.assertTrue((self.root / "router-report-parent.json").is_file())

    def test_regression_in_an_earlier_commit_of_a_push_is_caught_against_the_base(self):
        # A push of two commits: the first regresses the router and lowers baseline.json, the
        # second is unrelated. HEAD^1 is the first commit, whose lowered baseline the router
        # meets; the base commit (the push's `before`) still has the real bar.
        self.sabotage_router()
        self.rebaseline()
        _git(self.root, "add", "-A")
        _git(self.root, "commit", "-q", "-m", "regress and lower the baseline")
        lowered = self.rev(self.root, "HEAD")
        code, summary, log = self.gate(base=lowered)  # what a HEAD^1-only comparison would see
        self.assertEqual(code, 0, log)
        code, summary, log = self.gate()
        self.assertEqual(code, 1, log)
        self.assertIn(f"parent {self.parent_sha[:12]}", log)
        self.assertRegex(log, r"::error::routing regression \[parent \w+\]: recall = [0-9.]+ < required minimum")

    def test_empty_base_fails(self):
        code, summary, log = self.gate(base="")
        self.assertEqual(code, 1, log)
        self.assertIn("::error::ROUTING_BASE_SHA is empty", log)

    def test_base_missing_from_the_clone_fails(self):
        # Not "no baseline at the base" (a ::warning, for the change that introduces baseline.json):
        # a named base that does not resolve means the comparison cannot run at all.
        code, summary, log = self.gate(base="1" * 40)
        self.assertEqual(code, 1, log)
        self.assertIn("is not in this clone", log)

    def test_new_skill_does_not_stop_the_parent_check(self):
        # The parent benchmark has no case for the new slug; the per-skill coverage rule is for
        # this commit's benchmark only, so the no-regression check still runs (it once exited 2
        # and was skipped with a ::warning).
        self.add_skill_with_cases()
        self.rebaseline()
        code, summary, log = self.gate()
        self.assertNotIn("NOT RUN", summary)
        self.assertNotIn("does not validate", log)
        self.assertRegex(summary, r"this commit's router vs parent \w+'s benchmark \+ baseline: pass")
        self.assertEqual(code, 0, log)

    def test_new_skill_cannot_carry_a_lowered_baseline(self):
        # The audit scenario: add a skill, regress the router, re-baseline (lowering baseline.json)
        # in the same change. Checks 1 and 2 pass; check 3 must fail.
        self.add_skill_with_cases()
        self.sabotage_router()
        self.rebaseline()
        code, summary, log = self.gate()
        self.assertEqual(code, 1, log)
        self.assertIn("this commit's router vs this commit's baseline: pass", summary)
        self.assertRegex(log, r"::error::routing regression \[parent \w+\]: recall = [0-9.]+ < required minimum")
        self.assertNotIn("NOT RUN", summary)

    def test_new_prerequisite_is_relabelled_not_skipped(self):
        # A new `requires:` makes the parent's cases fail the prerequisite-label rule; the gate
        # marks the prerequisite acceptable on those cases instead of skipping the check.
        md = self.root / "data-preprocessing" / "SKILL.md"
        text = md.read_text(encoding="utf-8")
        self.assertEqual(text.count("\nrequires:\n"), 1)
        md.write_text(text.replace("\nrequires:\n", "\nrequires: [python-for-ml]\n"), encoding="utf-8", newline="\n")
        bench = self.read_bench()
        for case in bench["cases"]:
            labelled = case["expected"] + case["acceptable"]
            if "data-preprocessing" in labelled and "python-for-ml" not in labelled:
                case["forbidden"] = [s for s in case["forbidden"] if s != "python-for-ml"]
                case["acceptable"].append("python-for-ml")
        self.write_bench(bench)
        self.rebaseline()
        code, summary, log = self.gate()
        self.assertNotIn("NOT RUN", summary)
        self.assertNotIn("does not validate", log)
        self.assertIn("relabelled for new prerequisites", summary)
        self.assertEqual(code, 0, log)  # the oracle also selects the prerequisite this tree labels

    @staticmethod
    def without_skill(bench: dict, gone: str) -> dict:
        """The benchmark an author would commit after deleting `gone`."""
        cases = [c for c in bench["cases"] if gone not in c["expected"]
                 and not (c["category"] == "conflicting" and c["forbidden"] == [gone])]
        return {**bench, "cases": [{**c, "acceptable": [s for s in c["acceptable"] if s != gone],
                                    "forbidden": [s for s in c["forbidden"] if s != gone]} for c in cases]}

    def removable_skill(self, bench: dict) -> str:
        """A skill whose removal leaves a benchmark that still meets every coverage rule."""
        import eval_router as er  # noqa: PLC0415 - scripts/ is on sys.path
        skills = tu_sl.load_all_skills(self.root)
        requires = er.requires_map(skills)
        for gone in sorted(s.slug for s in skills):
            rest = {k: v for k, v in requires.items() if k != gone}
            if any(gone in v for v in rest.values()):
                continue
            valid = {s.slug for s in skills} - {gone}
            candidate = self.without_skill(bench, gone)
            if not (er.validate_benchmark(candidate, valid, rest) or er.coverage_errors(candidate, valid)
                    or er.split_integrity_errors(candidate)):
                return gone
        self.fail("no skill can be removed from the synthetic benchmark without breaking its coverage rules")

    def test_parent_cases_for_a_removed_skill_are_dropped_with_a_warning(self):
        bench = self.read_bench()
        gone = self.removable_skill(bench)
        self.write_bench(self.without_skill(bench, gone))
        shutil.rmtree(self.root / gone)
        self.rebaseline()
        code, summary, log = self.gate()
        self.assertNotIn("NOT RUN", summary)
        self.assertIn("expect a skill this commit no longer has", log)
        self.assertIn("dropped for removed skills", summary)
        self.assertEqual(code, 0, log)


FAKE_GH = """#!/usr/bin/env bash
echo "gh $*" >> "$FAKE_GH_LOG"
case "$1 $2" in
  "release view")
    [ -n "${FAKE_RELEASE_EXISTS:-}" ] || exit 1
    case " $* " in *" --json "*) echo "${FAKE_ASSET_COUNT:-0}" ;; esac
    exit 0 ;;
  "release download")
    [ -n "${FAKE_PUBLISHED_SUMS:-}" ] || { echo "no assets match the file pattern" >&2; exit 1; }
    dir=""; prev=""
    for a in "$@"; do [ "$prev" = "--dir" ] && dir="$a"; prev="$a"; done
    cp "$FAKE_PUBLISHED_SUMS" "$dir/SHA256SUMS"
    exit 0 ;;
  "release upload"|"release create") exit 0 ;;
esac
echo "unexpected gh call: $*" >&2
exit 2
"""


@unittest.skipUnless(_posix_bash(), "a POSIX bash is not available")
class TestReleaseUploadScript(unittest.TestCase):
    """release.yml publish "Refuse to replace an existing release's assets" (before attestation)
    followed by 'Upload to the GitHub release', run with a fake `gh` on PATH."""

    def setUp(self):
        self._tmp = tempfile.mkdtemp(prefix="release-upload-")
        tmp = Path(self._tmp)
        (tmp / "bin").mkdir()
        (tmp / "bin" / "gh").write_bytes(FAKE_GH.encode())
        (tmp / "bin" / "gh").chmod(0o755)
        (tmp / "work" / "dist").mkdir(parents=True)
        (tmp / "work" / "dist" / "ml-ai-skills-1.0.0.tar.gz").write_bytes(b"tarball")
        self.sums = tmp / "work" / "dist" / "SHA256SUMS"
        self.sums.write_bytes(b"abc123  ml-ai-skills-1.0.0.tar.gz\n")
        self.log = tmp / "gh.log"
        check = workflow_step("release.yml", "publish", "Refuse to replace an existing release")
        (tmp / "check.sh").write_bytes(str(check["run"]).encode())
        step = workflow_step("release.yml", "publish", "Upload to the GitHub release")
        (tmp / "upload.sh").write_bytes(str(step["run"]).encode())

    def tearDown(self):
        shutil.rmtree(self._tmp, ignore_errors=True)

    def upload(self, **fake) -> tuple[int, str, list[str]]:
        tmp = Path(self._tmp)
        (tmp / "rt").mkdir(exist_ok=True)
        gh_output = tmp / "rt" / "github-output"
        gh_output.write_bytes(b"")
        env = {**os.environ, "TAG": "v1.0.0", "RUNNER_TEMP": str(tmp / "rt"), "FAKE_GH_LOG": str(self.log),
               "GITHUB_OUTPUT": str(gh_output),
               "PATH": str(tmp / "bin") + os.pathsep + os.environ.get("PATH", ""), **fake}
        r = subprocess.run([_posix_bash(), str(tmp / "check.sh")], cwd=tmp / "work", env=env,
                           capture_output=True, text=True)
        out = r.stdout + r.stderr
        if r.returncode == 0:  # the job stops at a failing check, before attestation and upload
            outputs = dict(line.split("=", 1) for line in gh_output.read_text(encoding="utf-8").splitlines() if line)
            r = subprocess.run([_posix_bash(), str(tmp / "upload.sh")], cwd=tmp / "work",
                               env={**env, "MODE": outputs.get("mode", "")}, capture_output=True, text=True)
            out += r.stdout + r.stderr
        calls = self.log.read_text(encoding="utf-8").splitlines() if self.log.exists() else []
        return r.returncode, out, calls

    def published(self, content: bytes) -> str:
        path = Path(self._tmp) / "published-SHA256SUMS"
        path.write_bytes(content)
        return str(path)

    def test_refusal_runs_before_attestation_and_upload(self):
        # A refused re-point must not leave a provenance attestation for the rejected digest.
        doc = aw.parse_yaml((WORKFLOWS / "release.yml").read_text(encoding="utf-8"))
        steps = doc["jobs"]["publish"]["steps"]
        names = [str(s.get("name", "")) for s in steps]
        refuse = next(i for i, n in enumerate(names) if n.startswith("Refuse to replace an existing release"))
        attest = next(i for i, s in enumerate(steps) if "attest-build-provenance" in str(s.get("uses", "")))
        upload = names.index("Upload to the GitHub release")
        self.assertLess(refuse, attest, names)
        self.assertLess(attest, upload, names)
        self.assertEqual(steps[refuse].get("id"), "existing")
        self.assertIn("steps.existing.outputs.mode", str(steps[upload]["env"]["MODE"]))
        self.assertNotIn("gh release upload", str(steps[refuse]["run"]))
        self.assertNotIn("gh release create", str(steps[refuse]["run"]))

    def test_unknown_mode_does_not_upload(self):
        tmp = Path(self._tmp)
        (tmp / "rt").mkdir(exist_ok=True)
        env = {**os.environ, "TAG": "v1.0.0", "MODE": "", "FAKE_GH_LOG": str(self.log),
               "PATH": str(tmp / "bin") + os.pathsep + os.environ.get("PATH", "")}
        r = subprocess.run([_posix_bash(), str(tmp / "upload.sh")], cwd=tmp / "work", env=env,
                           capture_output=True, text=True)
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertFalse(self.log.exists())

    def test_new_release_is_created(self):
        code, out, calls = self.upload()
        self.assertEqual(code, 0, out)
        self.assertTrue(calls[-1].startswith("gh release create v1.0.0"), calls)

    def test_identical_rerun_reuploads(self):
        code, out, calls = self.upload(FAKE_RELEASE_EXISTS="1", FAKE_PUBLISHED_SUMS=self.published(self.sums.read_bytes()))
        self.assertEqual(code, 0, out)
        self.assertTrue(calls[-1].startswith("gh release upload v1.0.0") and calls[-1].endswith("--clobber"), calls)

    def test_different_bytes_under_an_existing_tag_are_refused(self):
        # The audit scenario: the tag was re-pointed at another commit with the same VERSION.
        code, out, calls = self.upload(FAKE_RELEASE_EXISTS="1",
                                       FAKE_PUBLISHED_SUMS=self.published(b"fff999  ml-ai-skills-1.0.0.tar.gz\n"))
        self.assertEqual(code, 1, out)
        self.assertIn("already exists with different assets", out)
        self.assertFalse(any("release upload" in c for c in calls), calls)

    def test_existing_release_without_assets_gets_them_without_clobber(self):
        code, out, calls = self.upload(FAKE_RELEASE_EXISTS="1", FAKE_ASSET_COUNT="0")
        self.assertEqual(code, 0, out)
        self.assertTrue(calls[-1].startswith("gh release upload v1.0.0"), calls)
        self.assertNotIn("--clobber", calls[-1])

    def test_existing_assets_without_a_readable_sha256sums_are_refused(self):
        code, out, calls = self.upload(FAKE_RELEASE_EXISTS="1", FAKE_ASSET_COUNT="2")
        self.assertEqual(code, 1, out)
        self.assertFalse(any("release upload" in c for c in calls), calls)



# --------------------------------------------------------------------------- ci.yml base commit, aggregator, locks

def _heredoc_body(run: str) -> str:
    """The Python body of the (first) `<<'PY'` heredoc in a run: script."""
    return run.split("<<'PY'\n", 1)[1].split("\nPY\n", 1)[0] + "\n"


def _ci() -> dict:
    return aw.parse_yaml((WORKFLOWS / "ci.yml").read_text(encoding="utf-8"))


BASE_STEP_JOBS = ("validate", "evals-offline", "deps")
ZERO_SHA = "0" * 40


class TestBaseCommitStepWiring(unittest.TestCase):
    def test_every_copy_of_the_base_step_is_identical_and_has_full_history(self):
        doc = _ci()
        steps = {job: workflow_step("ci.yml", job, "Resolve the base commit") for job in BASE_STEP_JOBS}
        first = steps[BASE_STEP_JOBS[0]]
        for job, step in steps.items():
            with self.subTest(job=job):
                self.assertEqual(step, first)
                self.assertEqual(step.get("id"), "base")
                checkout = doc["jobs"][job]["steps"][0]
                self.assertTrue(str(checkout["uses"]).startswith("actions/checkout@"))
                self.assertEqual(str(checkout["with"].get("fetch-depth")), "0")

    def test_no_job_compares_against_head_parent_or_shallow_history(self):
        text = (WORKFLOWS / "ci.yml").read_text(encoding="utf-8")
        self.assertNotIn("--base-ref HEAD^1", text)
        self.assertNotIn("fetch-depth: 2", text)
        contract = workflow_step("ci.yml", "validate", "Public contract matches its lock")
        self.assertIn('--base-ref "$BASE_SHA"', str(contract["run"]))
        self.assertIn("steps.base.outputs.sha", str(contract["env"]["BASE_SHA"]))
        gate = workflow_step("ci.yml", "evals-offline", "Routing benchmark")
        self.assertIn("steps.base.outputs.sha", str(gate["env"]["ROUTING_BASE_SHA"]))


@unittest.skipUnless(shutil.which("git") and _posix_bash(), "git and a POSIX bash are needed")
class TestBaseCommitStep(unittest.TestCase):
    """ci.yml "Resolve the base commit", run with bash against temp git repositories.

    origin: A <- B (main). The clone adds C (a feature branch off A) and M = merge of C into B,
    shaped like GitHub's pull_request test merge commit (parents B, C)."""

    @classmethod
    def setUpClass(cls):
        cls.script = str(workflow_step("ci.yml", "validate", "Resolve the base commit")["run"])
        cls._tmp = tempfile.mkdtemp(prefix="base-step-")
        tmp = Path(cls._tmp)
        origin = tmp / "origin"
        origin.mkdir()
        _git(origin, "init", "-q", "-b", "main")
        cls.sha = {}
        for name in ("A", "B"):
            (origin / "f.txt").write_text(name, encoding="utf-8")
            _git(origin, "add", "-A")
            _git(origin, "commit", "-q", "-m", name)
            cls.sha[name] = cls.rev(origin, "HEAD")
        cls.repo = tmp / "clone"
        _git(tmp, "clone", "-q", str(origin), str(cls.repo))
        _git(cls.repo, "checkout", "-q", "-b", "feature", cls.sha["A"])
        (cls.repo / "g.txt").write_text("C", encoding="utf-8")
        _git(cls.repo, "add", "-A")
        _git(cls.repo, "commit", "-q", "-m", "C")
        cls.sha["C"] = cls.rev(cls.repo, "HEAD")
        _git(cls.repo, "checkout", "-q", "--detach", cls.sha["B"])
        _git(cls.repo, "merge", "-q", "--no-ff", "-m", "M", cls.sha["C"])
        cls.sha["M"] = cls.rev(cls.repo, "HEAD")
        # D (child of B) and E (child of A) exist in origin after the clone, but no ref points to
        # them: what a force push leaves behind. Like GitHub, origin serves them by sha.
        _git(origin, "config", "uploadpack.allowAnySHA1InWant", "true")
        for name, parent in (("D", "B"), ("E", "A")):
            _git(origin, "checkout", "-q", "--detach", cls.sha[parent])
            (origin / "f.txt").write_text(name, encoding="utf-8")
            _git(origin, "commit", "-q", "-am", name)
            cls.sha[name] = cls.rev(origin, "HEAD")
        _git(origin, "checkout", "-q", "main")
        (tmp / "step.sh").write_text(cls.script, encoding="utf-8", newline="\n")
        root = tmp / "root"
        root.mkdir()
        _git(root, "init", "-q", "-b", "main")
        _git(root, "commit", "-q", "--allow-empty", "-m", "root")
        cls.root_repo = root

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls._tmp, ignore_errors=True)

    @staticmethod
    def rev(repo: Path, ref: str) -> str:
        return subprocess.run(["git", "-C", str(repo), "rev-parse", ref], capture_output=True, text=True,
                              check=True, timeout=60).stdout.strip()

    def run_step(self, head: str, repo: Path | None = None, **event: str) -> tuple[int, str, str]:
        repo = repo or self.repo
        _git(repo, "checkout", "-q", "--detach", self.sha.get(head, head))
        out = Path(self._tmp) / "github-output"
        out.write_bytes(b"")
        env = {**os.environ, "GITHUB_OUTPUT": str(out), "EVENT_NAME": "push", "PR_BASE_SHA": "", "PR_HEAD_SHA": "",
               "PUSH_BEFORE": "", "DEFAULT_BRANCH": "main", **event}
        r = subprocess.run([_posix_bash(), "--noprofile", "--norc", "-e", str(Path(self._tmp) / "step.sh")],
                           cwd=repo, env=env, capture_output=True, text=True, timeout=120)
        outputs = dict(line.split("=", 1) for line in out.read_text(encoding="utf-8").splitlines() if "=" in line)
        return r.returncode, outputs.get("sha", ""), r.stdout + r.stderr

    def assertBase(self, expected: str, head: str, **event: str) -> str:
        code, sha, log = self.run_step(head, **event)
        self.assertEqual(code, 0, log)
        self.assertEqual(sha, self.sha[expected], log)
        return log

    def test_pull_request_merge_commit_uses_its_first_parent(self):
        log = self.assertBase("B", "M", EVENT_NAME="pull_request", PR_BASE_SHA=self.sha["B"], PR_HEAD_SHA=self.sha["C"])
        self.assertIn("first parent of the test merge commit", log)

    def test_pull_request_with_a_stale_payload_base_uses_the_newer_merge_parent(self):
        # The payload's base sha can lag the tip GitHub merged against (the base branch moved).
        self.assertBase("B", "M", EVENT_NAME="pull_request", PR_BASE_SHA=self.sha["A"], PR_HEAD_SHA=self.sha["C"])

    def test_pull_request_checked_out_at_the_head_uses_the_merge_base(self):
        log = self.assertBase("A", "C", EVENT_NAME="pull_request", PR_BASE_SHA=self.sha["B"], PR_HEAD_SHA=self.sha["C"])
        self.assertIn("merge base of HEAD and the payload base", log)

    def test_push_uses_the_commit_before_the_push(self):
        # Not HEAD^1: a push of several commits is compared against where the branch was before.
        self.assertBase("A", "M", PUSH_BEFORE=self.sha["A"])

    def test_new_branch_push_and_manual_run_use_the_merge_base_with_the_default_branch(self):
        self.assertBase("A", "C", PUSH_BEFORE=ZERO_SHA)
        self.assertBase("A", "C", EVENT_NAME="workflow_dispatch")

    def test_on_the_default_branch_tip_the_fallback_is_the_first_parent(self):
        self.assertBase("A", "B", EVENT_NAME="workflow_dispatch")
        self.assertBase("A", "B", PUSH_BEFORE=ZERO_SHA)

    def test_malformed_or_missing_shas_fail(self):
        cases = {
            "pr base not hex": dict(EVENT_NAME="pull_request", PR_BASE_SHA="main; touch pwned", PR_HEAD_SHA=""),
            "pr base empty": dict(EVENT_NAME="pull_request", PR_BASE_SHA="", PR_HEAD_SHA=""),
            "pr base not in clone": dict(EVENT_NAME="pull_request", PR_BASE_SHA="1" * 40, PR_HEAD_SHA=""),
            "push before not hex": dict(PUSH_BEFORE="$(touch pwned)"),
            "push before not in clone (force push)": dict(PUSH_BEFORE="2" * 40),
        }
        for label, event in cases.items():
            with self.subTest(case=label):
                code, sha, log = self.run_step("M", **event)
                self.assertEqual(code, 1, log)
                self.assertEqual(sha, "")
                self.assertIn("::error::", log)
        self.assertFalse((self.repo / "pwned").exists())

    def test_an_unreferenced_base_is_fetched_by_sha_before_failing(self):
        # A force push leaves `before` (or a PR's base) unreferenced, so the clone lacks it.
        # Re-running the same push run would fail forever, so the step fetches it by sha first.
        for name in ("D", "E"):
            r = subprocess.run(["git", "-C", str(self.repo), "cat-file", "-e", self.sha[name] + "^{commit}"],
                               capture_output=True, timeout=60)
            self.assertNotEqual(r.returncode, 0, f"{name} must start out missing from the clone")
        log = self.assertBase("D", "M", PUSH_BEFORE=self.sha["D"])
        self.assertIn(f"fetched {self.sha['D']} by sha", log)
        log = self.assertBase("A", "C", EVENT_NAME="pull_request", PR_BASE_SHA=self.sha["E"], PR_HEAD_SHA=self.sha["C"])
        self.assertIn(f"fetched {self.sha['E']} by sha", log)

    def test_an_unfetchable_base_says_how_to_recover(self):
        code, _, log = self.run_step("M", PUSH_BEFORE="3" * 40)
        self.assertEqual(code, 1, log)
        self.assertIn("could not be fetched", log)
        self.assertIn("push a new commit on top", log)  # not "re-run": a re-run reuses the payload
        self.assertNotIn("re-run with workflow_dispatch", log)
        code, _, log = self.run_step("M", EVENT_NAME="pull_request", PR_BASE_SHA="4" * 40, PR_HEAD_SHA=self.sha["C"])
        self.assertEqual(code, 1, log)
        self.assertIn("push to the PR branch", log)

    def test_root_commit_without_parent_fails(self):
        head = self.rev(self.root_repo, "HEAD")
        code, sha, log = self.run_step(head, repo=self.root_repo, EVENT_NAME="workflow_dispatch")
        self.assertEqual(code, 1, log)
        self.assertIn("no base commit", log)


class TestAllRequiredJob(unittest.TestCase):
    def test_needs_every_job_except_the_manual_link_check(self):
        jobs = _ci()["jobs"]
        agg = jobs["all-required"]
        self.assertEqual(str(agg["if"]).strip(), "always()")
        self.assertEqual(sorted(agg["needs"]), sorted(set(jobs) - {"all-required", "link-check"}))
        self.assertEqual(agg.get("permissions"), {})

    def run_aggregator(self, needs) -> tuple[int, str]:
        body = _heredoc_body(str(workflow_step("ci.yml", "all-required", "Every needed job succeeded")["run"]))
        with tempfile.TemporaryDirectory() as tmp:
            script = Path(tmp) / "agg.py"
            script.write_text(body, encoding="utf-8")
            r = subprocess.run([sys.executable, str(script)], env={**os.environ, "NEEDS_JSON": json.dumps(needs)},
                               capture_output=True, text=True, timeout=60)
        return r.returncode, r.stdout + r.stderr

    def test_passes_only_when_every_needed_job_succeeded(self):
        ok = {"lint": {"result": "success", "outputs": {}}, "deps": {"result": "success", "outputs": {}}}
        self.assertEqual(self.run_aggregator(ok)[0], 0)
        for result in ("failure", "cancelled", "skipped", None):
            with self.subTest(result=result):
                code, log = self.run_aggregator({**ok, "deps": {"result": result}})
                self.assertEqual(code, 1, log)
                self.assertIn("::error::required job 'deps'", log)
        self.assertEqual(self.run_aggregator({})[0], 1)  # nothing needed = misconfigured, not a pass


class TestNoSwallowedGates(unittest.TestCase):
    def test_workflows_have_no_continue_on_error_or_swallowed_failures(self):
        # The audit's WARN codes for these are part of --strict; this pins them to zero by name.
        for f in sorted(WORKFLOWS.glob("*.y*ml")):
            with self.subTest(file=f.name):
                found = [x for x in aw.audit_file(f)
                         if x.code in ("continue-on-error", "swallowed-failure", "pipe-without-pipefail")]
                self.assertEqual(found, [])


CONSTRAINTS = REPO_ROOT / "constraints"
LOCKS = {"requirements-examples.txt": "examples-linux-cp313.txt", "requirements-dev.txt": "dev-linux-cp313.txt"}


def _norm(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def _pins(path: Path) -> dict[str, str]:
    """`name==version` lines of a requirements/lock file (comments and options skipped)."""
    out = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.split("#", 1)[0].strip()
        if not line or line.startswith("-"):
            continue
        name, sep, version = line.partition("==")
        assert sep and name.strip() and version.strip(), (path.name, line)
        out[_norm(name.strip())] = version.strip()
    return out


class TestDependencyLocks(unittest.TestCase):
    def test_locks_are_generated_sorted_and_cover_every_direct_pin(self):
        for req, lock in LOCKS.items():
            with self.subTest(lock=lock):
                path = CONSTRAINTS / lock
                text = path.read_text(encoding="utf-8")
                self.assertTrue(text.startswith(f"# GENERATED lock for {req}:"), text[:200])
                # Resolved where CI installs it: a lock generated on Windows or macOS lists other packages.
                self.assertIn("\n# resolved on linux x86_64 with CPython 3.13.\n", text)
                self.assertNotIn("\r", text)
                lines = [ln for ln in text.splitlines() if ln and not ln.startswith("#")]
                self.assertEqual(lines, sorted(lines, key=str.lower))
                locked, direct = _pins(path), _pins(REPO_ROOT / req)
                self.assertTrue(direct)
                for name, version in direct.items():
                    self.assertEqual(locked.get(name), version, f"{req} pins {name}=={version}; {lock} disagrees")
                self.assertGreater(len(locked), len(direct))  # transitive packages are locked too

    def test_ci_installs_every_requirements_file_with_its_lock(self):
        checked = 0
        for f in sorted(WORKFLOWS.glob("*.y*ml")):
            doc = aw.parse_yaml(f.read_text(encoding="utf-8"))
            for job_id, job in doc["jobs"].items():
                for step in job.get("steps") or []:
                    for line in str(step.get("run", "")).replace("\\\n", " ").splitlines():
                        if "pip install" not in line or "--dry-run" in line:
                            continue
                        for req, lock in LOCKS.items():
                            if req in line:
                                checked += 1
                                with self.subTest(file=f.name, job=job_id, line=line.strip()[:90]):
                                    self.assertIn(f"-c constraints/{lock}", line)
        self.assertGreaterEqual(checked, 6)  # ci.yml: lint, unit-with-dev-deps, examples (x2), security; release.yml

    def lock_check(self, report: dict, lock_text: str) -> tuple[int, str]:
        body = _heredoc_body(str(workflow_step("ci.yml", "deps", "Each lock is complete")["run"]))
        with tempfile.TemporaryDirectory() as tmp:
            Path(tmp, "check.py").write_text(body, encoding="utf-8")
            Path(tmp, "report.json").write_text(json.dumps(report), encoding="utf-8")
            Path(tmp, "lock.txt").write_text(lock_text, encoding="utf-8")
            r = subprocess.run([sys.executable, "check.py", "report.json", "lock.txt"], cwd=tmp, capture_output=True,
                               text=True, timeout=60)
        return r.returncode, r.stdout + r.stderr

    @staticmethod
    def report(**pkgs: str) -> dict:
        return {"version": "1", "install": [{"metadata": {"name": n.replace("__", "-"), "version": v}}
                                            for n, v in pkgs.items()]}

    def test_lock_check_requires_an_exact_match(self):
        report = self.report(numpy="2.5.4", typing_extensions="4.16.0", torch="2.14.1+cpu")
        good = "# GENERATED\nnumpy==2.5.4\ntorch==2.14.1+cpu\ntyping-extensions==4.16.0\n"
        self.assertEqual(self.lock_check(report, good)[0], 0)
        cases = {
            "missing": ("numpy==2.5.4\ntorch==2.14.1+cpu\n", "would be installed but is not in the lock"),
            "stale": (good + "six==1.17.0\n", "is in the lock but nothing requires it"),
            "version": (good.replace("2.5.4", "2.5.3"), "pip resolved 2.5.4, the lock has 2.5.3"),
            "malformed": (good + "scipy>=1.0\n", "not a 'name==version' line"),
        }
        for label, (lock, message) in cases.items():
            with self.subTest(case=label):
                code, log = self.lock_check(report, lock)
                self.assertEqual(code, 1, log)
                self.assertIn(message, log)

    def test_locks_are_regenerated_and_uploaded_after_the_consistency_check_fails(self):
        # A bumped pin without its lock fails the consistency step; the regeneration step must
        # still run (default `if: success()` would skip it) so the upload has something to upload.
        regen = workflow_step("ci.yml", "deps", "When requirements changed")
        upload = workflow_step("ci.yml", "deps", "Upload regenerated locks")
        self.assertIn("!cancelled()", str(regen.get("if", "")))
        self.assertIn(str(upload.get("if", "")), ("always()", "${{ always() }}", "${{ !cancelled() }}", "!cancelled()"))

    def test_lock_generator_output_round_trips_through_the_checker(self):
        body = _heredoc_body(str(workflow_step("ci.yml", "deps", "When requirements changed")["run"]))
        report = self.report(Zlib__ng="1.0", numpy="2.5.4", PyYAML="6.0.3")
        with tempfile.TemporaryDirectory() as tmp:
            Path(tmp, "gen.py").write_text(body, encoding="utf-8")
            Path(tmp, "report.json").write_text(json.dumps(report), encoding="utf-8")
            r = subprocess.run([sys.executable, "gen.py", "report.json", "requirements-examples.txt"], cwd=tmp,
                               capture_output=True, text=True, timeout=60)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertTrue(r.stdout.startswith("# GENERATED lock for requirements-examples.txt:"))
        self.assertEqual([ln for ln in r.stdout.splitlines() if not ln.startswith("#")],
                         ["numpy==2.5.4", "PyYAML==6.0.3", "Zlib-ng==1.0"])
        self.assertEqual(self.lock_check(report, r.stdout)[0], 0)


class TestDependabotGroups(unittest.TestCase):
    def test_example_pins_are_ungrouped_except_small_coupled_sets(self):
        # A wildcard group (PR #3) bumped interdependent example pins together and could not resolve.
        doc = aw.parse_yaml((REPO_ROOT / ".github" / "dependabot.yml").read_text(encoding="utf-8"))
        pip = [u for u in doc["updates"] if u["package-ecosystem"] == "pip"]
        self.assertEqual(len(pip), 1)
        groups = pip[0]["groups"]
        dev, examples = _pins(REPO_ROOT / "requirements-dev.txt"), _pins(REPO_ROOT / "requirements-examples.txt")
        self.assertEqual(sorted(_norm(p) for p in groups["dev-tools"]["patterns"]), sorted(dev))
        self.assertEqual(sorted(groups["dev-tools"]["update-types"]), ["minor", "patch"])
        coupled = {name: g for name, g in groups.items() if name != "dev-tools"}
        self.assertEqual(sorted(coupled), ["torch", "transformers-tokenizers"])
        for name, group in coupled.items():
            with self.subTest(group=name):
                patterns = [_norm(p) for p in group["patterns"]]
                self.assertEqual(len(patterns), 2)
                for p in patterns:
                    self.assertIn(p, examples)  # exact names of pinned packages, no wildcards


class TestCoverageConfig(unittest.TestCase):
    def test_subprocess_coverage_is_configured_and_combined_before_the_floor(self):
        cfg = configparser.ConfigParser()
        cfg.read(REPO_ROOT / ".coveragerc", encoding="utf-8")
        self.assertEqual(cfg.get("run", "patch").strip(), "subprocess")
        self.assertTrue(cfg.getboolean("run", "parallel"))
        self.assertTrue(cfg.getboolean("run", "branch"))
        self.assertEqual(cfg.get("run", "source").split(), ["scripts", "schema"])
        run = str(workflow_step("ci.yml", "unit-with-dev-deps", "Coverage report")["run"])
        self.assertLess(run.index("coverage combine"), run.index("--fail-under"))
        floor = int(re.search(r"--fail-under=(\d+)", run).group(1))
        self.assertGreaterEqual(floor, 84)  # never below the floor that was set before subprocess coverage
        dev = _pins(REPO_ROOT / "requirements-dev.txt")
        major, minor = (int(x) for x in dev["coverage"].split(".")[:2])
        self.assertGreaterEqual((major, minor), (7, 10))  # `patch = subprocess` needs coverage 7.10+

    def test_the_subprocess_guard_runs_before_combine_and_has_a_real_minimum(self):
        step = workflow_step("ci.yml", "unit-with-dev-deps", "Coverage report")
        run = str(step["run"])
        self.assertLess(run.index("<<'PY'"), run.index("coverage combine"))  # combine deletes the child files
        self.assertGreaterEqual(int(step["env"]["MIN_SUBPROCESS_LINES"]), 100)


try:
    from coverage import CoverageData
except ImportError:
    CoverageData = None


@unittest.skipIf(CoverageData is None, "coverage is not installed (requirements-dev.txt): the guard reads its data files")
class TestSubprocessCoverageGuard(unittest.TestCase):
    """ci.yml's pre-combine guard, run against hand-written coverage data files."""

    def run_guard(self, files: dict[str, dict[str, list[int]]], minimum: int = 3) -> tuple[int, str]:
        body = _heredoc_body(str(workflow_step("ci.yml", "unit-with-dev-deps", "Coverage report")["run"]))
        with tempfile.TemporaryDirectory() as tmp:
            for name, lines in files.items():
                data = CoverageData(basename=str(Path(tmp, name)))
                data.add_lines({str(Path(tmp, f)): ns for f, ns in lines.items()})
                data.write()
            Path(tmp, "guard.py").write_text(body, encoding="utf-8")
            env = {k: v for k, v in os.environ.items() if not k.startswith("COVERAGE_")}
            r = subprocess.run([sys.executable, "guard.py"], cwd=tmp, capture_output=True, text=True, timeout=60,
                               env={**env, "MIN_SUBPROCESS_LINES": str(minimum)})
        return r.returncode, r.stdout + r.stderr

    PARENT = {"a.py": list(range(1, 50)), "b.py": [1, 2, 3]}

    def test_passes_when_children_measured_lines_the_parent_did_not(self):
        code, log = self.run_guard({".coverage.h.1.x": self.PARENT, ".coverage.h.2.y": {"b.py": [1, 4, 5, 6]}})
        self.assertEqual(code, 0, log)
        self.assertIn("3 lines were measured only in subprocesses", log)

    def test_fails_when_children_wrote_files_but_added_nothing(self):
        cases = {
            "empty child files": {".coverage.h.1.x": self.PARENT, ".coverage.h.2.y": {}, ".coverage.h.3.z": {}},
            "children repeat the parent": {".coverage.h.1.x": self.PARENT, ".coverage.h.2.y": {"a.py": [1, 2, 3]}},
            "below the minimum": {".coverage.h.1.x": self.PARENT, ".coverage.h.2.y": {"b.py": [4, 5]}},
            "only the parent's file": {".coverage.h.1.x": self.PARENT},
            "no data at all": {},
        }
        for label, files in cases.items():
            with self.subTest(case=label):
                code, log = self.run_guard(files)
                self.assertEqual(code, 1, log)
                self.assertIn("::error::", log)


# --------------------------------------------------------------------------- freshness-scheduled.yml

FAKE_SCRIPT = """import json, os, sys
mode = os.environ.get("FAKE_{name}", "ok")
code = int(os.environ.get("FAKE_{name}_EXIT", "0"))
out = sys.argv[sys.argv.index("--json-out") + 1] if "--json-out" in sys.argv else None
if mode == "crash":
    sys.exit(code or 1)
report = {{"status": os.environ.get("FAKE_{name}_STATUS", "OK")}}
if out:
    open(out, "w", encoding="utf-8").write(json.dumps(report))
    print("summary: " + report["status"])
else:
    print(json.dumps(report))
sys.exit(code)
"""


@unittest.skipUnless(_posix_bash(), "a POSIX bash is not available")
class TestFreshnessScheduledSteps(unittest.TestCase):
    """Runs freshness-scheduled.yml's steps in order with bash, the way the runner does (`bash -e`,
    GITHUB_OUTPUT per step, `if: always()`), offline, in a temp copy of the tree."""

    @classmethod
    def setUpClass(cls):
        cls.doc = aw.parse_yaml((WORKFLOWS / "freshness-scheduled.yml").read_text(encoding="utf-8"))
        cls.job = cls.doc["jobs"]["verify-online"]

    def setUp(self):
        self._tmp = tempfile.mkdtemp(prefix="freshness-steps-")
        self.work = Path(self._tmp) / "work"
        self.work.mkdir()
        shim = Path(self._tmp) / "bin"
        shim.mkdir()
        exe = Path(sys.executable).as_posix()
        (shim / "python").write_bytes(f'#!/usr/bin/env bash\nexec "{exe}" "$@"\n'.encode())
        (shim / "python").chmod(0o755)
        self.path = str(shim) + os.pathsep + os.environ.get("PATH", "")

    def tearDown(self):
        shutil.rmtree(self._tmp, ignore_errors=True)

    def copy_tree(self) -> None:
        ignore = shutil.ignore_patterns("__pycache__")
        for d in tu_sl.iter_skill_dirs(REPO_ROOT):
            shutil.copytree(d, self.work / d.name, ignore=ignore)
        for rel in ("scripts", "schema", "docs", "tests", "evals"):
            shutil.copytree(REPO_ROOT / rel, self.work / rel, ignore=ignore)

    def fake_scripts(self) -> None:
        (self.work / "scripts").mkdir()
        for script, name in (("verify_online.py", "ONLINE"), ("check_freshness.py", "OFFLINE"),
                             ("check_claims.py", "CLAIMS")):
            (self.work / "scripts" / script).write_text(FAKE_SCRIPT.format(name=name), encoding="utf-8")

    def run_job(self, **extra_env: str) -> tuple[dict, str, str]:
        """{step name: exit code or None if skipped}, the step summary, and the combined log."""
        results, outputs, log, failed = {}, {}, [], False
        summary = Path(self._tmp) / "summary.md"
        for idx, step in enumerate(self.job["steps"]):
            name = str(step.get("name") or step.get("uses"))
            if "run" not in step:
                results[name] = None
                continue
            if failed and str(step.get("if", "")).strip() != "always()":
                results[name] = None
                continue
            env = {k: v for k, v in os.environ.items() if k.lower() not in ("no_proxy",)}
            env.update(PATH=self.path, GITHUB_STEP_SUMMARY=str(summary), PYTHONUTF8="1")
            for key, value in (step.get("env") or {}).items():
                m = re.fullmatch(r"\$\{\{\s*steps\.(\w+)\.outputs\.(\w+)\s*\}\}", str(value).strip())
                self.assertIsNotNone(m, f"step env {key}={value!r}: only steps.*.outputs.* is simulated")
                env[key] = outputs.get(m.group(1), {}).get(m.group(2), "")
            env.update(extra_env)
            out = Path(self._tmp) / f"output-{idx}"
            out.write_bytes(b"")
            env["GITHUB_OUTPUT"] = str(out)
            script = Path(self._tmp) / f"step-{idx}.sh"
            script.write_text(str(step["run"]), encoding="utf-8", newline="\n")
            timeout = 60 * int(step.get("timeout-minutes") or self.job["timeout-minutes"])
            r = subprocess.run([_posix_bash(), "--noprofile", "--norc", "-e", str(script)], cwd=self.work, env=env,
                               capture_output=True, text=True, encoding="utf-8", timeout=min(timeout, 600))
            log.append(f"== {name}: {r.returncode}\n{r.stdout}{r.stderr}")
            results[name] = r.returncode
            failed = failed or r.returncode != 0
            if step.get("id"):
                outputs[str(step["id"])] = dict(line.split("=", 1) for line in
                                                 out.read_text(encoding="utf-8").splitlines() if "=" in line)
        return results, summary.read_text(encoding="utf-8") if summary.exists() else "", "\n".join(log)

    def uploaded_paths(self) -> list[str]:
        upload = next(s for s in self.job["steps"] if "upload-artifact" in str(s.get("uses", "")))
        self.assertEqual(str(upload.get("if")).strip(), "always()")
        return [p.strip() for p in str(upload["with"]["path"]).splitlines() if p.strip()]

    def test_workflow_can_be_run_by_hand_and_only_reads(self):
        self.assertIn("workflow_dispatch", aw._triggers(self.doc.get("on", self.doc.get("true"))))
        self.assertEqual(self.doc["permissions"], {"contents": "read"})

    def test_real_scripts_offline_report_skipped_fail_the_run_and_leave_every_artifact(self):
        # A closed local port as the proxy: every request fails fast and nothing leaves the machine.
        self.copy_tree()
        dead = "http://127.0.0.1:9"
        results, summary, log = self.run_job(HTTPS_PROXY=dead, HTTP_PROXY=dead, https_proxy=dead, http_proxy=dead)
        self.assertEqual(results["Online check (automated-doc-check)"], 0, log)  # the code is recorded, not raised
        self.assertEqual(results["Result"], 2, log)
        self.assertIn("SKIPPED", log)
        report = json.loads((self.work / "verify-online-report.json").read_text(encoding="utf-8"))
        self.assertEqual(report["status"], "SKIPPED")
        self.assertIn("verify_online", summary)
        for rel in self.uploaded_paths():
            with self.subTest(artifact=rel):
                self.assertTrue((self.work / rel).is_file(), log)
        for rel in ("freshness-offline.json", "claims-check.json"):
            json.loads((self.work / rel).read_text(encoding="utf-8"))

    def test_result_reflects_every_failure_mode(self):
        self.fake_scripts()
        cases = {
            "all clean": ({}, 0, "No online findings."),
            "online findings": ({"FAKE_ONLINE_STATUS": "FINDINGS", "FAKE_ONLINE_EXIT": "1"}, 1, "Findings for human"),
            "online skipped": ({"FAKE_ONLINE_STATUS": "SKIPPED", "FAKE_ONLINE_EXIT": "2"}, 2, "SKIPPED"),
            "online incomplete": ({"FAKE_ONLINE_STATUS": "INCOMPLETE", "FAKE_ONLINE_EXIT": "1"}, 1, "did not finish"),
            "online crash, no report": ({"FAKE_ONLINE": "crash", "FAKE_ONLINE_EXIT": "1"}, 1, "no readable report"),
            "offline STALE": ({"FAKE_OFFLINE_EXIT": "1"}, 1, "check_freshness.py reported findings"),
            "offline crash": ({"FAKE_OFFLINE": "crash", "FAKE_OFFLINE_EXIT": "3"}, 1,
                              "check_freshness.py produced no readable"),
            "offline usage error": ({"FAKE_OFFLINE_EXIT": "2"}, 1, "check_freshness.py exited '2'"),
            "claims error": ({"FAKE_CLAIMS_EXIT": "1"}, 1, "check_claims.py reported findings"),
        }
        for label, (env, expected, message) in cases.items():
            with self.subTest(case=label):
                for rel in self.uploaded_paths():
                    (self.work / rel).unlink(missing_ok=True)
                results, _summary, log = self.run_job(**env)
                self.assertEqual(results["Result"], expected, log)
                self.assertIn(message, log)
                # Steps before Result never fail the job by themselves; every code reaches Result.
                self.assertEqual([c for n, c in results.items() if n != "Result" and c], [], log)


if __name__ == "__main__":
    unittest.main()
