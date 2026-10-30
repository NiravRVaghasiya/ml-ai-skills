#!/usr/bin/env python3
"""Machine-readable quality report: what has been checked, how, and what has not.

Combines the repo's own gates into one JSON document (schema
"ml-ai-skills/quality-report", version 2):

  * skill validation          scripts/validate_skills.py --json
  * schema / contract locks   scripts/generate_schema.py --check, scripts/check_contract.py --check
  * eval suite + coverage     scripts/validate_evals.py --coverage --json (offline; no model is run)
  * routing benchmark         scripts/eval_router.py --split all --json, compared with
                              evals/routing/baseline.json (test split)
  * freshness / claims        scripts/check_freshness.py --json, scripts/check_claims.py --json
  * unit / example tests      only when run (--run-tests, --run-examples) or ingested
                              (--test-results); pass/fail/error/skip counts with skip reasons
  * example-block coverage    ML_AI_SKILLS_EXAMPLE_COVERAGE jsonl (tests/examples/harness.py)
  * per-skill human review    docs/reviews.json (schema "ml-ai-skills/skill-reviews", version 1)
  * skipped checks            every check this report did not run, with the reason
  * known limitations         see docs/KNOWN-ISSUES.md for the full list

Per-skill status keeps five kinds of evidence apart and never collapses them into
one "verified" label: structural validity (metadata and sections), executed
example blocks, passing example tests, offline eval coverage, and recorded human
review (evidence_tiers: structural-valid, examples-executed, example-tests-passed,
evals-covered-offline, human-reviewed). Passing the
validator says nothing about whether a skill's advice is correct. A skill is only
"structural-valid" when validate_skills.py ran and returned parseable JSON.

Provenance: --collect-tests results record the git revision, dirty state and each
SKILL.md's sha256 at collection time; the report compares them with the current
tree and marks a skill's example evidence stale when they differ. Coverage records
written by tests/examples/harness.py carry the SKILL.md's skill_sha256, total_blocks,
git revision, tracked-file dirty state and untracked-file count, python, platform, a
timestamp and the calling test. Only records whose skill_sha256 and total_blocks equal
the current skill's count. A record with differing values is stale and one without
them cannot be tied to this tree; both count for nothing and are listed (stale_reasons /
unattributable_reasons). Current records decide first: a skill they fully cover stays
"all-runnable-blocks-executed" despite leftover stale records (the harness appends, so a
reused coverage file keeps them); otherwise a stale record makes it "coverage-stale", and
"coverage-unattributable" means no usable record exists. Records without a git revision
(e.g. a release archive) are accepted and counted; --require-coverage-revision treats them
as unattributable. scripts/check_example_coverage.py is stricter on purpose: as a gate on
one file it fails on any stale or unattributable record.

Block coverage alone does not show the example tests passed. "executed-and-passed"
needs every runnable block covered by current records AND example test results
(--run-examples or --test-results) in which the tests attributed to the skill passed,
from documents that recorded the current SKILL.md sha256. Several results documents
of one suite are merged (their totals and hard failures cover every document);
per skill, a failure in a current or untied document gives "example-tests-failing",
while a failure recorded only against another SKILL.md sha256 gives
"example-results-stale" (still a hard failure for the suite).
"coverage-only-outcome-unknown" means blocks were covered but no usable outcome exists.

Attribution: an example test module is attributed to the skills in its module-level
SKILL / SKILLS declaration when it has one (SKILLS = () declares "no skill", status
"declared-no-skill"); otherwise to the union of a static scan (the slug passed to
run_blocks / python_blocks / load_skill / block_index, or a module-level SLUG). The
skills the harness recorded the module running (each coverage line's test_module) are
added, except under SKILLS = (), where they are only listed (excluded_harness_records).
The module name (test_<slug>.py) is only a fallback. A module
none of these attribute is reported as "unattributed-test-module", never dropped, and
so is an unparseable test id.

Human review: a skill counts as "reviewed" only with a docs/reviews.json record
(skill, reviewer, date, skill_sha256, scope "full", outcome "approved") whose
skill_sha256 is the current SKILL.md's; records for another sha256 make it
"review-stale". Claim-level human-review entries in docs/claims.json are reported
separately and never make a skill reviewed.

Usage:
    python scripts/quality_report.py                         # static checks only (offline, ~10 s)
    python scripts/quality_report.py --run-tests             # + stdlib unit suite (CI's module list,
                                                             #   tests/test_install*.py excluded)
    python scripts/quality_report.py --run-tests --include-slow   # + installer tests (~9 min)
    python scripts/quality_report.py --run-examples PYTHON   # + tests/examples with PYTHON
                                                             #   (needs requirements-examples.txt)
    python scripts/quality_report.py --run-examples PYTHON --require-example-deps   # as CI: a missing
                                                             #   dependency fails, slow tests run
    python scripts/quality_report.py --test-results R.json --example-coverage C.jsonl
    python scripts/quality_report.py --example-coverage C.jsonl --require-coverage-revision
    python scripts/quality_report.py --out quality-report.json --timestamp 2026-10-28T00:00:00Z

    # Write a results file another run can ingest (any interpreter; stdlib only):
    python scripts/quality_report.py --collect-tests R.json --suite unit|examples [--include-slow]

Determinism: for the same tree, environment and inputs the JSON is byte-identical
except "generated_at" (pin it with --timestamp). Test outcomes are only as
deterministic as the tests themselves.

Exit codes: 0 report produced and no hard failure; 1 report produced with at
least one hard failure (listed in summary.hard_failures; a tool that cannot be
started or times out is recorded as one); 2 usage error, or an input file
(--test-results / --example-coverage) could not be read or parsed. An invalid
docs/reviews.json is a hard failure (exit 1), not a usage error.
"""
from __future__ import annotations

