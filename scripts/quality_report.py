#!/usr/bin/env python3
"""Machine-readable quality report: what has been checked, how, and what has not.

Combines the repo's own gates into one JSON document (schema
"ml-ai-skills/quality-report", version 1):

  * skill validation          scripts/validate_skills.py --json
  * schema / contract locks   scripts/generate_schema.py --check, scripts/check_contract.py --check
  * eval suite + coverage     scripts/validate_evals.py --coverage --json (offline; no model is run)
  * routing benchmark         scripts/eval_router.py --split all --json, compared with
                              evals/routing/baseline.json (test split)
  * freshness / claims        scripts/check_freshness.py --json, scripts/check_claims.py --json
  * unit / example tests      only when run (--run-tests, --run-examples) or ingested
                              (--test-results); pass/fail/error/skip counts with skip reasons
  * example-block coverage    ML_AI_SKILLS_EXAMPLE_COVERAGE jsonl (tests/examples/harness.py)
  * skipped checks            every check this report did not run, with the reason
  * known limitations         see docs/KNOWN-ISSUES.md for the full list

Per-skill status keeps four kinds of evidence apart and never collapses them into
one "verified" label: structural validity (metadata and sections), executed
example blocks, offline eval coverage, and recorded human review. Passing the
validator says nothing about whether a skill's advice is correct. A skill is only
"structural-valid" when validate_skills.py ran and returned parseable JSON.

Provenance: --collect-tests results record the git revision, dirty state and each
SKILL.md's sha256 at collection time; the report compares them with the current
tree and marks a skill's example evidence stale when they differ. Coverage jsonl
written by tests/examples/harness.py carries only total_blocks (no revision), so
ingested coverage is checked by block count only (and by a per-record
"skill_sha256" if a writer adds one); a coverage record whose total_blocks does
not match the current skill marks that skill "coverage-stale". Block coverage
alone does not show the example tests passed: the "example-tests-failing" status
needs example test results (--run-examples or --test-results).

Usage:
    python scripts/quality_report.py                         # static checks only (offline, ~10 s)
    python scripts/quality_report.py --run-tests             # + stdlib unit suite (CI's module list,
                                                             #   tests/test_install*.py excluded)
    python scripts/quality_report.py --run-tests --include-slow   # + installer tests (~9 min)
    python scripts/quality_report.py --run-examples PYTHON   # + tests/examples with PYTHON
                                                             #   (needs requirements-examples.txt)
    python scripts/quality_report.py --test-results R.json --example-coverage C.jsonl
    python scripts/quality_report.py --out quality-report.json --timestamp 2026-10-28T00:00:00Z

    # Write a results file another run can ingest (any interpreter; stdlib only):
    python scripts/quality_report.py --collect-tests R.json --suite unit|examples [--include-slow]

Determinism: for the same tree, environment and inputs the JSON is byte-identical
except "generated_at" (pin it with --timestamp). Test outcomes are only as
deterministic as the tests themselves.

Exit codes: 0 report produced and no hard failure; 1 report produced with at
least one hard failure (listed in summary.hard_failures; a tool that cannot be
started or times out is recorded as one); 2 usage error, or an input file
(--test-results / --example-coverage) could not be read or parsed.
"""
from __future__ import annotations

import argparse
import datetime as dt
import fnmatch
import hashlib
import json
import os
import platform
import shutil
import subprocess
import sys
import tempfile
import traceback
import unittest
from pathlib import Path
from typing import Any

SCRIPTS_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCRIPTS_DIR.parent
REPORT_SCHEMA = "ml-ai-skills/quality-report"
RESULTS_SCHEMA = "ml-ai-skills/test-results"
SCHEMA_VERSION = 1
# The only report field allowed to differ between two runs on identical inputs.
NONDETERMINISTIC_FIELDS = ("generated_at",)
SLOW_UNIT_GLOB = "test_install*.py"
SUITES = ("unit", "examples")
# Router metrics: higher is better / lower is better (same split as ci.yml's evals-offline gate).
ROUTER_MIN = ("precision", "recall", "top1_accuracy", "task_accuracy")
ROUTER_MAX = ("false_activation_rate", "mean_unnecessary_activations", "forbidden_selection_rate")
MESSAGE_LIMIT = 300

KNOWN_LIMITATIONS = (
    {"id": "no-model-graded-evals",
     "text": "No eval case has been graded against real model output; with-skill vs without-skill uplift "
             "is unmeasured. Offline evals check fixtures against regex hard assertions only."},
    {"id": "regex-hard-assertions",
     "text": "Hard assertions check that concepts are named and anticipated wrong conclusions are absent, "
             "not that reasoning is correct; every reviewer probe was tuned against and is now a fixture."},
    {"id": "examples-use-stubs",
     "text": "Executed example blocks run on tiny synthetic data, random-weight models and stubbed services; "
             "they show the code runs against the pinned versions, not that the advice is optimal."},
    {"id": "lexical-router",
     "text": "The router is lexical (no embeddings); its benchmark labels are single-labeller and the test "
             "split has 85 cases, so differences of a few points may be noise."},
    {"id": "runtime-loading-untested",
     "text": "Claude Code's runtime skill discovery/selection is not automatically tested; the plugin tests stop "
             "at plugin validate --strict and the marketplace add/install/list/update/reinstall return codes."},
    {"id": "platform-coverage",
     "text": "Locally executed on Windows 11 + Git Bash only; Linux/macOS appear only in CI configuration."},
    {"id": "no-human-review-records",
     "text": "No per-skill human-review record format exists, so skills_human_reviewed is 0 by construction; "
             "Phase 1/2 audits were agent reviews (docs/SKILL-AUDIT.md). Claim-level human-review entries in "
             "docs/claims.json are counted separately and never make a whole skill 'reviewed'."},
    {"id": "coverage-provenance",
     "text": "Coverage jsonl from tests/examples/harness.py records no git revision or content hash; ingested "
             "coverage is tied to the current tree only by each skill's python block count."},
    {"id": "example-attribution-by-module-name",
     "text": "Example test outcomes are attributed to a skill by module name (tests/examples/test_<slug>.py); "
             "a differently named module is not attributed to any skill."},
)


