"""Tests for scripts/audit_workflows.py - the GitHub Actions workflow audit.

Each bad fixture is the good fixture with exactly one defect, so a test proves
both that the defect is caught and that nothing else in the file is flagged.
"""
from __future__ import annotations

import contextlib
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

from testutil import REPO_ROOT
from testutil import sl as tu_sl

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
                    "-c", "core.autocrlf=false", "-C", str(root), *args], check=True, capture_output=True)


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

SABOTAGE = textwrap.dedent("""

    _route_unsabotaged = route


    def route(task_text, top_k=5, min_score=None):  # test sabotage: drop every third query's hits
        intent, hits = _route_unsabotaged(task_text, top_k=top_k, min_score=min_score)
        return intent, (hits if len(task_text) % 3 else [])
    """)


@unittest.skipUnless(shutil.which("git"), "git is not installed")
class TestRoutingGateScript(unittest.TestCase):
    """ci.yml evals-offline 'Routing benchmark (test split) must not regress', run in a temp git
    repo: parent commit = this tree, HEAD = the change under test."""

    @classmethod
    def setUpClass(cls):
        run = str(workflow_step("ci.yml", "evals-offline", "Routing benchmark")["run"])
        body = run.split("<<'PY'\n", 1)[1].rsplit("\nPY", 1)[0]
        cls.script = textwrap.dedent(body)
        # The parent commit: just what the router and eval_router read (skills, scripts/,
        # schema/, evals/routing/). Each test clones it, so its HEAD^1 is this commit.
        cls._template_dir = tempfile.mkdtemp(prefix="routing-gate-parent-")
        cls.parent = Path(cls._template_dir) / "parent"
        ignore = shutil.ignore_patterns("__pycache__")
        for d in tu_sl.iter_skill_dirs(REPO_ROOT):
            shutil.copytree(d, cls.parent / d.name, ignore=ignore)
        for rel in ("scripts", "schema", "evals/routing"):
            shutil.copytree(REPO_ROOT / rel, cls.parent / rel, ignore=ignore)
        _git(cls.parent, "init", "-q")
        _git(cls.parent, "add", "-A")
        _git(cls.parent, "commit", "-q", "-m", "parent")

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls._template_dir, ignore_errors=True)

    def setUp(self):
        self._tmp = tempfile.mkdtemp(prefix="routing-gate-")
        self.root = Path(self._tmp) / "repo"
        _git(Path(self._tmp), "clone", "-q", "--no-hardlinks", str(self.parent), str(self.root))

    def tearDown(self):
        shutil.rmtree(self._tmp, ignore_errors=True)

    def eval_router(self, *args: str) -> None:
        r = subprocess.run([sys.executable, "scripts/eval_router.py", *args], cwd=self.root, capture_output=True,
                           text=True, encoding="utf-8")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)

    def add_skill_with_cases(self) -> None:
        (self.root / "zz-quuxel-frobnication").mkdir()
        (self.root / "zz-quuxel-frobnication" / "SKILL.md").write_text(NEW_SKILL, encoding="utf-8", newline="\n")
        path = self.root / "evals" / "routing" / "benchmark.json"
        bench = json.loads(path.read_text(encoding="utf-8"))
        for i, split in enumerate(("dev", "test"), 1):
            bench["cases"].append({"id": f"zz-00{i}", "query": f"Frobnicate these quuxel zorbles, batch {i}.",
                                   "category": "typical", "split": split, "expected": ["zz-quuxel-frobnication"],
                                   "acceptable": [], "forbidden": [], "notes": ""})
        path.write_text(json.dumps(bench, indent=2) + "\n", encoding="utf-8", newline="\n")

    def rebaseline(self) -> None:
        self.eval_router("--write-baseline", "evals/routing/baseline.json")

    def gate(self) -> tuple[int, str, str]:
        _git(self.root, "add", "-A")
        _git(self.root, "commit", "-q", "--allow-empty", "-m", "change")
        script = Path(self._tmp) / "gate.py"
        script.write_text(self.script, encoding="utf-8", newline="\n")
        summary = Path(self._tmp) / "summary.md"
        env = {**os.environ, "RUNNER_TEMP": self._tmp, "GITHUB_STEP_SUMMARY": str(summary), "PYTHONUTF8": "1"}
        r = subprocess.run([sys.executable, str(script)], cwd=self.root, env=env, capture_output=True, text=True,
                           encoding="utf-8")
        return r.returncode, summary.read_text(encoding="utf-8") if summary.exists() else "", r.stdout + r.stderr

    def test_unchanged_tree_passes_all_three_checks(self):
        code, summary, log = self.gate()
        self.assertEqual(code, 0, log)
        self.assertRegex(summary, r"vs parent \w+'s benchmark \+ baseline: pass")

    def test_new_skill_does_not_stop_the_parent_check(self):
        # Before the fix the parent benchmark failed coverage for the new slug (exit 2) and the
        # no-regression check was skipped with a ::warning. Whether it passes is the router's
        # business: even a skill with no shared vocabulary shifts IDF weights for every skill.
        self.add_skill_with_cases()
        self.rebaseline()
        code, summary, log = self.gate()
        self.assertNotIn("NOT RUN", summary)
        self.assertNotIn("does not validate", log)
        self.assertRegex(summary, r"this commit's router vs parent \w+'s benchmark \+ baseline: ")
        self.assertEqual(code, 0 if summary.rstrip().endswith("baseline: pass") else 1, log)

    def test_new_skill_cannot_carry_a_lowered_baseline(self):
        # The audit scenario: add a skill, regress the router, re-baseline (lowering baseline.json)
        # in the same change. Checks 1 and 2 pass; check 3 must fail.
        self.add_skill_with_cases()
        with open(self.root / "scripts" / "router.py", "a", encoding="utf-8", newline="\n") as fh:
            fh.write(SABOTAGE)
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
        path = self.root / "evals" / "routing" / "benchmark.json"
        bench = json.loads(path.read_text(encoding="utf-8"))
        for case in bench["cases"]:
            labelled = case["expected"] + case["acceptable"]
            if "data-preprocessing" in labelled and "python-for-ml" not in labelled:
                case["forbidden"] = [s for s in case["forbidden"] if s != "python-for-ml"]
                case["acceptable"].append("python-for-ml")
        path.write_text(json.dumps(bench, indent=2) + "\n", encoding="utf-8", newline="\n")
        self.rebaseline()
        code, summary, log = self.gate()
        self.assertNotIn("NOT RUN", summary)
        self.assertNotIn("does not validate", log)
        self.assertIn("relabelled for new prerequisites", summary)
        self.assertIn(code, (0, 1), log)  # pass/fail is the router's business; the check ran

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
            if not (er.validate_benchmark(candidate, valid, rest) or er.coverage_errors(candidate, valid)):
                return gone
        self.skipTest("no skill can be removed without breaking the benchmark's coverage rules")

    def test_parent_cases_for_a_removed_skill_are_dropped_with_a_warning(self):
        bench_path = self.root / "evals" / "routing" / "benchmark.json"
        bench = json.loads(bench_path.read_text(encoding="utf-8"))
        gone = self.removable_skill(bench)
        bench_path.write_text(json.dumps(self.without_skill(bench, gone), indent=2) + "\n", encoding="utf-8",
                              newline="\n")
        shutil.rmtree(self.root / gone)
        self.rebaseline()
        code, summary, log = self.gate()
        self.assertNotIn("NOT RUN", summary)
        self.assertIn("expect a skill this commit no longer has", log)
        self.assertIn("dropped for removed skills", summary)
        self.assertIn(code, (0, 1), log)


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


def _posix_bash() -> str | None:
    bash = shutil.which("bash")
    if bash is None or "system32" in bash.lower():  # WSL's launcher, not a POSIX bash for this tree
        return None
    return bash


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


if __name__ == "__main__":
    unittest.main()