import argparse
import ast
import datetime as dt
import fnmatch
import hashlib
import json
import os
import platform
import re
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
# 2: examples.status gained executed-and-passed / coverage-only-outcome-unknown /
# coverage-unattributable (all-runnable-blocks-executed moved to examples.coverage_status),
# human_review gained per-skill records, tests.examples gained attribution. Within version 2:
# several results documents of one suite are merged (tests.<suite>.documents), and
# examples.example_results splits a skill's outcomes into current / stale / untied.
REPORT_SCHEMA_VERSION = 2
RESULTS_SCHEMA = "ml-ai-skills/test-results"
RESULTS_SCHEMA_VERSION = 1
SCHEMA_VERSION = RESULTS_SCHEMA_VERSION  # earlier name, kept for callers of the results format
# Coverage records written by tests/examples/harness.py; a record without "schema" is the
# earlier format (skill, blocks, total_blocks[, skill_sha256]).
COVERAGE_SCHEMA = "ml-ai-skills/example-coverage"
COVERAGE_SCHEMA_VERSIONS = (2,)
# Per-skill human review records (docs/reviews.json).
REVIEWS_PATH = Path("docs") / "reviews.json"
REVIEWS_SCHEMA = "ml-ai-skills/skill-reviews"
REVIEWS_SCHEMA_VERSION = 1
REVIEW_REQUIRED_KEYS = ("skill", "reviewer", "date", "skill_sha256", "scope", "outcome")
REVIEW_OPTIONAL_KEYS = ("notes",)
REVIEW_SCOPES = ("full", "content", "code", "references")   # only "full" makes a skill "reviewed"
REVIEW_OUTCOMES = ("approved", "changes-requested", "rejected")
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
# Module-level names an example test module can use to declare the skill(s) it exercises.
DECLARED_SKILL_NAMES = ("SKILL", "SKILLS")
# Harness entry points whose first argument names the skill whose SKILL.md is read.
HARNESS_SKILL_CALLS = ("run_blocks", "python_blocks", "load_skill", "block_index")
EXAMPLE_ID_RE = re.compile(r"(?:^|[.\s(])examples\.(test_\w+)")
# The only report field allowed to differ between two runs on identical inputs.
NONDETERMINISTIC_FIELDS = ("generated_at",)
SLOW_UNIT_GLOB = "test_install*.py"
SUITES = ("unit", "examples")
# Router metrics: higher is better / lower is better. Taken from scripts/eval_router.py (the
# split ci.yml's evals-offline gate uses), so a newly gated metric cannot go unflagged here.
# eval_router is still run as a subprocess for its numbers; only these constants are imported.
# scripts/ goes on sys.path first, as the sibling scripts do, so `python -I` and imports from
# other directories work too.
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))
from eval_router import HIGHER_IS_BETTER as ROUTER_MIN  # noqa: E402
from eval_router import LOWER_IS_BETTER as ROUTER_MAX  # noqa: E402
MESSAGE_LIMIT = 300
FAILING_OUTCOMES = ("failed", "error", "unexpected_success")