# --------------------------------------------------------------------------------------------
# Test collection (stdlib only; runs under any interpreter, e.g. the example venv)
# --------------------------------------------------------------------------------------------

def _first_line(text: str) -> str:
    lines = [ln for ln in text.strip().splitlines() if ln.strip()]
    line = lines[-1].strip() if lines else ""
    return line[:MESSAGE_LIMIT]


class _CollectingResult(unittest.TestResult):
    """Records one outcome per reported test (a failing subTest counts as one failure)."""

    def __init__(self) -> None:
        super().__init__()
        self.buffer = True
        self.records: list[dict[str, Any]] = []

    def _add(self, test, outcome: str, **extra) -> None:
        self.records.append({"id": test.id(), "outcome": outcome, **extra})

    def addSuccess(self, test):
        super().addSuccess(test)
        self._add(test, "passed")

    def addFailure(self, test, err):
        super().addFailure(test, err)
        self._add(test, "failed", message=_first_line("".join(traceback.format_exception(*err))))

    def addError(self, test, err):
        super().addError(test, err)
        self._add(test, "error", message=_first_line("".join(traceback.format_exception(*err))))

    def addSkip(self, test, reason):
        super().addSkip(test, reason)
        self._add(test, "skipped", reason=str(reason))

    def addExpectedFailure(self, test, err):
        super().addExpectedFailure(test, err)
        self._add(test, "expected_failure")

    def addUnexpectedSuccess(self, test):
        super().addUnexpectedSuccess(test)
        self._add(test, "unexpected_success")

    def addSubTest(self, test, subtest, err):
        super().addSubTest(test, subtest, err)
        if err is not None:
            outcome = "failed" if issubclass(err[0], test.failureException) else "error"
            self._add(subtest, outcome, message=_first_line("".join(traceback.format_exception(*err))))


def unit_modules(root: Path, include_slow: bool) -> list[str]:
    """CI's module list: every tests/test_*.py, minus tests/test_install*.py unless include_slow."""
    mods = []
    for p in sorted((root / "tests").glob("test_*.py")):
        if not include_slow and fnmatch.fnmatch(p.name, SLOW_UNIT_GLOB):
            continue
        mods.append(p.stem)
    return mods


def summarize_records(records: list[dict]) -> dict:
    counts = {k: 0 for k in ("passed", "failed", "error", "skipped", "expected_failure", "unexpected_success")}
    reasons: dict[str, int] = {}
    for r in records:
        counts[r["outcome"]] = counts.get(r["outcome"], 0) + 1
        if r["outcome"] == "skipped":
            reasons[r.get("reason", "")] = reasons.get(r.get("reason", ""), 0) + 1
    return {"total": len(records), **counts,
            "skip_reasons": [{"reason": k, "count": v} for k, v in sorted(reasons.items(), key=lambda kv: (-kv[1], kv[0]))]}


def collect_tests(root: Path, suite: str, include_slow: bool = False) -> dict:
    """Run one suite in this interpreter and return a results document."""
    tests_dir = root / "tests"
    if str(tests_dir) not in sys.path:
        sys.path.insert(0, str(tests_dir))
    loader = unittest.TestLoader()
    if suite == "unit":
        selection = unit_modules(root, include_slow)
        tests = loader.loadTestsFromNames(selection)
        command = "PYTHONPATH=tests python -m unittest " + " ".join(selection)
    elif suite == "examples":
        tests = loader.discover(str(tests_dir / "examples"), pattern="test_*.py", top_level_dir=str(tests_dir))
        selection = ["tests/examples/test_*.py"]
        command = 'python -m unittest discover -s tests/examples -t tests -p "test_*.py"'
    else:
        raise ValueError(f"unknown suite {suite!r}")
    provenance = _provenance(root)  # captured before the run: what the tests were run against
    result = _CollectingResult()
    old_cwd = os.getcwd()
    os.chdir(root)
    try:
        tests.run(result)
    finally:
        os.chdir(old_cwd)
    records = sorted(result.records, key=lambda r: (r["id"], r["outcome"]))
    return {"schema": RESULTS_SCHEMA, "schema_version": SCHEMA_VERSION, "suite": suite,
            "command": command, "include_slow": include_slow, "selection": selection,
            "python": platform.python_version(), "platform": platform.system(),
            "env": {k: os.environ.get(k) for k in ("ML_AI_SKILLS_REQUIRE_EXAMPLE_DEPS", "ML_AI_SKILLS_SLOW_EXAMPLES")},
            "provenance": provenance, "summary": summarize_records(records), "tests": records}


def validate_results(doc: Any) -> list[str]:
    problems = []
    if not isinstance(doc, dict) or doc.get("schema") != RESULTS_SCHEMA:
        return [f"not a {RESULTS_SCHEMA} document"]
    if doc.get("schema_version") != SCHEMA_VERSION:
        problems.append(f"unsupported schema_version {doc.get('schema_version')!r}")
    if doc.get("suite") not in SUITES:
        problems.append(f"unknown suite {doc.get('suite')!r}")
    prov = doc.get("provenance")
    if prov is not None and (not isinstance(prov, dict) or not isinstance(prov.get("skill_sha256", {}), dict)):
        problems.append("'provenance' must be an object with a 'skill_sha256' object")
    if not isinstance(doc.get("tests"), list):
        problems.append("'tests' must be a list")
    else:
        for i, t in enumerate(doc["tests"]):
            if not isinstance(t, dict) or not isinstance(t.get("id"), str) or t.get("outcome") not in (
                    "passed", "failed", "error", "skipped", "expected_failure", "unexpected_success"):
                problems.append(f"tests[{i}] is not an {{id, outcome}} record")
                break
    return problems