KNOWN_LIMITATIONS = (
    {"id": "no-model-graded-evals",
     "text": "Real model output was graded once (2026-10-30: claude-cli sonnet answers, opus judge, one sample "
             "per case per arm, 84 cases; docs/KNOWN-ISSUES.md #1). That judge is uncalibrated against human "
             "labels, so the rubric layer and the 'correct' uplift rest on a model judge; offline runs still "
             "check fixtures against regex hard assertions only."},
    {"id": "regex-hard-assertions",
     "text": "Hard assertions check that concepts are named and anticipated wrong conclusions are absent, "
             "not that reasoning is correct; every reviewer probe was tuned against and is now a fixture. "
             "On the blind-written held-out set (evals/_grading/heldout, model-adjudicated labels, likely easier "
             "than real output) the regex layer measured TPR 0.855 and TNR 0.916."},
    {"id": "examples-use-stubs",
     "text": "Executed example blocks run on tiny synthetic data, random-weight models and stubbed services; "
             "they show the code runs against the pinned versions, not that the advice is optimal."},
    {"id": "lexical-router",
     "text": "The router is lexical (no embeddings). The test split has 142 cases in 85 families; labels were "
             "reviewed by independent model reviewers, not people, so differences of a few points may be noise "
             "(docs/ROUTING.md)."},
    {"id": "runtime-loading-untested",
     "text": "Claude Code's runtime skill discovery/selection is not automatically tested; the plugin tests stop "
             "at plugin validate --strict and the marketplace add/install/list/update/reinstall return codes."},
    {"id": "platform-coverage",
     "text": "Locally executed on Windows 11 + Git Bash, with some suites also run under WSL 2 Ubuntu; no "
             "GitHub Actions run of this tree has been observed, and macOS / bash 3.2 appear only in CI "
             "configuration (docs/KNOWN-ISSUES.md #4)."},
    {"id": "human-review-records",
     "text": "A skill counts as human-reviewed only with a docs/reviews.json record (scope full, outcome approved) "
             "for its current SKILL.md sha256. The reviewer field is not authenticated: the record is only as "
             "trustworthy as the review of the commit that added it. Phase 1/2 audits were agent reviews "
             "(docs/SKILL-AUDIT.md) and are not recorded there. Claim-level human-review entries in "
             "docs/claims.json are counted separately and never make a whole skill 'reviewed'."},
    {"id": "coverage-provenance",
     "text": "Coverage records carry skill_sha256, git revision, tracked-file dirty state, an untracked-file count, "
             "python, platform and a timestamp. Only the SKILL.md content (sha256) is compared; the revision is "
             "reported, not compared, and records without one are accepted unless --require-coverage-revision is "
             "given. With a dirty tree the revision does not pin the scripts and tests that ran. Coverage shows "
             "blocks ran without raising, not that assertions passed."},
    {"id": "example-attribution",
     "text": "Example test outcomes are attributed per module (SKILL/SKILLS declarations, the harness's recorded "
             "test_module, a static scan for the slug a module loads, module name as fallback). A module that "
             "exercises several skills attributes all its outcomes to each of them, and outcomes are not tied to "
             "individual blocks: coverage and results may come from different tests or runs."},
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
    return {"schema": RESULTS_SCHEMA, "schema_version": RESULTS_SCHEMA_VERSION, "suite": suite,
            "command": command, "include_slow": include_slow, "selection": selection,
            "python": platform.python_version(), "platform": platform.system(),
            "env": {k: os.environ.get(k) for k in ("ML_AI_SKILLS_REQUIRE_EXAMPLE_DEPS", "ML_AI_SKILLS_SLOW_EXAMPLES")},
            "provenance": provenance, "summary": summarize_records(records), "tests": records}


def validate_results(doc: Any) -> list[str]:
    problems = []
    if not isinstance(doc, dict) or doc.get("schema") != RESULTS_SCHEMA:
        return [f"not a {RESULTS_SCHEMA} document"]
    if doc.get("schema_version") != RESULTS_SCHEMA_VERSION:
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
            if args.require_example_deps:
                # As the CI examples job: a missing dependency fails and slow tests run (harness.py).
                env["ML_AI_SKILLS_REQUIRE_EXAMPLE_DEPS"] = "1"
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
    raw["example_modules"] = scan_example_modules(root, {s["skill"] for s in raw["skills"]})
    raw["reviews"] = load_reviews(root)
    raw["today"] = args.today or dt.date.today().isoformat()
    raw["environment"] = {"python": platform.python_version(), "platform": platform.system(),
                          "platform_release": platform.release(), **_git_state(root)}
    raw["options"] = {"run_tests": bool(args.run_tests), "include_slow": bool(args.include_slow),
                      "run_examples": bool(args.run_examples), "slow_examples": bool(args.slow_examples),
                      "require_example_deps": bool(args.require_example_deps),
                      "require_coverage_revision": bool(getattr(args, "require_coverage_revision", False)),
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
    """HEAD, tracked-file dirty state and untracked-file count of `root`, or None when `root`
    is not itself the top of a git work tree (an extracted release inside another repository
    must not report that repository). Same semantics as tests/examples/harness.py: untracked
    files (CI's own logs and reports) are counted, not folded into git_dirty."""
    none = {"git_revision": None, "git_dirty": None, "git_untracked_files": None}

    def git(*args: str, timeout: int = 30) -> subprocess.CompletedProcess:
        # --no-optional-locks: never take .git/index.lock (other git processes may be running).
        return subprocess.run(["git", "--no-optional-locks", *args], cwd=root, capture_output=True, text=True,
                              encoding="utf-8", errors="replace", timeout=timeout)
    try:
        top = git("rev-parse", "--show-toplevel")
        rev = git("rev-parse", "HEAD")
        if top.returncode != 0 or rev.returncode != 0 or (
                os.path.normcase(os.path.realpath(top.stdout.strip())) != os.path.normcase(os.path.realpath(root))):
            return none
        dirty = git("status", "--porcelain", "--untracked-files=no", timeout=120)
        untracked = git("ls-files", "--others", "--exclude-standard", "-z", timeout=120)
    except (OSError, subprocess.SubprocessError):
        return none
    return {"git_revision": rev.stdout.strip(),
            "git_dirty": bool(dirty.stdout.strip()) if dirty.returncode == 0 else None,
            "git_untracked_files": (len([p for p in untracked.stdout.split("\0") if p])
                                    if untracked.returncode == 0 else None)}


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


def scan_example_module(source: str, known: set[str]) -> dict:
    """Statically find the skills an example test module exercises (never imports it).

    Returns {"declared", "harness_calls", "slug_constant", "unknown_slugs", "parse_error"}:
    ``declared`` from a module-level ``SKILL = "slug"`` / ``SKILLS = ("a", "b")`` (None when the
    module declares nothing; ``SKILLS = ()`` declares that it exercises no skill);
    ``harness_calls`` from the first argument of run_blocks / python_blocks / load_skill /
    block_index (a string literal, a module-level string constant, or ``X / SLUG``);
    ``slug_constant`` from a module-level ``SLUG = "slug"``. Only slugs of skills in the
    current tree count; other strings in those places are listed in ``unknown_slugs``.
    """
    out: dict[str, Any] = {"declared": None, "harness_calls": [], "slug_constant": [], "unknown_slugs": [],
                           "parse_error": None}
    try:
        tree = ast.parse(source)
    except (SyntaxError, ValueError) as e:
        out["parse_error"] = f"{type(e).__name__}: {e}"[:MESSAGE_LIMIT]
        return out
    consts: dict[str, Any] = {}
    for node in tree.body:
        if isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
            name, value = node.targets[0].id, node.value
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name) and node.value is not None:
            name, value = node.target.id, node.value
        else:
            continue
        if isinstance(value, ast.Constant) and isinstance(value.value, str):
            consts[name] = value.value
        elif isinstance(value, (ast.Tuple, ast.List)) and all(
                isinstance(e, ast.Constant) and isinstance(e.value, str) for e in value.elts):
            consts[name] = [e.value for e in value.elts]

    def resolve(node: ast.AST) -> str | None:
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            return node.value
        if isinstance(node, ast.Name) and isinstance(consts.get(node.id), str):
            return consts[node.id]
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Div):  # REPO_ROOT / SLUG
            return resolve(node.right)
        return None

    def add(key: str, slug: str) -> None:
        target = out[key] if slug in known else out["unknown_slugs"]
        if slug not in target:
            target.append(slug)

    for name in DECLARED_SKILL_NAMES:
        if name not in consts:
            continue
        value = consts[name]
        if out["declared"] is None:
            out["declared"] = []
        for slug in ([value] if isinstance(value, str) else value):
            add("declared", slug)
    if isinstance(consts.get("SLUG"), str):
        add("slug_constant", consts["SLUG"])
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not node.args:
            continue
        func = node.func
        fname = func.attr if isinstance(func, ast.Attribute) else func.id if isinstance(func, ast.Name) else None
        if fname in HARNESS_SKILL_CALLS:
            slug = resolve(node.args[0])
            if slug is not None:
                add("harness_calls", slug)
    for key in ("declared", "harness_calls", "slug_constant", "unknown_slugs"):
        if out[key] is not None:
            out[key].sort()
    return out


def scan_example_modules(root: Path, known: set[str]) -> dict[str, dict]:
    """scan_example_module for every tests/examples/test_*.py, keyed by module stem."""
    out = {}
    for path in sorted((root / "tests" / "examples").glob("test_*.py")):
        try:
            source = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as e:
            out[path.stem] = {"declared": None, "harness_calls": [], "slug_constant": [], "unknown_slugs": [],
                              "parse_error": f"cannot read: {e}"[:MESSAGE_LIMIT]}
            continue
        out[path.stem] = scan_example_module(source, known)
    return out


def load_reviews(root: Path) -> dict:
    """Read docs/reviews.json: {"path", "present", "doc", "error"} (validation happens in build_report)."""
    path = root / REVIEWS_PATH
    if not path.is_file():
        return {"path": REVIEWS_PATH.as_posix(), "present": False, "doc": None, "error": None}
    try:
        return {"path": REVIEWS_PATH.as_posix(), "present": True,
                "doc": json.loads(path.read_text(encoding="utf-8")), "error": None}
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as e:
        return {"path": REVIEWS_PATH.as_posix(), "present": True, "doc": None,
                "error": f"{type(e).__name__}: {e}"[:MESSAGE_LIMIT]}


def validate_reviews(doc: Any, known: set[str], today: str | None) -> list[str]:
    """Problems with a docs/reviews.json document (empty list = valid).

    Format: {"schema": "ml-ai-skills/skill-reviews", "schema_version": 1, "description"?: str,
    "reviews": [{"skill", "reviewer", "date" (YYYY-MM-DD), "skill_sha256" (64 lowercase hex,
    LF-normalised SKILL.md), "scope" (full|content|code|references), "outcome"
    (approved|changes-requested|rejected), "notes"?}]}. A date may be at most one day after
    `today` (same tolerance as docs/claims.json, for writers in zones ahead of UTC).
    """
    if not isinstance(doc, dict) or doc.get("schema") != REVIEWS_SCHEMA:
        return [f"not a {REVIEWS_SCHEMA} document"]
    problems = []
    if doc.get("schema_version") != REVIEWS_SCHEMA_VERSION:
        problems.append(f"unsupported schema_version {doc.get('schema_version')!r}")
    extra = sorted(set(doc) - {"schema", "schema_version", "description", "reviews"})
    if extra:
        problems.append(f"unknown top-level key(s) {extra}")
    if "description" in doc and not isinstance(doc["description"], str):
        problems.append("'description' must be a string")
    reviews = doc.get("reviews")
    if not isinstance(reviews, list):
        return [*problems, "'reviews' must be a list"]
    latest = None
    if today:
        try:
            latest = dt.date.fromisoformat(today) + dt.timedelta(days=1)
        except ValueError:
            latest = None
    seen = set()
    for i, r in enumerate(reviews):
        where = f"reviews[{i}]"
        if not isinstance(r, dict):
            problems.append(f"{where} is not an object")
            continue
        missing = [k for k in REVIEW_REQUIRED_KEYS if k not in r]
        unknown = sorted(set(r) - set(REVIEW_REQUIRED_KEYS) - set(REVIEW_OPTIONAL_KEYS))
        if missing:
            problems.append(f"{where}: missing key(s) {missing}")
        if unknown:
            problems.append(f"{where}: unknown key(s) {unknown}")
        for k in (*REVIEW_REQUIRED_KEYS, *REVIEW_OPTIONAL_KEYS):
            if k in r and (not isinstance(r[k], str) or not r[k].strip()):
                problems.append(f"{where}: {k!r} must be a non-empty string")
        if missing or any(not isinstance(r.get(k), str) for k in REVIEW_REQUIRED_KEYS):
            continue
        if r["skill"] not in known:
            problems.append(f"{where}: skill {r['skill']!r} is not a skill folder in this tree")
        if not SHA256_RE.match(r["skill_sha256"]):
            problems.append(f"{where}: skill_sha256 must be 64 lowercase hex characters")
        if r["scope"] not in REVIEW_SCOPES:
            problems.append(f"{where}: scope {r['scope']!r} not in {list(REVIEW_SCOPES)}")
        if r["outcome"] not in REVIEW_OUTCOMES:
            problems.append(f"{where}: outcome {r['outcome']!r} not in {list(REVIEW_OUTCOMES)}")
        try:
            if not DATE_RE.match(r["date"]):
                raise ValueError(r["date"])
            day = dt.date.fromisoformat(r["date"])
            if latest and day > latest:
                problems.append(f"{where}: date {r['date']} is in the future (today {today})")
        except ValueError:
            problems.append(f"{where}: date {r['date']!r} is not YYYY-MM-DD")
        key = tuple(r[k] for k in REVIEW_REQUIRED_KEYS if k != "outcome")
        if key in seen:
            problems.append(f"{where}: duplicate of an earlier record (same skill, reviewer, date, sha256, scope)")
        seen.add(key)
    return problems


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


def _example_module_of(test_id: str) -> str | None:
    """Module stem of an examples-suite test id, including setUpClass and import failures:
    'examples.test_x.C.test_y', 'setUpClass (examples.test_x.C)',
    'unittest.loader._FailedTest.examples.test_x' -> 'test_x'."""
    m = EXAMPLE_ID_RE.search(test_id)
    return m.group(1) if m else None


def _coverage_module(name: Any) -> str | None:
    """Example module stem from a harness-recorded test_module ('tests.examples.test_x' when run from
    the repo root, 'examples.test_x' with tests/ as top level, or 'test_x')."""
    if not isinstance(name, str):
        return None
    if name.startswith("tests."):
        name = name[len("tests."):]
    if name.startswith("examples."):
        name = name[len("examples."):]
    return name if name.startswith("test_") and "." not in name else None