# --------------------------------------------------------------------------------------------
# Running the repo's tools
# --------------------------------------------------------------------------------------------

def run_tool(root: Path, args: list[str], timeout: int = 600, python: str | None = None,
             env: dict | None = None) -> dict:
    """Run `python scripts/<tool> ...` in root; return {command, exit_code, stdout, stderr}."""
    cmd = [python or sys.executable, *args]
    full_env = {**os.environ, "PYTHONUTF8": "1", **(env or {})}
    try:
        p = subprocess.run(cmd, cwd=root, capture_output=True, text=True, encoding="utf-8",
                           errors="replace", timeout=timeout, env=full_env)
        return {"command": "python " + " ".join(args), "exit_code": p.returncode,
                "stdout": p.stdout, "stderr": p.stderr}
    except (OSError, subprocess.TimeoutExpired) as e:
        return {"command": "python " + " ".join(args), "exit_code": None, "stdout": "", "stderr": str(e)}


def _json_or_none(text: str) -> Any:
    try:
        return json.loads(text)
    except (json.JSONDecodeError, TypeError):
        return None


def gather(root: Path, args: argparse.Namespace) -> dict:
    """Run every static check and the requested suites; return the raw inputs for build_report."""
    today = ["--today", args.today] if args.today else []
    raw: dict[str, Any] = {}
    raw["validate_skills"] = run_tool(root, ["scripts/validate_skills.py", "--json", *today])
    raw["generate_schema"] = run_tool(root, ["scripts/generate_schema.py", "--check"])
    raw["check_contract"] = run_tool(root, ["scripts/check_contract.py", "--check"])
    raw["validate_evals"] = run_tool(root, ["scripts/validate_evals.py", "--coverage", "--json"])
    raw["eval_router"] = run_tool(root, ["scripts/eval_router.py", "--split", "all", "--json"])
    raw["check_freshness"] = run_tool(root, ["scripts/check_freshness.py", "--json", *today])
    raw["check_claims"] = run_tool(root, ["scripts/check_claims.py", "--json", *today])
    if args.check_generated:
        raw["check_generated"] = run_tool(root, ["scripts/check_generated.py"], timeout=1800)

    test_results: list[dict] = []
    coverage_lines: list[str] = []
    inputs: list[dict] = []
    for path in args.test_results or []:
        test_results.append(json.loads(Path(path).read_text(encoding="utf-8")))
        inputs.append({"kind": "test-results", "source": str(path)})
    for path in args.example_coverage or []:
        lines = Path(path).read_text(encoding="utf-8").splitlines()
        coverage_lines += lines
        inputs.append({"kind": "example-coverage", "source": str(path), "records": sum(1 for x in lines if x.strip())})

    with tempfile.TemporaryDirectory(prefix="mlai-quality-") as tmp:
        if args.run_tests:
            out = Path(tmp) / "unit.json"
            flags = ["--include-slow"] if args.include_slow else []
            res = run_tool(root, ["scripts/quality_report.py", "--collect-tests", str(out), "--suite", "unit",
                                  *flags], timeout=args.timeout)
            raw["run_unit"] = {k: v for k, v in res.items() if k != "stdout"}
            if out.exists():
                test_results.append(json.loads(out.read_text(encoding="utf-8")))
                inputs.append({"kind": "test-results", "source": "this-run (--run-tests)"})
        if args.run_examples:
            out = Path(tmp) / "examples.json"
            cov = Path(tmp) / "coverage.jsonl"
            env = {"ML_AI_SKILLS_EXAMPLE_COVERAGE": str(cov)}
            if args.slow_examples:
                env["ML_AI_SKILLS_SLOW_EXAMPLES"] = "1"
            res = run_tool(root, ["scripts/quality_report.py", "--collect-tests", str(out), "--suite", "examples"],
                           timeout=args.timeout, python=args.run_examples, env=env)
            raw["run_examples"] = {k: v for k, v in res.items() if k != "stdout"}
            if out.exists():
                test_results.append(json.loads(out.read_text(encoding="utf-8")))
                inputs.append({"kind": "test-results", "source": "this-run (--run-examples)"})
            if cov.exists():
                lines = cov.read_text(encoding="utf-8").splitlines()
                coverage_lines += lines
                inputs.append({"kind": "example-coverage", "source": "this-run (--run-examples)",
                               "records": sum(1 for x in lines if x.strip())})

    raw["test_results"] = test_results
    raw["example_coverage"] = coverage_lines
    raw["inputs"] = inputs
    raw["claude_cli"] = shutil.which("claude") is not None
    raw["baseline"] = _json_or_none(_read(root / "evals" / "routing" / "baseline.json"))
    raw["benchmark_sha256"] = _benchmark_sha(root / "evals" / "routing" / "benchmark.json")
    raw["skills"] = _skill_inventory(root)
    raw["environment"] = {"python": platform.python_version(), "platform": platform.system(),
                          "platform_release": platform.release(), **_git_state(root)}
    raw["options"] = {"run_tests": bool(args.run_tests), "include_slow": bool(args.include_slow),
                      "run_examples": bool(args.run_examples), "slow_examples": bool(args.slow_examples),
                      "check_generated": bool(args.check_generated), "today": args.today}
    return raw