def _parse_coverage(lines: list[str], inventory: dict[str, dict], require_revision: bool = False) -> dict:
    """Fold coverage jsonl into per-skill evidence, keeping only records tied to this tree.

    Returns {"executed": {slug: set}, "stale": {slug: [reasons]}, "unattributable": {slug: [reasons]},
    "modules": {module stem: set(slugs)}, "bad": n, "unknown_skills": [...], "provenance": {...}}.
    A record counts only when its total_blocks AND skill_sha256 equal the current skill's; a
    differing value makes it stale, a missing one makes it unattributable. With
    require_revision a record without a git_revision is unattributable too.
    """
    executed: dict[str, set[int]] = {}
    stale: dict[str, list[str]] = {}
    unattributable: dict[str, list[str]] = {}
    modules: dict[str, set[str]] = {}
    unknown: set[str] = set()
    bad = 0
    prov: dict[str, Any] = {"records": 0, "accepted": 0, "legacy_records": 0, "records_without_revision": 0,
                            "records_without_test": 0, "records_with_untracked_files": 0,
                            "git_revisions": set(), "git_dirty": set(),
                            "python": set(), "platforms": set()}
    times: list[str] = []

    def note(bucket: dict[str, list[str]], slug: str, reason: str) -> None:
        if reason not in bucket.setdefault(slug, []):
            bucket[slug].append(reason)

    for line in lines:
        if not line.strip():
            continue
        try:
            rec = json.loads(line)
            slug = rec["skill"]
            blocks = {int(i) for i in rec["blocks"]}
            if not isinstance(slug, str):
                raise TypeError("skill is not a string")
            if "schema" in rec and (rec["schema"] != COVERAGE_SCHEMA
                                    or rec.get("schema_version") not in COVERAGE_SCHEMA_VERSIONS):
                raise ValueError("unsupported coverage schema")
        except (json.JSONDecodeError, KeyError, TypeError, ValueError, AttributeError):
            bad += 1
            continue
        prov["records"] += 1
        if "schema" not in rec:
            prov["legacy_records"] += 1
        rev = rec.get("git_revision")
        if isinstance(rev, str) and rev:
            prov["git_revisions"].add(rev)
        else:
            prov["records_without_revision"] += 1
        if isinstance(rec.get("git_dirty"), bool):
            prov["git_dirty"].add(rec["git_dirty"])
        untracked = rec.get("git_untracked_files")
        if isinstance(untracked, int) and not isinstance(untracked, bool) and untracked > 0:
            prov["records_with_untracked_files"] += 1
        for key, field in (("python", "python"), ("platforms", "platform")):
            if rec.get(field) is not None:
                prov[key].add(str(rec[field]))
        if isinstance(rec.get("recorded_at"), str):
            times.append(rec["recorded_at"])
        module = _coverage_module(rec.get("test_module"))
        if module is None:
            prov["records_without_test"] += 1
        inv = inventory.get(slug)
        if inv is None:
            unknown.add(slug)  # not a skill in the current tree: listed, never attributed
            continue
        if module is not None:
            modules.setdefault(module, set()).add(slug)
        total, sha = rec.get("total_blocks"), rec.get("skill_sha256")
        has_total, has_sha = isinstance(total, int), isinstance(sha, str) and bool(sha)
        if not (has_total and has_sha):
            missing = " or ".join(k for k, ok in (("total_blocks", has_total), ("skill_sha256", has_sha)) if not ok)
            note(unattributable, slug, f"coverage record has no {missing} to tie it to the current SKILL.md")
        elif require_revision and not (isinstance(rev, str) and rev):
            note(unattributable, slug, "coverage record has no git_revision (--require-coverage-revision)")
        elif total != inv["python_blocks"]:
            note(stale, slug, f"coverage record has total_blocks {total}, "
                              f"current SKILL.md has {inv['python_blocks']} python block(s)")
        elif sha != inv.get("sha256"):
            note(stale, slug, "coverage record skill_sha256 differs from the current SKILL.md")
        else:
            prov["accepted"] += 1
            executed.setdefault(slug, set()).update(blocks)
    times.sort()
    out_prov = {k: sorted(v) if isinstance(v, set) else v for k, v in prov.items()}
    out_prov["recorded_between"] = [times[0], times[-1]] if times else None
    return {"executed": executed, "stale": stale, "unattributable": unattributable, "modules": modules,
            "bad": bad, "unknown_skills": sorted(unknown), "provenance": out_prov}


def _attribute_examples(raw: dict, inventory: dict[str, dict], coverage_modules: dict[str, set[str]],
                        ex_docs: list[dict]) -> tuple[dict[str, dict], dict[str, list[tuple[int, dict]]], list[str]]:
    """Attribute example test modules to skills.

    Returns (modules, result records per skill as (index into ex_docs, record), unparseable
    test ids). Each module entry lists its skills, the source of each attribution and, when
    results were ingested, its outcome counts over every document. A module that no source
    attributes gets status "unattributed-test-module" (or "declared-no-skill" for SKILLS = ()):
    its outcomes still count in the suite totals, but towards no skill.
    """
    scans = raw.get("example_modules") or {}
    results_by_module: dict[str, list[tuple[int, dict]]] = {}
    unparseable: list[str] = []
    for i, doc in enumerate(ex_docs):
        for t in doc.get("tests", []):
            mod = _example_module_of(t["id"])
            if mod is None:
                unparseable.append(t["id"])
            else:
                results_by_module.setdefault(mod, []).append((i, t))
    modules: dict[str, dict] = {}
    for mod in sorted(set(scans) | set(coverage_modules) | set(results_by_module)):
        scan = scans.get(mod) or {}
        declared = scan.get("declared")
        recorded = sorted(coverage_modules.get(mod, set()))
        excluded: list[str] = []
        if declared == []:
            # SKILLS = () says "this module is evidence for no skill" (e.g. harness self-tests that
            # run a real skill's blocks to test the harness): its harness records are only listed.
            sources: dict[str, list[str]] = {"declared": []}
            excluded = recorded
        elif declared is not None:
            # A non-empty declaration replaces the static inference; harness records (the module
            # really ran that skill's blocks, so its failures concern it) are still added, and flagged.
            sources = {"declared": list(declared), "harness-record": recorded}
        else:
            sources = {"harness-record": recorded, "harness-call": list(scan.get("harness_calls", [])),
                       "slug-constant": list(scan.get("slug_constant", []))}
        skills = sorted({s for v in sources.values() for s in v})
        by_name = next((slug for slug in sorted(inventory) if _example_module_for(slug) == mod), None)
        if not skills and by_name and declared is None:
            sources["module-name"] = [by_name]
            skills = [by_name]
        if skills:
            status = "attributed"
        elif declared is not None:
            status = "declared-no-skill"  # e.g. harness self-tests: explicitly evidence for no skill
        else:
            status = "unattributed-test-module"
        entry: dict[str, Any] = {
            "skills": skills, "status": status,
            "sources": {k: v for k, v in sources.items() if v},
            "outcomes": (summarize_records([t for _, t in results_by_module[mod]])
                         if mod in results_by_module else None)}
        if declared and set(recorded) - set(declared):
            entry["undeclared_harness_records"] = sorted(set(recorded) - set(declared))
        if excluded:
            entry["excluded_harness_records"] = excluded
        if scan.get("unknown_slugs"):
            entry["unknown_slugs"] = scan["unknown_slugs"]
        if scan.get("parse_error"):
            entry["parse_error"] = scan["parse_error"]
        if scans and mod not in scans:
            entry["note"] = f"no tests/examples/{mod}.py in this tree"
        modules[mod] = entry
    per_skill: dict[str, list[tuple[int, dict]]] = {}
    for mod, entry in modules.items():
        for slug in entry["skills"]:
            per_skill.setdefault(slug, []).extend(results_by_module.get(mod, []))
    return modules, per_skill, unparseable


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
        if res:
            section["exit_code"] = res.get("exit_code")
            section["output_tail"] = (res.get("stdout", "") + res.get("stderr", "")).strip().splitlines()[-3:]
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


def _merge_provenance(provs: list[dict]) -> dict:
    """One provenance summary for several results documents of a suite (each kept under "documents")."""
    if len(provs) == 1:
        return provs[0]
    revs = sorted({p["git_revision"] for p in provs if p["git_revision"]})
    matches = [p["matches_current_revision"] for p in provs]
    dirty = [p["git_dirty"] for p in provs]
    recorded_stale = [p["stale_skills"] for p in provs if p["stale_skills"] is not None]
    return {"recorded": all(p["recorded"] for p in provs),
            "git_revision": revs[0] if len(revs) == 1 and all(p["git_revision"] for p in provs) else None,
            "git_revisions": revs,
            "git_dirty": True if True in dirty else (False if all(d is False for d in dirty) else None),
            "matches_current_revision": (False if False in matches else
                                         True if all(m is True for m in matches) else None),
            "stale_skills": sorted(set().union(*recorded_stale)) if recorded_stale else None,
            "documents": provs,
            "note": "Several results documents were merged: suite totals and hard failures cover all of them; "
                    "per-skill outcomes compare each document's own provenance with the current tree."}


def _merged_field(docs: list[dict], key: str) -> Any:
    values = [d.get(key) for d in docs]
    return values[0] if all(v == values[0] for v in values) else values


def _tests_section(raw: dict, inventory: dict[str, dict]) -> dict:
    """Per-suite totals. Several valid documents of one suite are merged, never one chosen:
    picking one would let a failure in another document vanish from the totals."""
    suites: dict[str, Any] = {}
    grouped: dict[str, list[dict]] = {}
    for doc in raw.get("test_results", []):
        problems = validate_results(doc)
        if problems:
            suites.setdefault("invalid", []).append(problems)
            continue
        grouped.setdefault(doc["suite"], []).append(doc)
    env = raw.get("environment", {})
    for name, docs in grouped.items():
        records = [t for d in docs for t in d["tests"]]
        suites[name] = {"documents": len(docs),
                        **{k: _merged_field(docs, k) for k in ("command", "python", "platform", "env")},
                        # Installer tests ran if any merged unit document included them.
                        "include_slow": any(bool(d.get("include_slow")) for d in docs),
                        **summarize_records(records),
                        "provenance": _merge_provenance([_results_provenance(d, env, inventory) for d in docs]),
                        "failures": [t for t in records if t["outcome"] in ("failed", "error",
                                                                             "unexpected_success")]}
    for name in SUITES:
        suites.setdefault(name, None)
    return suites


def _results_sha_matches(doc: dict | None, slug: str, inv: dict) -> bool:
    """True only when a results document recorded this skill's SKILL.md sha256 and it is the current one."""
    prov = (doc or {}).get("provenance")
    shas = prov.get("skill_sha256") if isinstance(prov, dict) else None
    return bool(isinstance(shas, dict) and inv.get("sha256") and shas.get(slug) == inv["sha256"])


def _inputs_section(raw: dict, tests: dict, cov: dict) -> list[dict]:
    """Where each piece of ingested or collected evidence came from, and whether it matches this tree."""
    out = []
    for item in raw.get("inputs", []):
        entry = dict(item)
        if item.get("kind") == "example-coverage":
            entry["provenance"] = ("this run" if str(item.get("source", "")).startswith("this-run") else
                                   "ingested; see the example-coverage-provenance entry")
        out.append(entry)
    for name in SUITES:
        s = tests.get(name)
        if s:
            out.append({"kind": "test-results-provenance", "suite": name, **s["provenance"]})
    if raw.get("example_coverage"):
        prov = dict(cov["provenance"])
        cur = raw.get("environment", {}).get("git_revision")
        revs = prov["git_revisions"]
        # True: every record names the current HEAD; False: some record names another revision;
        # None: unknown (no current revision, or some records name none).
        if cur and revs and revs != [cur]:
            prov["matches_current_revision"] = False
        elif cur and revs and not prov["records_without_revision"]:
            prov["matches_current_revision"] = True
        else:
            prov["matches_current_revision"] = None
        prov["note"] = ("A record counts only when its skill_sha256 and total_blocks match the current SKILL.md; "
                        "git revision, dirty state (tracked files), untracked-file count, python and platform are "
                        "reported, not compared, because the SKILL.md content is what the coverage is about. "
                        "Records without a git revision are accepted unless --require-coverage-revision is given.")
        out.append({"kind": "example-coverage-provenance", **prov})
    if cov["stale"]:
        out.append({"kind": "example-coverage-stale", "skills": sorted(cov["stale"])})
    if cov["unattributable"]:
        out.append({"kind": "example-coverage-unattributable", "skills": sorted(cov["unattributable"])})
    if cov["unknown_skills"]:
        out.append({"kind": "example-coverage-unknown-skills", "skills": cov["unknown_skills"],
                    "note": "coverage records for slugs that are not skill folders in this tree; not attributed"})
    return out


def _reviews_section(raw: dict, inventory: dict[str, dict]) -> tuple[dict, dict[str, list[dict]]]:
    """Validate docs/reviews.json and group its records by skill (only when the file is valid)."""
    info = raw.get("reviews")
    if not isinstance(info, dict) or not info.get("present"):
        return ({"status": "not-run", "path": REVIEWS_PATH.as_posix(), "records": None,
                 "note": "docs/reviews.json not found: per-skill human review was not measured"}, {})
    if info.get("error"):
        return {"status": "fail", "path": info.get("path"), "records": None, "problems": [info["error"]]}, {}
    doc = info.get("doc")
    problems = validate_reviews(doc, set(inventory), raw.get("today"))
    if problems:
        return {"status": "fail", "path": info.get("path"), "records": None, "problems": problems}, {}
    by_skill: dict[str, list[dict]] = {}
    for r in doc["reviews"]:
        by_skill.setdefault(r["skill"], []).append(r)
    return ({"status": "pass", "path": info.get("path"), "records": len(doc["reviews"]),
             "note": "Records are only as trustworthy as the review of the commit that added them; the reviewer "
                     "field is not authenticated, and agent reviews must not be recorded here."}, by_skill)