def _read(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8")
    except OSError:
        return None


def _benchmark_sha(path: Path) -> str | None:
    try:
        return hashlib.sha256(path.read_bytes().replace(b"\r\n", b"\n")).hexdigest()
    except OSError:
        return None


def _git_state(root: Path) -> dict:
    try:
        rev = subprocess.run(["git", "rev-parse", "HEAD"], cwd=root, capture_output=True, text=True, timeout=30)
        dirty = subprocess.run(["git", "status", "--porcelain"], cwd=root, capture_output=True, text=True,
                               timeout=120)
    except (OSError, subprocess.TimeoutExpired):
        return {"git_revision": None, "git_dirty": None}
    if rev.returncode != 0:
        return {"git_revision": None, "git_dirty": None}
    return {"git_revision": rev.stdout.strip(), "git_dirty": bool(dirty.stdout.strip()) if dirty.returncode == 0 else None}


def _skill_sha256(root: Path) -> dict[str, str]:
    """sha256 of each skill's SKILL.md (CRLF normalised), keyed by folder name."""
    if str(SCRIPTS_DIR) not in sys.path:
        sys.path.insert(0, str(SCRIPTS_DIR))
    import skills_lib as sl  # stdlib-only

    out = {}
    for d in sl.iter_skill_dirs(root):
        try:
            out[d.name] = hashlib.sha256((d / "SKILL.md").read_bytes().replace(b"\r\n", b"\n")).hexdigest()
        except OSError:
            continue
    return out


def _provenance(root: Path) -> dict:
    return {**_git_state(root), "skill_sha256": _skill_sha256(root)}


def _skill_inventory(root: Path) -> list[dict]:
    if str(SCRIPTS_DIR) not in sys.path:
        sys.path.insert(0, str(SCRIPTS_DIR))
    import skills_lib as sl  # only the report mode needs the parser

    shas = _skill_sha256(root)
    inv = []
    for skill in sl.load_all_skills(root):
        blocks = [code for _, code in skill.code_blocks(lang="python")]
        illustrative = [i for i, b in enumerate(blocks) if _is_illustrative(b)]
        fm = skill.frontmatter
        inv.append({"skill": skill.slug, "type": fm.get("type"), "domain": fm.get("domain"),
                    "lifecycle": fm.get("lifecycle"), "risk_level": fm.get("risk_level"),
                    "evidence_level": fm.get("evidence_level"), "last_review_date": fm.get("last_verified"),
                    "python_blocks": len(blocks), "illustrative_blocks": len(illustrative),
                    "illustrative_indices": illustrative, "sha256": shas.get(skill.slug)})
    return inv


def _is_illustrative(code: str) -> bool:
    """Same rule as tests/examples/harness.py: first non-blank line starts with '# illustrative'."""
    for line in code.splitlines():
        if line.strip():
            return line.strip().lower().startswith("# illustrative")
    return False


# --------------------------------------------------------------------------------------------
# Report assembly (pure: everything comes from `raw`, so tests can inject data)
# --------------------------------------------------------------------------------------------

def _tool_status(res: dict | None) -> str:
    if res is None:
        return "not-run"
    if res.get("exit_code") is None:
        return "error"
    return "pass" if res["exit_code"] == 0 else "fail"


def _example_module_for(slug: str) -> str:
    return "test_" + slug.replace("-", "_")


def _parse_coverage(lines: list[str], inventory: dict[str, dict]
                    ) -> tuple[dict[str, set[int]], dict[str, list[str]], int]:
    """Return (executed block indices, stale reasons, malformed-line count) per skill.

    A record is only trusted when it can be tied to the current tree: its total_blocks must
    equal the skill's current python block count (and its skill_sha256, if present, must match).
    """
    executed: dict[str, set[int]] = {}
    stale: dict[str, list[str]] = {}
    bad = 0
    for line in lines:
        if not line.strip():
            continue
        try:
            rec = json.loads(line)
            slug = rec["skill"]
            blocks = {int(i) for i in rec["blocks"]}
        except (json.JSONDecodeError, KeyError, TypeError, ValueError):
            bad += 1
            continue
        inv = inventory.get(slug)
        reason = None
        if inv is None:
            pass  # not a skill in the current tree: nothing to attribute it to
        elif not isinstance(rec.get("total_blocks"), int):
            reason = "coverage record has no total_blocks to compare with the current skill"
        elif rec["total_blocks"] != inv["python_blocks"]:
            reason = (f"coverage record has total_blocks {rec['total_blocks']}, "
                      f"current SKILL.md has {inv['python_blocks']} python block(s)")
        elif rec.get("skill_sha256") is not None and inv.get("sha256") and rec["skill_sha256"] != inv["sha256"]:
            reason = "coverage record skill_sha256 differs from the current SKILL.md"
        if reason:
            if reason not in stale.setdefault(slug, []):
                stale[slug].append(reason)
            continue
        executed.setdefault(slug, set()).update(blocks)
    return executed, stale, bad


def _results_provenance(doc: dict, environment: dict, inventory: dict[str, dict]) -> dict:
    """Compare a results document's recorded provenance with the current tree."""
    prov = doc.get("provenance")
    if not isinstance(prov, dict):
        return {"recorded": False, "git_revision": None, "git_dirty": None, "matches_current_revision": None,
                "stale_skills": None, "note": "results file records no provenance; it cannot be tied to this tree"}
    rev, cur = prov.get("git_revision"), environment.get("git_revision")
    shas = prov.get("skill_sha256") or {}
    stale = sorted(slug for slug, inv in inventory.items()
                   if inv.get("sha256") and shas.get(slug) != inv.get("sha256"))
    return {"recorded": True, "git_revision": rev, "git_dirty": prov.get("git_dirty"),
            "matches_current_revision": (rev == cur) if rev and cur else None, "stale_skills": stale,
            "note": "Only SKILL.md contents are compared per skill; with a dirty tree on either side a matching "
                    "revision does not prove the scripts or tests were identical."}


def _router_section(raw: dict) -> dict:
    res = raw.get("eval_router")
    doc = _json_or_none(res["stdout"]) if res else None
    section: dict[str, Any] = {"status": _tool_status(res), "command": res["command"] if res else None}
    if not isinstance(doc, dict) or "splits" not in doc:
        section["status"] = "error" if res else "not-run"
        return section
    section["top_k"] = doc.get("top_k")
    section["splits"] = {name: {k: v for k, v in m.items() if k != "counts"}
                         for name, m in sorted(doc["splits"].items())}
    section["misrouted"] = {s: sum(1 for c in doc.get("misrouted", []) if c.get("split") == s) for s in ("dev", "test")}
    baseline = raw.get("baseline")
    if isinstance(baseline, dict) and isinstance(baseline.get("metrics"), dict):
        floor = baseline["metrics"].get("test", {})
        current = doc["splits"].get("test", {})
        regressions = []
        for k in ROUTER_MIN:
            if floor.get(k) is not None and current.get(k) is not None and current[k] < floor[k]:
                regressions.append(f"test {k} {current[k]} < baseline {floor[k]}")
        for k in ROUTER_MAX:
            if floor.get(k) is not None and current.get(k) is not None and current[k] > floor[k]:
                regressions.append(f"test {k} {current[k]} > baseline {floor[k]}")
        sha_ok = baseline.get("benchmark_sha256") == raw.get("benchmark_sha256")
        section["baseline"] = {"date": baseline.get("date"), "benchmark_sha256_matches": sha_ok,
                               "test_floor": {k: floor.get(k) for k in (*ROUTER_MIN, *ROUTER_MAX)},
                               "regressions": regressions}
        if regressions or not sha_ok:
            section["status"] = "fail"
    else:
        section["baseline"] = None
    section["note"] = ("Benchmark labels are single-labeller; weights were tuned on dev only. A floor match is "
                       "a regression check, not evidence of routing quality outside the benchmark.")
    return section


def _tests_section(raw: dict, inventory: dict[str, dict]) -> dict:
    suites: dict[str, Any] = {}
    for doc in raw.get("test_results", []):
        problems = validate_results(doc)
        if problems:
            suites.setdefault("invalid", []).append(problems)
            continue
        summary = summarize_records(doc["tests"])
        suites[doc["suite"]] = {"command": doc.get("command"), "python": doc.get("python"),
                                "platform": doc.get("platform"), "include_slow": doc.get("include_slow"),
                                "env": doc.get("env"), **summary,
                                "provenance": _results_provenance(doc, raw.get("environment", {}), inventory),
                                "failures": [t for t in doc["tests"] if t["outcome"] in ("failed", "error",
                                                                                          "unexpected_success")]}
    for name in SUITES:
        suites.setdefault(name, None)
    return suites


def _inputs_section(raw: dict, tests: dict, cov_stale: dict[str, list[str]]) -> list[dict]:
    """Where each piece of ingested or collected evidence came from, and whether it matches this tree."""
    out = []
    for item in raw.get("inputs", []):
        entry = dict(item)
        if item.get("kind") == "example-coverage":
            entry["provenance"] = ("this run" if str(item.get("source", "")).startswith("this-run") else
                                   "not recorded by tests/examples/harness.py (checked by block count only)")
        out.append(entry)
    for name in SUITES:
        s = tests.get(name)
        if s:
            out.append({"kind": "test-results-provenance", "suite": name, **s["provenance"]})
    if cov_stale:
        out.append({"kind": "example-coverage-stale", "skills": sorted(cov_stale)})
    return out


def build_report(raw: dict, timestamp: str) -> dict:
    hard: list[str] = []
    skipped: list[dict] = []

    # --- validators / locks ---------------------------------------------------------------
    vs = raw.get("validate_skills")
    vs_doc = _json_or_none(vs["stdout"]) if vs else None
    # Structural validity is only claimed when the validator ran and returned the expected JSON.
    structural_measured = (isinstance(vs_doc, dict) and isinstance(vs_doc.get("errors"), list)
                           and isinstance(vs_doc.get("warnings"), list))
    if not structural_measured:
        vs_doc = {"errors": [], "warnings": []}
        if vs:
            hard.append("validate_skills.py did not produce JSON")
    errors_by_skill: dict[str, int] = {}
    warnings_by_skill: dict[str, int] = {}
    for e in vs_doc.get("errors", []):
        errors_by_skill[e.get("skill")] = errors_by_skill.get(e.get("skill"), 0) + 1
    for w in vs_doc.get("warnings", []):
        warnings_by_skill[w.get("skill")] = warnings_by_skill.get(w.get("skill"), 0) + 1
    if vs_doc.get("errors"):
        hard.append(f"validate_skills: {len(vs_doc['errors'])} error(s)")
    checks: dict[str, Any] = {
        "validate_skills": {"status": _tool_status(vs) if structural_measured or not vs else "error",
                            "command": vs["command"] if vs else None, "measured": structural_measured,
                            "errors": len(vs_doc.get("errors", [])), "warnings": len(vs_doc.get("warnings", [])),
                            "error_codes": sorted({e.get("code") for e in vs_doc.get("errors", [])} - {None})},
    }
    for name in ("generate_schema", "check_contract", "check_generated"):
        res = raw.get(name)
        checks[name] = {"status": _tool_status(res), "command": res["command"] if res else None,
                        "output_tail": (res["stdout"] + res["stderr"]).strip().splitlines()[-3:] if res else []}
        if res and res.get("exit_code") != 0:
            hard.append(f"{name}: exit {res.get('exit_code')}")

    # --- evals ----------------------------------------------------------------------------
    ve = raw.get("validate_evals")
    ve_doc = _json_or_none(ve["stdout"]) if ve else None
    cov_rows: dict[str, dict] = {}
    if isinstance(ve_doc, dict):
        cov = ve_doc.get("coverage") or {}
        cov_rows = {r["skill"]: r for r in cov.get("skills", [])}
        checks["validate_evals"] = {
            "status": "pass" if ve_doc.get("ok") else "fail", "command": ve["command"],
            "errors": len(ve_doc.get("errors", [])), "warnings": len(ve_doc.get("warnings", [])),
            "fixture_self_tests": ve_doc.get("fixture_self_tests"), "coverage_summary": cov.get("summary"),
            "model_graded": False,
            "note": "Offline only: fixtures are checked against regex hard assertions. No model output was graded; "
                    "baseline and uplift are unmeasured."}
        if ve_doc.get("errors"):
            hard.append(f"validate_evals: {len(ve_doc['errors'])} error(s)")
    else:
        checks["validate_evals"] = {"status": _tool_status(ve) if ve else "not-run",
                                    "command": ve["command"] if ve else None}
        if ve:
            hard.append("validate_evals.py did not produce JSON")

    # --- router ---------------------------------------------------------------------------
    checks["eval_router"] = _router_section(raw)
    if checks["eval_router"]["status"] in ("fail", "error"):
        hard.append("eval_router: " + "; ".join((checks["eval_router"].get("baseline") or {}).get("regressions")
                                               or ["benchmark/baseline problem (see checks.eval_router)"]))

    # --- freshness / claims -----------------------------------------------------------------
    cf = raw.get("check_freshness")
    cf_doc = _json_or_none(cf["stdout"]) if cf else None
    fresh_by_skill: dict[str, dict[str, int]] = {}
    if isinstance(cf_doc, dict):
        for f in cf_doc.get("findings", []):
            d = fresh_by_skill.setdefault(f.get("skill"), {})
            d[f.get("level")] = d.get(f.get("level"), 0) + 1
        checks["check_freshness"] = {"status": _tool_status(cf), "command": cf["command"],
                                     "kinds_run": cf_doc.get("kinds_run"), "counts": cf_doc.get("counts"),
                                     "note": cf_doc.get("note")}
        if (cf_doc.get("counts") or {}).get("STALE"):
            hard.append(f"check_freshness: {cf_doc['counts']['STALE']} STALE finding(s)")
    else:
        checks["check_freshness"] = {"status": _tool_status(cf), "command": cf["command"] if cf else None}
    cc = raw.get("check_claims")
    cc_doc = _json_or_none(cc["stdout"]) if cc else None
    claims_by_skill: dict[str, dict[str, int]] = {}
    if isinstance(cc_doc, dict):
        checks["check_claims"] = {"status": _tool_status(cc), "command": cc["command"],
                                  "summary": cc_doc.get("summary"), "errors": len(cc_doc.get("errors", [])),
                                  "review_due": [f"{f.get('claim')}: {f.get('code')}" for f in cc_doc.get("findings", [])],
                                  "note": "A claim's verification records how it was checked on last_reviewed; "
                                          "this report does not re-run it."}
        if cc_doc.get("errors"):
            hard.append(f"check_claims: {len(cc_doc['errors'])} error(s)")
    else:
        checks["check_claims"] = {"status": _tool_status(cc), "command": cc["command"] if cc else None}
    human_claims: dict[str, int] = {}
    for c in raw.get("claims", []):
        d = claims_by_skill.setdefault(c.get("skill"), {})
        d[c.get("verification")] = d.get(c.get("verification"), 0) + 1
        if c.get("verification") == "human-review" and c.get("status") == "current":
            human_claims[c.get("skill")] = human_claims.get(c.get("skill"), 0) + 1

    # --- tests ----------------------------------------------------------------------------
    inventory = {inv["skill"]: inv for inv in raw.get("skills", [])}
    tests = _tests_section(raw, inventory)
    for name in SUITES:
        s = tests.get(name)
        if s and (s["failed"] or s["error"] or s["unexpected_success"]):
            hard.append(f"{name} tests: {s['failed']} failed, {s['error']} error(s), "
                        f"{s['unexpected_success']} unexpected success(es)")
    if tests.get("invalid"):
        hard.append("an ingested test-results file is invalid")
    for key, label in (("run_unit", "unit"), ("run_examples", "examples")):
        res = raw.get(key)
        if res and tests.get(label) is None:
            hard.append(f"{label} suite did not produce results (exit {res.get('exit_code')}): "
                        f"{_first_line(res.get('stderr', ''))}")

    executed, cov_stale, bad_cov = _parse_coverage(raw.get("example_coverage", []), inventory)
    coverage_measured = bool(raw.get("example_coverage"))
    example_records: dict[str, list[dict]] = {}
    ex_doc = next((d for d in raw.get("test_results", []) if d.get("suite") == "examples" and not validate_results(d)),
                  None)
    ex_stale: set[str] = set()
    if ex_doc:
        ex_stale = set(_results_provenance(ex_doc, raw.get("environment", {}), inventory)["stale_skills"] or [])
        for t in ex_doc["tests"]:
            parts = t["id"].replace("(", " ").replace(")", " ").split()
            mod = next((p.split(".")[1] if p.startswith("examples.") else None for p in parts if "test_" in p), None)
            if mod:
                example_records.setdefault(mod, []).append(t)

    # --- skipped checks -------------------------------------------------------------------
    opts = raw.get("options", {})
    if tests.get("unit") is None:
        skipped.append({"check": "unit-tests", "reason": "not run (pass --run-tests or --test-results)"})
    elif not tests["unit"].get("include_slow"):
        skipped.append({"check": "installer-tests",
                        "reason": "tests/test_install*.py excluded from the unit run (~9 min); "
                                  "pass --include-slow (CI runs them in the installer job on ubuntu/macOS)"})
    if tests.get("examples") is None:
        skipped.append({"check": "example-tests",
                        "reason": "not run (pass --run-examples PYTHON with requirements-examples.txt installed)"})
    if coverage_measured and tests.get("examples") is None:
        skipped.append({"check": "example-test-outcomes",
                        "reason": "block coverage was ingested without example test results: it shows which blocks "
                                  "executed without raising, not whether the tests' assertions passed"})
    if not coverage_measured:
        skipped.append({"check": "example-block-coverage",
                        "reason": "no ML_AI_SKILLS_EXAMPLE_COVERAGE data (comes with --run-examples or "
                                  "--example-coverage)"})
    if not raw.get("claude_cli"):
        skipped.append({"check": "claude-cli",
                        "reason": "claude CLI not on PATH: plugin validate --strict and the local marketplace "
                                  "add/install/update tests in tests/test_generate_plugins.py skip"})
    elif tests.get("unit") is None:
        skipped.append({"check": "claude-cli", "reason": "claude CLI present, but tests/test_generate_plugins.py "
                                                         "was not run by this report"})
    if raw.get("check_generated") is None:
        skipped.append({"check": "generated-artifacts",
                        "reason": "scripts/check_generated.py not run (~40-55 s; pass --check-generated)"})
    skipped.append({"check": "network", "reason": "offline by design: scripts/verify_online.py, "
                                                  "validate_skills.py --check-links and run_evals.py model mode "
                                                  "were not run"})
    skipped.append({"check": "model-evals", "reason": "no model was called; eval pass rates and with/without-skill "
                                                      "uplift are unmeasured"})
    skipped.append({"check": "executable-evals",
                    "reason": "run_evals.py --run-executable not invoked; its test ids are a subset of tests/examples"})
    skipped.append({"check": "github-actions", "reason": "CI/release workflows are not executed by this tool; "
                                                         "their results are unknown here"})
    skipped.append({"check": "other-platforms",
                    "reason": f"only this platform ({raw.get('environment', {}).get('platform')}) was exercised"})

    # --- per-skill status -----------------------------------------------------------------
    per_skill = []
    for inv in raw.get("skills", []):
        slug = inv["skill"]
        n_err = errors_by_skill.get(slug, 0)
        runnable = inv["python_blocks"] - inv["illustrative_blocks"]
        skip = set(inv.get("illustrative_indices", []))
        # Count only runnable block indices: a test may also exec an illustrative block explicitly.
        done = len({i for i in executed.get(slug, set()) if 0 <= i < inv["python_blocks"] and i not in skip})
        recs = example_records.get(_example_module_for(slug), [])
        ex_counts = summarize_records(recs) if recs else None
        stale_reasons = list(cov_stale.get(slug, []))
        results_stale = bool(recs) and slug in ex_stale
        if results_stale:
            stale_reasons.append("example test results were recorded against a different SKILL.md (sha256 differs)")
        if runnable == 0:
            ex_status = "no-runnable-blocks"
        elif cov_stale.get(slug):
            ex_status = "coverage-stale"
        elif results_stale:
            ex_status = "example-results-stale"
        elif not coverage_measured:
            ex_status = "not-measured"
        elif done == 0:
            ex_status = "not-executed"
        elif done >= runnable:
            ex_status = "all-runnable-blocks-executed"
        else:
            ex_status = "partially-executed"
        if (ex_counts and (ex_counts["failed"] or ex_counts["error"]) and not results_stale
                and ex_status != "no-runnable-blocks"):
            ex_status = "example-tests-failing"
        row = cov_rows.get(slug)
        if row is None:
            ev_status = "not-measured" if not cov_rows else "no-cases"
        elif row.get("missing"):
            ev_status = "partial"
        else:
            ev_status = "covered-offline"
        tiers = []
        if structural_measured and n_err == 0:
            tiers.append("structural-valid")
        if ex_status == "all-runnable-blocks-executed":
            tiers.append("examples-executed")
        if ev_status == "covered-offline":
            tiers.append("evals-covered-offline")
        human = human_claims.get(slug, 0)
        per_skill.append({
            **{k: inv.get(k) for k in ("skill", "type", "domain", "lifecycle", "risk_level", "evidence_level",
                                         "last_review_date")},
            "structural": ({"status": "valid" if n_err == 0 else "invalid", "errors": n_err,
                            "warnings": warnings_by_skill.get(slug, 0)} if structural_measured else
                           {"status": "not-measured", "errors": None, "warnings": None}),
            "examples": {"status": ex_status, "python_blocks": inv["python_blocks"],
                         "illustrative_blocks": inv["illustrative_blocks"], "runnable_blocks": runnable,
                         "executed_blocks": done if coverage_measured else None, "example_tests": ex_counts,
                         **({"stale_reasons": stale_reasons} if stale_reasons else {})},
            "evals": {"status": ev_status, "model_graded": False,
                      **({k: row.get(k) for k in ("cases", "non_happy_path_cases", "hard_assertion_cases",
                                                  "executable_cases", "has_plan", "missing")} if row else {})},
            "claims": claims_by_skill.get(slug, {}),
            "freshness": fresh_by_skill.get(slug, {}),
            # No per-skill review record format exists: current claim-level reviews are reported, never promoted.
            "human_review": {"status": "claim-level-review-only" if human else "not-recorded",
                             "current_human_reviewed_claims": human},
            "evidence_tiers": tiers,
        })

    def count(pred) -> int:
        return sum(1 for s in per_skill if pred(s))

    summary = {
        "hard_failures": hard,
        "skills_total": len(per_skill),
        "skills_structural_valid": (count(lambda s: s["structural"]["status"] == "valid")
                                    if structural_measured else None),
        "skills_examples_all_runnable_executed": (count(lambda s: s["examples"]["status"] ==
                                                        "all-runnable-blocks-executed") if coverage_measured else None),
        "skills_with_runnable_blocks": count(lambda s: s["examples"]["runnable_blocks"] > 0),
        "skills_evals_covered_offline": count(lambda s: s["evals"]["status"] == "covered-offline"),
        # 0 by construction until a per-skill human-review record format exists (see known_limitations).
        "skills_human_reviewed": 0,
        "skills_with_claim_level_human_review": count(lambda s: s["human_review"]["status"] ==
                                                      "claim-level-review-only"),
        "skills_with_stale_example_evidence": sorted(s["skill"] for s in per_skill if s["examples"]["status"]
                                                     in ("coverage-stale", "example-results-stale")),
        "model_graded_evals": "unmeasured",
        "statement": ("No skill is labelled verified. Structural validity, executed examples and offline eval "
                      "coverage are separate, partial kinds of evidence; none of them shows the advice is correct."),
    }
    if bad_cov:
        summary["coverage_lines_ignored"] = bad_cov

    return {
        "schema": REPORT_SCHEMA, "schema_version": SCHEMA_VERSION, "generated_at": timestamp,
        "environment": raw.get("environment", {}), "options": opts, "summary": summary,
        "inputs": _inputs_section(raw, tests, cov_stale),
        "checks": checks, "tests": tests, "skills": per_skill, "skipped_checks": skipped,
        "known_limitations": {"items": list(KNOWN_LIMITATIONS), "full_list": "docs/KNOWN-ISSUES.md"},
    }


def _load_claims(root: Path) -> list[dict]:
    doc = _json_or_none(_read(root / "docs" / "claims.json"))
    return [c for c in (doc or {}).get("claims", []) if isinstance(c, dict)]


def _now() -> str:
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0],
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--root", type=Path, default=REPO_ROOT, help="repository root (default: this repo)")
    ap.add_argument("--out", type=Path, help="write the JSON report here (default: stdout)")
    ap.add_argument("--run-tests", action="store_true", help="run the stdlib unit suite (CI's module list)")
    ap.add_argument("--include-slow", action="store_true", help="include tests/test_install*.py (~9 min)")
    ap.add_argument("--run-examples", metavar="PYTHON", help="run tests/examples with this interpreter")
    ap.add_argument("--slow-examples", action="store_true", help="set ML_AI_SKILLS_SLOW_EXAMPLES=1 for examples")
    ap.add_argument("--test-results", nargs="+", metavar="FILE", help="ingest --collect-tests results files")
    ap.add_argument("--example-coverage", nargs="+", metavar="FILE", help="ingest coverage jsonl files")
    ap.add_argument("--check-generated", action="store_true", help="also run scripts/check_generated.py")
    ap.add_argument("--today", help="pin today's date (YYYY-MM-DD) for the date-dependent checks")
    ap.add_argument("--timestamp", help="pin generated_at (for byte-identical reports)")
    ap.add_argument("--timeout", type=int, default=3600, help="seconds allowed per test suite (default 3600)")
    ap.add_argument("--collect-tests", type=Path, metavar="OUT", help="run one suite and write its results JSON")
    ap.add_argument("--suite", choices=SUITES, help="suite for --collect-tests")
    args = ap.parse_args(argv)
    root = args.root.resolve()

    if args.collect_tests:
        if not args.suite:
            ap.error("--collect-tests needs --suite")
        doc = collect_tests(root, args.suite, args.include_slow)
        args.collect_tests.write_text(json.dumps(doc, indent=2, sort_keys=True) + "\n", encoding="utf-8",
                                      newline="\n")
        s = doc["summary"]
        print(f"{args.suite}: {s['total']} outcome(s): {s['passed']} passed, {s['failed']} failed, "
              f"{s['error']} error(s), {s['skipped']} skipped")
        return 0 if not (s["failed"] or s["error"] or s["unexpected_success"]) else 1
    if args.suite:
        ap.error("--suite is only valid with --collect-tests")
    if args.today:
        try:
            dt.date.fromisoformat(args.today)
        except ValueError:
            ap.error(f"--today {args.today!r} is not YYYY-MM-DD")
    if args.timestamp is not None:
        try:
            dt.datetime.fromisoformat(args.timestamp.replace("Z", "+00:00"))
        except ValueError:
            ap.error(f"--timestamp {args.timestamp!r} is not an ISO-8601 date-time (e.g. 2026-10-28T00:00:00Z)")
    for paths in (args.test_results or [], args.example_coverage or []):
        for p in paths:
            if not Path(p).is_file():
                ap.error(f"{p} does not exist")

    try:
        raw = gather(root, args)
    except (OSError, json.JSONDecodeError) as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 2
    raw["claims"] = _load_claims(root)
    report = build_report(raw, args.timestamp or _now())
    text = json.dumps(report, indent=2, sort_keys=True) + "\n"
    if args.out:
        args.out.write_text(text, encoding="utf-8", newline="\n")
        s = report["summary"]
        print(f"wrote {args.out}: {s['skills_total']} skills, "
              f"{s['skills_structural_valid'] if s['skills_structural_valid'] is not None else 'unknown'} "
              "structurally valid, "
              f"{s['skills_evals_covered_offline']} with offline eval coverage, "
              f"{len(s['hard_failures'])} hard failure(s), {len(report['skipped_checks'])} skipped check(s)")
    else:
        sys.stdout.write(text)
    return 1 if report["summary"]["hard_failures"] else 0


if __name__ == "__main__":
    sys.exit(main())