def _human_review(slug: str, inv: dict, records: list[dict], measured: bool, claims: int) -> dict:
    """Per-skill human review status; claim-level reviews are reported beside it, never promoted."""
    current = [r for r in records if inv.get("sha256") and r["skill_sha256"] == inv["sha256"]]
    worst = {o: i for i, o in enumerate(REVIEW_OUTCOMES)}  # ties on a date: the least favourable outcome
    latest = max(current, key=lambda r: (r["date"], worst[r["outcome"]], r["scope"] == "full"), default=None)
    if latest is not None:
        if latest["outcome"] != "approved":
            status = "review-changes-requested"
        elif latest["scope"] == "full":
            status = "reviewed"
        else:
            status = "partially-reviewed"
    elif records:
        status = "review-stale"
    elif claims:
        status = "claim-level-review-only"
    else:
        status = "not-recorded"
    return {"status": status, "per_skill_review_measured": measured, "per_skill_records": len(records),
            "current_records": len(current),
            "latest_current": ({k: latest[k] for k in ("reviewer", "date", "scope", "outcome")} if latest else None),
            "current_human_reviewed_claims": claims}


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

    cov = _parse_coverage(raw.get("example_coverage", []), inventory,
                          require_revision=bool(raw.get("options", {}).get("require_coverage_revision")))
    executed, cov_stale, cov_unattr = cov["executed"], cov["stale"], cov["unattributable"]
    coverage_measured = bool(raw.get("example_coverage"))
    # Every valid examples document (the same set _tests_section merges), each with its own provenance.
    ex_docs = [d for d in raw.get("test_results", []) if d.get("suite") == "examples" and not validate_results(d)]
    ex_provs = [_results_provenance(d, raw.get("environment", {}), inventory) for d in ex_docs]
    modules, example_records, unparseable_ids = _attribute_examples(raw, inventory, cov["modules"], ex_docs)
    unattributed = sorted(m for m, e in modules.items() if e["status"] == "unattributed-test-module")
    attribution = {
        "modules": modules, "unattributed_modules": unattributed,
        "unparseable_test_ids": unparseable_ids[:50], "unparseable_test_ids_total": len(unparseable_ids),
        "note": "Outcomes are attributed per module to every skill the module is attributed to. Outcomes of "
                "unattributed modules and unparseable ids count in the suite totals but towards no skill."}

    # --- per-skill human review -----------------------------------------------------------
    checks["reviews"], reviews_by_skill = _reviews_section(raw, inventory)
    reviews_measured = checks["reviews"]["status"] == "pass"
    if checks["reviews"]["status"] == "fail":
        hard.append(f"{checks['reviews']['path']}: {len(checks['reviews']['problems'])} problem(s)")

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
    if not reviews_measured:
        skipped.append({"check": "per-skill-human-review",
                        "reason": ("docs/reviews.json not found" if checks["reviews"]["status"] == "not-run" else
                                   "docs/reviews.json is invalid (see checks.reviews)")
                                  + ": no skill is counted as human-reviewed"})
    if not raw.get("claude_cli"):
        skipped.append({"check": "claude-cli",
                        "reason": "claude CLI not on PATH: plugin validate --strict and the local marketplace "
                                  "add/install/update tests in tests/test_generate_plugins.py skip (as does the "
                                  "opt-in tests/test_claude_cli_integration.py, ML_AI_SKILLS_CLAUDE_CLI=1). "
                                  "The export's upload-format rules are checked offline by "
                                  "tests/test_export_skills.py against the repo's own encoding of them (the "
                                  "upstream validators run only opt-in); no real upload is made, so acceptance "
                                  "by claude.ai or the Skills API is not checked"})
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
        tagged = example_records.get(slug, [])
        recs = [t for _, t in tagged]
        ex_counts = summarize_records(recs) if recs else None
        # Split by the provenance of the document each outcome came from: recorded the current
        # SKILL.md sha256 / recorded another (or none for this skill) / recorded no provenance.
        current = [t for i, t in tagged if _results_sha_matches(ex_docs[i], slug, inv)]
        stale_res = [t for i, t in tagged if not _results_sha_matches(ex_docs[i], slug, inv)
                     and ex_provs[i]["recorded"] and slug in (ex_provs[i]["stale_skills"] or [])]
        untied = [t for i, t in tagged if not _results_sha_matches(ex_docs[i], slug, inv)
                  and not (ex_provs[i]["recorded"] and slug in (ex_provs[i]["stale_skills"] or []))]
        stale_reasons = list(cov_stale.get(slug, []))
        unattr_reasons = list(cov_unattr.get(slug, []))

        # Block coverage: which runnable blocks ran without raising, from records tied to this SKILL.md.
        if runnable == 0:
            cov_status = "no-runnable-blocks"
        elif not coverage_measured:
            cov_status = "not-measured"
        elif done >= runnable:
            # Current records decide first; leftover stale/unattributable records are only listed.
            cov_status = "all-runnable-blocks-executed"
        elif cov_stale.get(slug):
            cov_status = "coverage-stale"
        elif done == 0 and cov_unattr.get(slug):
            cov_status = "coverage-unattributable"
        elif done == 0:
            cov_status = "not-executed"
        else:
            cov_status = "partially-executed"

        # Test outcome: results of the tests attributed to this skill, at the current SKILL.md sha256.
        if stale_res:
            stale_reasons.append("example test results were recorded against a different SKILL.md (sha256 differs)")
        if untied:
            unattr_reasons.append("example test results record no sha256 for this skill's current SKILL.md")
        if not ex_docs:
            outcome = "not-measured"
        elif not recs:
            outcome = "no-attributed-tests"
        elif any(t["outcome"] in FAILING_OUTCOMES for t in current + untied):
            outcome = "failing"  # even when the results cannot be tied to this tree: never hide a failure
        elif current:
            outcome = "passed" if any(t["outcome"] == "passed" for t in current) else "all-skipped"
        elif stale_res:
            outcome = "results-stale"  # a failure here concerns another SKILL.md; still a suite hard failure
        else:
            outcome = "results-unattributable"

        if runnable == 0:
            ex_status = "no-runnable-blocks"
        elif outcome == "failing":
            ex_status = "example-tests-failing"
        elif cov_status == "coverage-stale":
            ex_status = "coverage-stale"
        elif outcome == "results-stale":
            ex_status = "example-results-stale"
        elif cov_status == "all-runnable-blocks-executed":
            ex_status = "executed-and-passed" if outcome == "passed" else "coverage-only-outcome-unknown"
        else:
            ex_status = cov_status
        row = cov_rows.get(slug)
        if row is None:
            ev_status = "not-measured" if not cov_rows else "no-cases"
        elif row.get("missing"):
            ev_status = "partial"
        else:
            ev_status = "covered-offline"
        human = _human_review(slug, inv, reviews_by_skill.get(slug, []), reviews_measured, human_claims.get(slug, 0))
        tiers = []
        if structural_measured and n_err == 0:
            tiers.append("structural-valid")
        if ex_status in ("executed-and-passed", "coverage-only-outcome-unknown"):
            tiers.append("examples-executed")
        if ex_status == "executed-and-passed":
            tiers.append("example-tests-passed")
        if ev_status == "covered-offline":
            tiers.append("evals-covered-offline")
        if human["status"] == "reviewed":
            tiers.append("human-reviewed")
        per_skill.append({
            **{k: inv.get(k) for k in ("skill", "type", "domain", "lifecycle", "risk_level", "evidence_level",
                                         "last_review_date")},
            "skill_sha256": inv.get("sha256"),
            "structural": ({"status": "valid" if n_err == 0 else "invalid", "errors": n_err,
                            "warnings": warnings_by_skill.get(slug, 0)} if structural_measured else
                           {"status": "not-measured", "errors": None, "warnings": None}),
            "examples": {"status": ex_status, "coverage_status": cov_status, "outcome": outcome,
                         "python_blocks": inv["python_blocks"],
                         "illustrative_blocks": inv["illustrative_blocks"], "runnable_blocks": runnable,
                         "executed_blocks": done if coverage_measured else None, "example_tests": ex_counts,
                         "example_results": ({"current": len(current), "stale": len(stale_res),
                                              "untied": len(untied)} if recs else None),
                         "test_modules": sorted(m for m, e in modules.items() if slug in e["skills"]),
                         **({"stale_reasons": stale_reasons} if stale_reasons else {}),
                         **({"unattributable_reasons": unattr_reasons} if unattr_reasons else {})},
            "evals": {"status": ev_status, "model_graded": False,
                      **({k: row.get(k) for k in ("cases", "non_happy_path_cases", "hard_assertion_cases",
                                                  "executable_cases", "has_plan", "missing")} if row else {})},
            "claims": claims_by_skill.get(slug, {}),
            "freshness": fresh_by_skill.get(slug, {}),
            # Per-skill records (docs/reviews.json) and claim-level reviews (docs/claims.json) are kept apart.
            "human_review": human,
            "evidence_tiers": tiers,
        })

    def count(pred) -> int:
        return sum(1 for s in per_skill if pred(s))

    def slugs(*statuses: str, field: str = "status") -> list[str]:
        return sorted(s["skill"] for s in per_skill if s["examples"][field] in statuses)

    summary = {
        "hard_failures": hard,
        "skills_total": len(per_skill),
        "skills_structural_valid": (count(lambda s: s["structural"]["status"] == "valid")
                                    if structural_measured else None),
        "skills_examples_all_runnable_executed": (count(lambda s: s["examples"]["coverage_status"] ==
                                                        "all-runnable-blocks-executed") if coverage_measured else None),
        "skills_examples_executed_and_passed": (count(lambda s: s["examples"]["status"] == "executed-and-passed")
                                                if coverage_measured and ex_docs else None),
        "skills_examples_coverage_only_outcome_unknown": slugs("coverage-only-outcome-unknown"),
        "skills_example_tests_failing": slugs("example-tests-failing"),
        "skills_with_runnable_blocks": count(lambda s: s["examples"]["runnable_blocks"] > 0),
        "skills_evals_covered_offline": count(lambda s: s["evals"]["status"] == "covered-offline"),
        # Per-skill records only (docs/reviews.json, current sha256, scope full, approved); None if not measured.
        "skills_human_reviewed": (count(lambda s: s["human_review"]["status"] == "reviewed")
                                  if reviews_measured else None),
        "skills_partially_human_reviewed": count(lambda s: s["human_review"]["status"] == "partially-reviewed"),
        "skills_human_review_stale": sorted(s["skill"] for s in per_skill
                                            if s["human_review"]["status"] == "review-stale"),
        "skills_with_claim_level_human_review": count(lambda s: s["human_review"]["current_human_reviewed_claims"]),
        "skills_with_stale_example_evidence": slugs("coverage-stale", "example-results-stale"),
        "skills_with_unattributable_example_evidence": sorted(
            s["skill"] for s in per_skill if s["examples"]["coverage_status"] == "coverage-unattributable"
            or s["examples"]["outcome"] == "results-unattributable"),
        "unattributed_test_modules": unattributed,
        "model_graded_evals": "unmeasured",
        "statement": ("No skill is labelled verified. Structural validity, executed examples, passing example "
                      "tests, offline eval coverage and recorded human review are separate, partial kinds of "
                      "evidence; none of them shows the advice is correct."),
    }
    if cov["bad"]:
        summary["coverage_lines_ignored"] = cov["bad"]

    return {
        "schema": REPORT_SCHEMA, "schema_version": REPORT_SCHEMA_VERSION, "generated_at": timestamp,
        "environment": raw.get("environment", {}), "options": opts, "summary": summary,
        "inputs": _inputs_section(raw, tests, cov),
        "checks": checks, "tests": tests, "example_attribution": attribution, "skills": per_skill,
        "skipped_checks": skipped,
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
    ap.add_argument("--require-example-deps", action="store_true",
                    help="set ML_AI_SKILLS_REQUIRE_EXAMPLE_DEPS=1 for examples, as CI does: a missing dependency "
                         "fails and slow examples run")
    ap.add_argument("--test-results", nargs="+", metavar="FILE", help="ingest --collect-tests results files")
    ap.add_argument("--example-coverage", nargs="+", metavar="FILE", help="ingest coverage jsonl files")
    ap.add_argument("--require-coverage-revision", action="store_true",
                    help="count coverage records without a git_revision as unattributable (default: accepted, "
                         "since a release archive has no git checkout; the SKILL.md sha256 is still required)")
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
