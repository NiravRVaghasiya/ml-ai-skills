#!/usr/bin/env python3
"""Run the behavioral eval suite and write a machine-readable JSON report.

Modes (see docs/EVALUATION.md and evals/SCHEMA.md "Running evals"):

offline (default - no network, deterministic)
    python scripts/run_evals.py [--out report.json]
        validate evals/ and run the fixture self-tests
    python scripts/run_evals.py --responses recorded.jsonl [--rubric-labels labels.jsonl]
        + grade recorded responses ({"case_id", "condition", "response"[, "sample"]} per line)
    python scripts/run_evals.py --run-executable [--python PATH] [--slow-examples] [--require-executed]
        + run the tests/examples unittest ids of `kind: executable` cases
    python scripts/run_evals.py --grade-heldout | --grade-labelled {heldout,calibration,dev-fixtures,PATH}
        score the regex graders (and an optional judge) against a labelled set

model (explicit opt-in; see docs/EVALUATION.md "Backends" for each provider's requirements)
    python scripts/run_evals.py --provider anthropic --model ID --allow-network --max-cost-usd USD
        --price-input-per-mtok P --price-output-per-mtok P   (+ ANTHROPIC_API_KEY in the environment)
    python scripts/run_evals.py --provider claude-cli --model ID --allow-network --max-cost-usd USD
    python scripts/run_evals.py --provider replay --replay-file recorded.jsonl
        [--samples N] [--run-dir DIR [--resume]] [--judge-provider P --judge-model ID] [--dry-run]
    Each case runs with the skill's SKILL.md as system context (with_skill) and without it
    (without_skill), same model and parameters. Paid providers never run without a cost ceiling.

compare
    python scripts/run_evals.py --compare BASE.json NEW.json
        regressions (passed in BASE; failed, missing, or no longer gradable in
        NEW), fixture regressions, or an invalid NEW report -> exit 1

Exit codes: 0 ok (a failing response is a measurement, not a harness error);
1 validation/self-test/input failure, executable test failure, a skipped
executable test under --require-executed, labelled-set errors, or a regression
(and, with --strict, any graded hard failure or non-ok model attempt);
2 usage error, or not-run: model mode refused (no model configured, a missing
requirement, or a worst-case cost above the ceiling) - nothing was sent.
"""
from __future__ import annotations

import argparse
import datetime as _dt
import json
import os
import platform
import re
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Callable

sys.path.insert(0, str(Path(__file__).resolve().parent))
import eval_backends as eb  # noqa: E402
import eval_grading as eg  # noqa: E402
import eval_lib as el  # noqa: E402
import skills_lib as sl  # noqa: E402

# Re-exported for callers and tests that import them from here.
from eval_backends import (  # noqa: E402,F401
    _OPENER,
    API_URL,
    DEFAULT_ANTHROPIC_VERSION,
    RETRY_STATUSES,
    ApiError,
    MessagesClient,
    Transport,
    TransportError,
    _api_error_message,
    _NoRedirectHandler,
    response_text,
    urllib_transport,
)
from eval_grading import JUDGE_SYSTEM_PROMPT, JUDGE_USER_TEMPLATE  # noqa: E402,F401

REPORT_SCHEMA = "ml-ai-skills/eval-report"
COMPARE_SCHEMA = "ml-ai-skills/eval-compare"
PLAN_SCHEMA = "ml-ai-skills/eval-plan"
RUN_SCHEMA = "ml-ai-skills/eval-run"
# 2: graded[].sample/rubric/verdict, summary.final, uplift, provenance, verdict_rules,
#    model_evaluation, grader_version. --compare reads version 1 and 2 reports.
SCHEMA_VERSION = 2
READABLE_SCHEMA_VERSIONS = (1, 2)
# The only report field allowed to differ between two runs on identical inputs.
NONDETERMINISTIC_FIELDS = ("generated_at",)
CONDITIONS = ("with_skill", "without_skill")
CONDITION_RE = re.compile(r"^[a-z][a-z0-9_]*$")
PROVIDERS = ("anthropic", "claude-cli", "replay")
PAID_PROVIDERS = ("anthropic", "claude-cli")
DEFAULT_API_KEY_ENV = "ANTHROPIC_API_KEY"
SLOW_EXAMPLES_ENV = "ML_AI_SKILLS_SLOW_EXAMPLES"
REQUIRE_EXAMPLE_DEPS_ENV = "ML_AI_SKILLS_REQUIRE_EXAMPLE_DEPS"
UNMEASURED_BASELINE = {
    "status": "unmeasured",
    "reason": "No baseline is shipped with this repository and none was compared in this run. "
              "Produce one with a recorded or model run, then use --compare BASE.json NEW.json.",
}

# --------------------------------------------------------------------------
# Prompt templates (hashed into every report, so a template edit is visible as
# a changed prompt_template_sha256, not a silent score shift)
# --------------------------------------------------------------------------
PROMPT_TEMPLATE_VERSION = 1
BASE_SYSTEM_PROMPT = (
    "You are an assistant helping a user with a machine-learning or AI engineering question. "
    "Answer the user's message directly and concisely."
)
WITH_SKILL_SYSTEM_TEMPLATE = (
    BASE_SYSTEM_PROMPT + "\n\nThe following skill document is loaded for this conversation. Apply it where it "
    "is relevant to the user's message.\n\n<skill name=\"{slug}\">\n{skill_md}\n</skill>"
)


def prompt_template_sha256() -> str:
    """The answering-model templates only. The judge prompts have their own hash
    (eval_grading.judge_prompt_sha256), so a judge-prompt edit does not invalidate recorded answers."""
    blob = json.dumps({"version": PROMPT_TEMPLATE_VERSION, "base": BASE_SYSTEM_PROMPT,
                       "with_skill": WITH_SKILL_SYSTEM_TEMPLATE}, sort_keys=True)
    return el.sha256_bytes(blob.encode("utf-8"))


def system_prompt(condition: str, skill: sl.Skill) -> str:
    if condition == "with_skill":
        return WITH_SKILL_SYSTEM_TEMPLATE.format(slug=skill.slug, skill_md=skill.raw)
    return BASE_SYSTEM_PROMPT


def _sha(text: str) -> str:
    return el.sha256_bytes(text.encode("utf-8"))


def _json_sha(obj: Any) -> str:
    return _sha(json.dumps(obj, sort_keys=True, ensure_ascii=False))


# --------------------------------------------------------------------------
# Environment / provenance
# --------------------------------------------------------------------------

def git_info(root: Path) -> dict[str, Any]:
    def run(*args: str) -> str | None:
        try:
            out = subprocess.run(["git", *args], cwd=root, capture_output=True, text=True, timeout=10)
        except (OSError, subprocess.SubprocessError):
            return None
        return out.stdout.strip() if out.returncode == 0 else None
    rev = run("rev-parse", "HEAD")
    status = run("status", "--porcelain") if rev else None
    return {"git_revision": rev, "git_dirty": None if status is None else bool(status)}


def environment_info(root: Path) -> dict[str, Any]:
    return {"python_version": platform.python_version(), "python_implementation": platform.python_implementation(),
            "platform": sys.platform, **git_info(root)}


def provenance(suite: el.EvalSuite, cases: list[el.Case]) -> dict[str, Any]:
    by_slug = suite.skill_by_slug()
    entries = {}
    for c in cases:
        skill = by_slug.get(c.skill_dir)
        entries[c.uid] = {
            "path": el.rel(c.path, suite.root),
            "sha256": c.sha256,
            "fixtures": {f.name: el.sha256_file(f.path) for f in sorted(c.fixtures, key=lambda f: f.name)},
            "skill_sha256": el.sha256_bytes(skill.raw.encode("utf-8")) if skill else None,
            "category": c.category,
            "kind": c.kind,
        }
    lines = [f"{uid}\t{e['sha256']}\t{','.join(e['fixtures'].values())}" for uid, e in sorted(entries.items())]
    skills = {}
    for slug in sorted({c.skill_dir for c in cases}):
        skill = by_slug.get(slug)
        plan_path = el.evals_dir(suite.root) / slug / "plan.json"
        skills[slug] = {"sha256": el.sha256_bytes(skill.raw.encode("utf-8")) if skill else None,
                        "plan_sha256": el.sha256_file(plan_path) if plan_path.is_file() else None}
    return {"dataset_sha256": el.sha256_bytes("\n".join(lines).encode("utf-8")), "case_count": len(entries),
            "cases": entries, "skills": skills}


# Destinations never written to a report: where the report goes and the local checkout path.
_CONFIG_EXCLUDE = ("root", "out")


def run_config(args: argparse.Namespace) -> dict[str, Any]:
    """Every option of this run, as given (paths as strings). Holds environment variable *names* only:
    no secret value is ever stored in args."""
    cfg = {}
    for k, v in sorted(vars(args).items()):
        if k in _CONFIG_EXCLUDE:
            continue
        if k == "python" and v == sys.executable:
            v = "sys.executable"
        cfg[k] = str(v) if isinstance(v, Path) else ([str(x) for x in v] if isinstance(v, list) and v and
                                                      isinstance(v[0], Path) else v)
    return cfg


def _file_sha(path: Path | None) -> str | None:
    return el.sha256_file(path) if path and path.is_file() else None


def report_provenance(args: argparse.Namespace, dataset: dict, environment: dict, generated_at: str,
                      judge: eg.Judge | None, *, schema: str = REPORT_SCHEMA,
                      schema_version: int = SCHEMA_VERSION) -> dict[str, Any]:
    """The provenance block of every document run_evals writes (report, plan, calibration, run manifest)."""
    cfg = run_config(args)
    return {
        "report_schema": schema, "schema_version": schema_version, "harness_version": el.HARNESS_VERSION,
        "grader_version": eg.GRADER_VERSION, "timestamp_field": "generated_at (UTC, top level)",
        "git_revision": environment.get("git_revision"), "git_dirty": environment.get("git_dirty"),
        "dataset_sha256": dataset["dataset_sha256"],
        "skill_sha256": {s: v["sha256"] for s, v in dataset["skills"].items()},
        "case_sha256": {c: v["sha256"] for c, v in dataset["cases"].items()},
        "prompt_template_version": PROMPT_TEMPLATE_VERSION, "prompt_template_sha256": prompt_template_sha256(),
        "judge_prompt_sha256": eg.judge_prompt_sha256() if judge else None,
        "responses_sha256": _file_sha(args.responses), "rubric_labels_sha256": _file_sha(args.rubric_labels),
        "replay_file_sha256": _file_sha(args.replay_file),
        "config": cfg, "config_sha256": _json_sha(cfg),
    }


# --------------------------------------------------------------------------
# Grading + summaries
# --------------------------------------------------------------------------

def load_responses(path: Path, known: set[str]) -> tuple[list[dict], list[str]]:
    rows, errors, seen = [], [], set()
    # split("\n"), not splitlines(): --save-responses writes ensure_ascii=False, which leaves
    # U+2028/U+2029/U+0085 raw inside JSON strings, and splitlines() breaks records on them.
    # (read_text's universal-newline mode already turns CRLF into "\n".)
    for n, line in enumerate(path.read_text(encoding="utf-8").split("\n"), 1):
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError as e:
            errors.append(f"{path.name}:{n}: not valid JSON ({e})")
            continue
        fields = ("case_id", "condition", "response")
        if not isinstance(row, dict) or not all(isinstance(row.get(k), str) for k in fields):
            errors.append(f"{path.name}:{n}: each line needs string fields case_id, condition, response")
            continue
        if not CONDITION_RE.match(row["condition"]):
            errors.append(f"{path.name}:{n}: condition {row['condition']!r} must match {CONDITION_RE.pattern}")
            continue
        if row["case_id"] not in known:
            errors.append(f"{path.name}:{n}: unknown case_id {row['case_id']!r} (expected '<skill>/<case id>')")
            continue
        sample = row.get("sample", 0)
        if type(sample) is not int or sample < 0:
            errors.append(f"{path.name}:{n}: sample must be a non-negative integer")
            continue
        status = row.get("status", "ok")
        if status not in eb.COMPLETED_STATUSES:
            errors.append(f"{path.name}:{n}: status {status!r} must be one of {eb.COMPLETED_STATUSES}")
            continue
        key = (row["case_id"], row["condition"]) if sample == 0 else (row["case_id"], row["condition"], sample)
        if key in seen:
            errors.append(f"{path.name}:{n}: duplicate response for {key}")
            continue
        seen.add(key)
        rows.append({**row, "sample": sample, "status": status})
    return rows, errors


NOT_GRADED_SUBJECTIVE = {"status": "not-graded",
                         "reason": "no --judge-model configured; grade expected_behavior/must_not by review"}


def graded_entry(case: el.Case, condition: str, response: str, source: str, *, sample: int = 0,
                 status: str = "ok", rubric_labels: dict | None = None,
                 judge: dict[str, Any] | None = None, judge_configured: bool = False) -> dict[str, Any]:
    hard = eg.structural(case, response)
    rsha = _sha(response)
    rubric = eg.rubric_layer(case, rsha, labels=rubric_labels, judge=judge)
    # Whether a rubric grader was in effect for this run: then a refusal counts as "not correct"
    # in the correct layer instead of having no outcome (eval_grading.outcome_correct).
    rubric["grader_configured"] = judge_configured or rubric_labels is not None
    if status == "refusal" and rubric["status"] == "not-graded" and rubric["grader_configured"]:
        rubric["reason"] = "refusal: no answer to grade; counted as not correct in the correct layer"
    return {"case_id": case.uid, "condition": condition, "sample": sample, "source": source, "status": status,
            "response": response, "response_sha256": rsha, "hard": hard,
            "subjective": judge if judge is not None else dict(NOT_GRADED_SUBJECTIVE),
            "rubric": rubric, "verdict": eg.combine(status, hard, rubric)}


def failed_entry(case: el.Case, condition: str, sample: int, completion: eb.Completion, source: str) -> dict:
    hard = {"gradable": case.has_hard_assertions, "passed": None, "assertions": []}
    rubric = {"status": "not-graded", "source": None, "items": [],
              "reason": f"no answer (attempt status {completion.status})"}
    return {"case_id": case.uid, "condition": condition, "sample": sample, "source": source,
            "status": completion.status, "error": completion.error, "hard": hard,
            "subjective": {"status": "not-graded", "reason": "the model call did not produce an answer"},
            "rubric": rubric, "verdict": eg.combine(completion.status, hard, rubric)}


def _rate(passed: int, total: int) -> float | None:
    return round(passed / total, 6) if total else None


def summarize(graded: list[dict]) -> dict[str, Any]:
    hard: dict[str, dict] = {}
    subjective: dict[str, dict] = {}
    for g in graded:
        h = hard.setdefault(g["condition"], {"responses": 0, "gradable": 0, "passed": 0, "failed": 0,
                                             "errors": 0, "refusals": 0, "not_gradable": 0})
        h["responses"] += 1
        if g["status"] not in eb.COMPLETED_STATUSES:
            h["errors"] += 1
        elif not g["hard"]["gradable"]:
            h["not_gradable"] += 1
        else:
            h["gradable"] += 1
            h["passed" if g["hard"]["passed"] and g["status"] == "ok" else "failed"] += 1
        if g["status"] == "refusal":
            h["refusals"] += 1
        s = subjective.setdefault(g["condition"], {"graded": 0, "rubric_passed": 0, "not_graded": 0,
                                                   "judge_errors": 0})
        st = g["subjective"]["status"]
        if st == "graded":
            s["graded"] += 1
            s["rubric_passed"] += bool(g["subjective"]["rubric_pass"])
        elif st == "not-graded":
            s["not_graded"] += 1
        else:
            s["judge_errors"] += 1
    for h in hard.values():
        h["pass_rate"] = _rate(h["passed"], h["gradable"])
    for s in subjective.values():
        s["rubric_pass_rate"] = _rate(s["rubric_passed"], s["graded"])
    return {"hard": dict(sorted(hard.items())), "subjective": dict(sorted(subjective.items())),
            "final": eg.summarize_final(graded)}


def skill_uplift(graded: list[dict]) -> dict[str, Any]:
    """with_skill vs without_skill hard (structural) pass rate on cases complete in both arms;
    never estimated from unpaired data. See `uplift` in the report for the other layers."""
    u = eg.paired_uplift(graded, "structural")
    if u["status"] != "measured":
        return {"status": "unmeasured", "incomplete_cases": u["incomplete_cases"],
                "reason": "no case has hard-graded responses under both with_skill and without_skill"}
    return {"status": "measured", "layer": "structural (regex hard assertions; not a correctness measure)",
            "paired_cases": u["paired_cases"], "samples_per_arm": u["samples_per_arm"],
            "with_skill_pass_rate": u["with_skill_rate"]["rate"],
            "without_skill_pass_rate": u["without_skill_rate"]["rate"],
            "with_skill_ci95": u["with_skill_rate"]["ci95"], "without_skill_ci95": u["without_skill_rate"]["ci95"],
            "delta": u["delta"], "only_with_skill_passed": u["cases_with_better"],
            "only_without_skill_passed": u["cases_without_better"], "incomplete_cases": u["incomplete_cases"],
            "note": "Pass-rate difference on this small, hand-written case set; not a statistically powered "
                    "estimate and no significance is claimed. Model sampling is not deterministic, so repeat "
                    "runs can differ."}


# --------------------------------------------------------------------------
# Executable tests
# --------------------------------------------------------------------------

_SUMMARY_RE = re.compile(r"^(?:OK|FAILED)\b.*$", re.MULTILINE)
SKIPPED_LABEL = "skipped (not evidence)"
EXEC_OUTPUT_TAIL_CHARS = 4000


def run_executable(cases: list[el.Case], root: Path, python: str, timeout: float, *,
                   env_overrides: dict[str, str] | None = None) -> dict[str, Any]:
    tests: dict[str, list[str]] = {}
    skipped: list[dict] = []
    for c in cases:
        if c.kind != "executable":
            continue
        blocked = [r for r in c.str_list("requires") if r not in ("example-deps",)]
        for t in c.str_list("executable_tests"):
            if blocked:
                skipped.append({"test_id": t, "cases": [c.uid], "status": "skipped", "executed": False,
                                "status_label": SKIPPED_LABEL,
                                "detail": f"case requires {', '.join(blocked)}; not run by the offline harness"})
            else:
                tests.setdefault(t, []).append(c.uid)
    env = {**os.environ, **(env_overrides or {})}
    results = []
    for test_id, uids in sorted(tests.items()):
        out = ""
        try:
            proc = subprocess.run([python, "-m", "unittest", "-v", test_id], cwd=root / "tests",
                                  capture_output=True, text=True, encoding="utf-8", errors="replace",
                                  timeout=timeout, env=env)
            out = proc.stderr + proc.stdout
            summary = _SUMMARY_RE.findall(out)
            detail = summary[-1].strip() if summary else f"exit code {proc.returncode}"
            skip = re.search(r"\.\.\. skipped '(.*)'", out)
            if proc.returncode != 0:
                status = "failed"
            elif skip or "skipped=" in detail:
                # A skipped example test proves nothing: report it, never count it as passed.
                status = "skipped"
                detail = f"{detail}: {skip.group(1)}" if skip else detail
            else:
                status = "passed"
        except subprocess.TimeoutExpired:
            status, detail = "failed", f"timed out after {timeout:g}s"
        entry = {"test_id": test_id, "cases": sorted(uids), "status": status, "executed": status != "skipped",
                 "detail": detail}
        if status == "skipped":
            entry["status_label"] = SKIPPED_LABEL
        elif status == "failed" and out:
            # The traceback is the evidence for a failure; keep its tail (redacted with the report).
            entry["output_tail"] = out[-EXEC_OUTPUT_TAIL_CHARS:]
        results.append(entry)
    results += skipped
    results.sort(key=lambda r: (r["test_id"], r["cases"]))
    counts = {s: sum(r["status"] == s for r in results) for s in ("passed", "failed", "skipped")}
    return {"status": "run", "python": "sys.executable" if python == sys.executable else python,
            "env_overrides": dict(sorted((env_overrides or {}).items())),
            "evidence_note": "only 'passed' and 'failed' are evidence; a skipped test ran nothing",
            "results": results, "counts": counts}


# --------------------------------------------------------------------------
# Model mode: configuration checks
# --------------------------------------------------------------------------

# Options that configure the answering model. Any of them with a non-default value puts the run
# on the model-mode path, so a forgotten --provider/--model is refused with the missing-flag list
# instead of silently running offline and ignoring them.
ANSWER_DESTS = ("provider", "model", "conditions", "temperature", "max_tokens", "save_responses", "samples",
                "price_input_per_mtok", "price_output_per_mtok", "replay_file", "run_dir", "resume")
# Options that configure the judge. They need responses to grade: model mode, --responses or --grade-*.
JUDGE_DESTS = ("judge_provider", "judge_model", "judge_max_tokens", "judge_temperature", "judge_price_input_per_mtok",
               "judge_price_output_per_mtok", "judge_replay_file")
# Options shared by the answering model and the judge (so a judge-only run can --dry-run and has the same
# preflight). Given alone, they mean model mode.
SHARED_DESTS = ("allow_network", "max_cost_usd", "timeout", "max_retries", "backoff_base", "backoff_cap", "endpoint",
                "anthropic_version", "api_key_env", "claude_cli_path", "claude_cli_arg", "no_cli_budget_flag",
                "dry_run", "allow_partial_budget")
MODEL_ONLY_DESTS = ANSWER_DESTS + JUDGE_DESTS + SHARED_DESTS


def _flags_given(args: argparse.Namespace, dests: tuple[str, ...]) -> list[str]:
    defaults = vars(build_parser().parse_args([]))  # post-`type` defaults (e.g. --conditions as a list)
    return ["--" + d.replace("_", "-") for d in dests if getattr(args, d) != defaults[d]]


def model_flags_given(args: argparse.Namespace) -> list[str]:
    """Model-only options set to a non-default value (comparison, not argv scanning, so
    `--conditions=x` and abbreviations are detected too)."""
    return _flags_given(args, MODEL_ONLY_DESTS)


def _paid_problems(provider: str, model: str | None, prices: tuple, args: argparse.Namespace, env: dict, *,
                   role: str, dry_run: bool) -> list[str]:
    missing = []
    flag = "--model" if role == "answer" else "--judge-model"
    if not model:
        missing.append(f"{flag} <model id>")
    if dry_run:
        return missing
    if not args.allow_network:
        missing.append("--allow-network")
    if args.max_cost_usd is None:
        missing.append("--max-cost-usd <ceiling in USD> (paid providers never run without one)")
    p = "--price" if role == "answer" else "--judge-price"
    if provider == "anthropic":
        key = env.get(args.api_key_env)
        if not key:
            missing.append(f"{args.api_key_env} environment variable")
        elif key != key.strip() or any(ord(ch) < 0x20 or ord(ch) == 0x7F or ord(ch) > 0xFF for ch in key):
            # http.client would reject it with an error that quotes the key; name the variable only.
            missing.append(f"{args.api_key_env} without surrounding whitespace or control/non-latin-1 characters "
                           "(the value is not shown)")
        if None in prices:
            missing.append(f"{p}-input-per-mtok and {p}-output-per-mtok (USD per million tokens, from your provider's "
                           "current price list; none is built in)")
    if provider == "claude-cli" and args.no_cli_budget_flag and None in prices:
        # Without --max-budget-usd nothing caps a single CLI call (the CLI ignores --max-tokens, so
        # prices give no per-call bound either). Prices are still required so a call is charged even
        # when the CLI reports no cost; the ceiling is then checked between calls only.
        missing.append(f"{p}-input-per-mtok and {p}-output-per-mtok (required with --no-cli-budget-flag)")
    return missing


def model_preconditions(args: argparse.Namespace, env: dict) -> list[str]:
    """Every missing requirement for answering-model mode; empty list means allowed."""
    if args.provider is None:
        return [f"--provider ({' | '.join(PROVIDERS)})"] + ([] if args.model else ["--model <model id>"])
    if args.provider == "replay":
        return [] if args.replay_file else ["--replay-file FILE.jsonl (provider replay)"]
    return _paid_problems(args.provider, args.model, (args.price_input_per_mtok, args.price_output_per_mtok),
                          args, env, role="answer", dry_run=args.dry_run)


def judge_prices(args: argparse.Namespace) -> tuple:
    prices = (args.judge_price_input_per_mtok, args.judge_price_output_per_mtok)
    if None in prices and (args.judge_provider or args.provider) == args.provider and args.judge_model == args.model:
        prices = (args.price_input_per_mtok, args.price_output_per_mtok)  # same model: same prices
    return prices


def judge_preconditions(args: argparse.Namespace, env: dict) -> list[str]:
    provider = args.judge_provider or args.provider
    if provider is None:
        return [f"--judge-provider ({' | '.join(PROVIDERS)})"]
    if provider == "replay":
        return [] if args.judge_replay_file else ["--judge-replay-file FILE.jsonl (judge provider replay)"]
    return _paid_problems(provider, args.judge_model, judge_prices(args), args, env, role="judge",
                          dry_run=args.dry_run)


def usage_problems(args: argparse.Namespace) -> list[str]:
    p = []
    if args.samples < 1:
        p.append("--samples must be >= 1")
    for name in ("max_tokens", "judge_max_tokens"):
        if getattr(args, name) < 1:
            p.append(f"--{name.replace('_', '-')} must be >= 1")
    for name in ("timeout", "executable_timeout"):
        if not getattr(args, name) > 0:
            p.append(f"--{name.replace('_', '-')} must be > 0")
    if args.max_retries < 0 or args.backoff_base < 0 or args.backoff_cap < 0:
        p.append("--max-retries, --backoff-base and --backoff-cap must be >= 0")
    if args.max_cost_usd is not None and not args.max_cost_usd > 0:
        p.append("--max-cost-usd must be > 0")
    for name in ("price_input_per_mtok", "price_output_per_mtok", "judge_price_input_per_mtok",
                 "judge_price_output_per_mtok", "temperature", "judge_temperature"):
        v = getattr(args, name)
        if v is not None and v < 0:
            p.append(f"--{name.replace('_', '-')} must be >= 0")
    if not args.endpoint.startswith("https://"):
        p.append("--endpoint must be an https:// URL")
    if args.resume and not args.run_dir:
        p.append("--resume needs --run-dir")
    exec_only = [f for f, on in (("--require-executed", args.require_executed), ("--slow-examples", args.slow_examples),
                                 ("--require-example-deps", args.require_example_deps)) if on]
    if exec_only and not args.run_executable:
        p.append(f"{', '.join(exec_only)} need(s) --run-executable (without it nothing is executed)")
    return p


# --------------------------------------------------------------------------
# Model mode: backends, run directory, the run itself
# --------------------------------------------------------------------------

BackendFactory = Callable[[str, argparse.Namespace], eb.Backend]


def make_backend(role: str, args: argparse.Namespace, env: dict, *, transport: Transport | None,
                 sleep: Callable[[float], None], cli_runner=None) -> eb.Backend:
    provider = args.provider if role == "answer" else (args.judge_provider or args.provider)
    model = args.model if role == "answer" else args.judge_model
    prices = (args.price_input_per_mtok, args.price_output_per_mtok) if role == "answer" else judge_prices(args)
    pricing = eb.Pricing(*prices) if None not in prices else None
    if provider == "replay":
        path = args.replay_file if role == "answer" else args.judge_replay_file
        rows, _ = eb.load_replay(path)  # errors were reported before the run
        return eb.ReplayBackend(rows, model=model or f"replay:{path.name}", source_sha256=el.sha256_file(path))
    if provider == "claude-cli":
        kw = {"runner": cli_runner} if cli_runner else {}
        return eb.ClaudeCliBackend(model, command=[args.claude_cli_path], timeout=args.timeout,
                                   extra_args=tuple(args.claude_cli_arg or ()), pricing=pricing,
                                   budget_flag=not args.no_cli_budget_flag, **kw)
    # .get: a --dry-run builds the backend without a key and never calls it.
    client = eb.MessagesClient(env.get(args.api_key_env, ""), anthropic_version=args.anthropic_version, timeout=args.timeout,
                               max_retries=args.max_retries, backoff_base=args.backoff_base,
                               backoff_cap=args.backoff_cap, transport=transport, sleep=sleep, url=args.endpoint)
    return eb.AnthropicBackend(client, model, pricing, api_key_env=args.api_key_env)


class RunStore:
    """A run directory: `manifest.json` (provenance of the first run) and append-only
    `records.jsonl`, one line per answer or judge attempt.

    `--resume` reuses a recorded attempt only when its key (case, condition,
    sample) AND its provenance hash (everything sent to the model plus the
    model configuration) match, and only if it completed (answer: ok/refusal;
    judge: graded/unparseable). Failures, timeouts, rate limits and
    budget-exhausted attempts are attempted again. A truncated last line (a
    crash mid-write) is skipped and counted.
    """

    def __init__(self, path: Path, secrets: list[str]):
        self.path = path
        self.records_path = path / "records.jsonl"
        self.secrets = secrets
        self.index: dict[tuple[str, str], list[dict]] = {}
        self.stats = {"reused": 0, "stale_ignored": 0, "reattempted": 0, "corrupt_lines": 0, "recorded": 0}

    def open(self, *, resume: bool, manifest: dict) -> str | None:
        has_records = self.records_path.is_file() and self.records_path.stat().st_size > 0
        if has_records and not resume:
            return f"run directory {self.path} already has records; pass --resume, or use a new --run-dir"
        if resume and not has_records:
            return f"--resume: {self.records_path} does not exist or is empty"
        self.path.mkdir(parents=True, exist_ok=True)
        if has_records:
            for line in el.read_text_lf(self.records_path).split("\n"):
                if not line.strip():
                    continue
                try:
                    rec = json.loads(line)
                    self.index.setdefault((rec["type"], rec["key"]), []).append(rec)
                except (json.JSONDecodeError, KeyError, TypeError):
                    self.stats["corrupt_lines"] += 1
            stamp = manifest["created_at"].replace(":", "").replace("+", "Z")
            self._write_json(self.path / f"manifest.resume-{stamp}.json", manifest)
        else:
            self._write_json(self.path / "manifest.json", manifest)
        return None

    def _write_json(self, p: Path, doc: dict) -> None:
        p.write_text(eb.redact(dump(doc), self.secrets), encoding="utf-8", newline="\n")

    def lookup(self, kind: str, key: str, prov: str, done: Callable[[dict], bool]) -> dict | None:
        recs = self.index.get((kind, key), [])
        match = [r for r in recs if r.get("provenance_sha256") == prov]
        if recs and not match:
            self.stats["stale_ignored"] += 1
        for r in reversed(match):
            if done(r):
                self.stats["reused"] += 1
                return r
        if match:
            self.stats["reattempted"] += 1
        return None

    def append(self, kind: str, key: str, prov: str, payload: dict, now: str) -> None:
        rec = {"type": kind, "key": key, "provenance_sha256": prov, "recorded_at": now, **payload}
        line = eb.redact(json.dumps(rec, sort_keys=True, ensure_ascii=False), self.secrets)
        with self.records_path.open("a", encoding="utf-8", newline="\n") as f:
            f.write(line + "\n")
            f.flush()
            os.fsync(f.fileno())
        self.stats["recorded"] += 1


def _answer_done(rec: dict) -> bool:
    return (rec.get("completion") or {}).get("status") in eb.COMPLETED_STATUSES


def _judge_done(rec: dict) -> bool:
    return (rec.get("judge") or {}).get("status") in ("graded", "unparseable")


class ModelRun:
    """Drives the answer (and optional judge) calls with a shared budget and optional run store."""

    def __init__(self, backend: eb.Backend | None, judge: eg.Judge | None, budget: eb.Budget,
                 store: RunStore | None, log: Callable[[str], None], now: Callable[[], str]):
        self.backend = backend
        self.judge = judge
        self.budget = budget
        self.store = store
        self.log = log
        self.now = now

    def _call(self, backend: eb.Backend, req: eb.Request) -> eb.Completion:
        worst = backend.worst_case_cost(req)
        if not self.budget.can_afford(worst):
            why = self.budget.stop_reason or (
                f"the cost ceiling (${self.budget.ceiling}) would be exceeded (spent ~${self.budget.spent:.4f}, "
                f"this call up to {'unknown' if worst is None else f'${worst:.4f}'})")
            return eb.Completion(status="budget-exhausted", attempts=0, error=f"not sent: {why}")
        if isinstance(backend, eb.ClaudeCliBackend) and self.budget.ceiling is not None:
            backend.max_budget_for_next = self.budget.ceiling - self.budget.spent
        c = backend.complete(req)
        self.budget.charge(self.charged_cost(backend, c, worst), paid=backend.paid)
        return c

    @staticmethod
    def charged_cost(backend: eb.Backend, c: eb.Completion, worst: float | None) -> float | None:
        """What one call is charged against the ceiling (pessimistic where unknown).

        Observed cost (usage x prices, or the CLI's reported cost) when there is one, plus one worst
        case per unconfirmed HTTP attempt (a timeout or dropped connection that may have been billed).
        Without an observed cost a paid call is charged its worst case per attempt that may have been
        billed. None means the cost is unknown; Budget.charge then stops a paid run."""
        meta = c.meta if isinstance(c.meta, dict) else {}
        if meta.get("sent") is False:
            return 0.0  # never left this machine (e.g. the CLI could not be started)
        cost = backend.observed_cost(c)
        if not backend.paid:
            return cost
        unconfirmed = meta.get("unconfirmed_attempts")
        unconfirmed = unconfirmed if isinstance(unconfirmed, int) and unconfirmed > 0 else 0
        if cost is None:
            return None if worst is None else worst * max(1, unconfirmed)
        return cost + (worst or 0.0) * unconfirmed

    def answer(self, req: eb.Request, prov: str, extra: dict) -> eb.Completion:
        key = f"{req.case_id}|{req.condition}|{req.sample}"
        if self.store:
            rec = self.store.lookup("answer", key, prov, _answer_done)
            if rec:
                return eb.Completion.from_dict(rec["completion"])
        self.log(f"  {req.case_id} [{req.condition}] sample {req.sample}")
        c = self._call(self.backend, req)
        if self.store:
            self.store.append("answer", key, prov, {"completion": c.to_dict(), **extra}, self.now())
        return c

    def grade(self, case: el.Case, response: str, condition: str, sample: int, answer_prov: str) -> dict:
        req = self.judge.request(case, response, condition, sample)
        prov = _json_sha({"answer": answer_prov, "response_sha256": _sha(response), "judge": self.judge.identity(),
                          "rubric": [case.str_list("expected_behavior"), case.str_list("must_not")],
                          "input_sha256": _sha(case.data.get("input", ""))})
        key = f"{case.uid}|{condition}|{sample}"
        if self.store:
            rec = self.store.lookup("judge", key, prov, _judge_done)
            if rec:
                return rec["judge"]
        c = self._call(self.judge.backend, req)
        result = eg.parse_judge(case, c, self.judge.backend.model)
        if self.store:
            self.store.append("judge", key, prov, {"judge": result, "completion_status": c.status}, self.now())
        return result


def answer_provenance(req: eb.Request, backend: eb.Backend) -> str:
    """Everything sent to the model plus the model configuration (not the grading: answers are
    re-graded on every run, so editing a case's regexes does not re-buy its answers)."""
    return _json_sha({"system_sha256": _sha(req.system), "user_sha256": _sha(req.user), "max_tokens": req.max_tokens,
                      "temperature": req.temperature, "condition": req.condition, "sample": req.sample,
                      "backend": backend.identity(), "prompt_template_sha256": prompt_template_sha256()})


def plan_requests(cases: list[el.Case], suite: el.EvalSuite, args: argparse.Namespace) -> list[eb.Request]:
    by_slug = suite.skill_by_slug()
    reqs = []
    # Arms are interleaved per case and sample, so a stop (budget, Ctrl-C) affects both arms alike.
    for case in cases:
        skill = by_slug[case.skill_dir]
        for sample in range(args.samples):
            for condition in args.conditions:
                reqs.append(eb.Request(system=system_prompt(condition, skill), user=case.data.get("input", ""),
                                       max_tokens=args.max_tokens, temperature=args.temperature,
                                       case_id=case.uid, condition=condition, sample=sample))
    return reqs


def worst_case_total(reqs: list[eb.Request], backend: eb.Backend | None, judge: eg.Judge | None,
                     cases: dict[str, el.Case]) -> float | None:
    total = 0.0
    for r in reqs:
        if backend is not None:
            w = backend.worst_case_cost(r)
            if w is None and backend.paid:
                return None
            total += w or 0.0
        if judge is not None:
            jr = judge.request(cases[r.case_id], "x" * (4 * r.max_tokens), r.condition, r.sample)
            w = judge.backend.worst_case_cost(jr)
            if w is None and judge.backend.paid:
                return None
            total += w or 0.0
    return round(total, 6)


def judge_worst_case(judge: eg.Judge, pairs: list[tuple[el.Case, str]]) -> float | None:
    """Worst case of one judge call per (case, answer) in a judge-only run; None if unpriced and paid."""
    total = 0.0
    for case, text in pairs:
        w = judge.backend.worst_case_cost(judge.request(case, text, labelled_condition(text)))
        if w is None and judge.backend.paid:
            return None
        total += w or 0.0
    return round(total, 6)


ESTIMATE_NOTE = "worst case: every call uses its max_tokens and input is ~1 token per 2 bytes"


def _money(v: float | None, none: str) -> str:
    return none if v is None else f"${v:.4f}"


def over_ceiling(args: argparse.Namespace, paid: bool, worst: float | None, log: Callable[[str], None]) -> bool:
    """Log and return True when a priced worst case exceeds --max-cost-usd (and --allow-partial-budget is off)."""
    if not (paid and worst is not None and args.max_cost_usd is not None and worst > args.max_cost_usd
            and not args.allow_partial_budget):
        return False
    log(f"run_evals: not-run: the worst-case estimate ${worst:.4f} exceeds --max-cost-usd "
        f"${args.max_cost_usd:.4f}. Lower --max-tokens/--judge-max-tokens/--samples, select fewer cases, raise the "
        "ceiling, or pass --allow-partial-budget (unaffordable attempts are then recorded as budget-exhausted).")
    return True


def plan_doc(args: argparse.Namespace, suite: el.EvalSuite, cases: list[el.Case], preflight: dict,
             backend: eb.Backend | None, judge: eg.Judge | None, environment: dict, generated_at: str) -> dict:
    return {"schema": PLAN_SCHEMA, "schema_version": 1, "status": "not-run", "reason": "dry run",
            "generated_at": generated_at, "preflight": preflight,
            "backend": backend.describe() if backend else None, "judge": judge.describe() if judge else None,
            "cases": [c.uid for c in cases], "conditions": list(args.conditions) if backend else None,
            "samples": args.samples if backend else None,
            "provenance": report_provenance(args, provenance(suite, cases), environment, generated_at, judge,
                                            schema=PLAN_SCHEMA, schema_version=1)}


def run_model(cases: list[el.Case], suite: el.EvalSuite, args: argparse.Namespace, runner: ModelRun,
              rubric_labels: dict | None) -> list[dict]:
    by_uid = {c.uid: c for c in cases}
    by_slug = suite.skill_by_slug()
    graded = []
    for req in plan_requests(cases, suite, args):
        case = by_uid[req.case_id]
        prov = answer_provenance(req, runner.backend)
        extra = {"case_sha256": case.sha256, "skill_sha256": _sha(by_slug[case.skill_dir].raw)}
        c = runner.answer(req, prov, extra)
        if c.status not in eb.COMPLETED_STATUSES:
            entry = failed_entry(case, req.condition, req.sample, c, "model")
        else:
            judge = (runner.grade(case, c.text, req.condition, req.sample, prov)
                     if runner.judge and c.status == "ok" else None)
            entry = graded_entry(case, req.condition, c.text, "model", sample=req.sample, status=c.status,
                                 rubric_labels=rubric_labels, judge=judge, judge_configured=runner.judge is not None)
        entry.update({"stop_reason": c.stop_reason, "model_reported": c.model_reported, "usage": c.usage,
                      "cost_usd": c.cost_usd, "attempts": c.attempts})
        graded.append(entry)
    return graded


def model_meta(args: argparse.Namespace, runner: ModelRun, store: RunStore | None, preflight: dict) -> dict:
    b = runner.backend
    client = getattr(b, "client", None)
    judge = runner.judge
    return {"provider": args.provider, "model": b.model, "judge_model": judge.backend.model if judge else None,
            "endpoint": client.url if client else None,
            "anthropic_version": client.anthropic_version if client else None,
            "conditions": list(args.conditions), "samples": args.samples, "max_tokens": args.max_tokens,
            "temperature": args.temperature,
            "temperature_note": ("sent explicitly" if args.temperature is not None else
                                 "not sent; the provider's default sampling applies"),
            "prompt_template_version": PROMPT_TEMPLATE_VERSION, "prompt_template_sha256": prompt_template_sha256(),
            "timeout_s": args.timeout, "max_retries": args.max_retries,
            "http_attempts": client.attempts if client else None,
            "http_client": f"urllib (Python {platform.python_version()} stdlib)" if client else None,
            "backend": b.describe(), "judge": judge.describe() if judge else None,
            "judge_same_model_as_answerer": (bool(judge) and judge.backend.provider == b.provider
                                             and judge.backend.model == b.model),
            "budget": runner.budget.to_dict(), "preflight": preflight,
            "run_dir": str(store.path) if store else None, "resume": store.stats if store else None,
            "call_order": "per case, per sample, both conditions in turn (a stop affects both arms alike)"}


def judge_only_meta(args: argparse.Namespace, judge: eg.Judge, budget: eb.Budget) -> dict:
    return {"provider": None, "model": None, "judge_model": judge.backend.model, "judge": judge.describe(),
            "budget": budget.to_dict(), "note": "judge-only run over recorded responses"}


# --------------------------------------------------------------------------
# Report
# --------------------------------------------------------------------------

def select_cases(cases: list[el.Case], skills: list[str] | None, ids: list[str] | None) -> list[el.Case]:
    out = [c for c in cases if (not skills or c.skill_dir in skills) and (not ids or c.uid in ids or c.id in ids)]
    return sorted(out, key=lambda c: c.uid)


def _gkey(g: dict) -> tuple[str, str, int]:
    return (g["case_id"], g["condition"], g.get("sample") or 0)


def model_evaluation_status(mode: str, graded: list[dict], model_run: dict | None) -> dict[str, Any]:
    if mode != "model":
        return {"status": "not-run", "reason": "no model configured (offline run); see docs/EVALUATION.md"}
    by_status: dict[str, int] = {}
    for g in graded:
        by_status[g["status"]] = by_status.get(g["status"], 0) + 1
    failures = sum(n for s, n in by_status.items() if s in eb.FAILURE_STATUSES)
    return {"status": "run", "complete": failures == 0, "attempts": len(graded), "by_status": dict(sorted(by_status.items())),
            "provider": (model_run or {}).get("provider"), "model": (model_run or {}).get("model")}


def build_report(suite: el.EvalSuite, cases: list[el.Case], *, mode: str, graded: list[dict],
                 input_errors: list[str], executable: dict[str, Any] | None, model_run: dict[str, Any] | None,
                 environment: dict[str, Any], generated_at: str,
                 report_prov: dict[str, Any] | None = None) -> dict[str, Any]:
    uids = {c.uid for c in cases}
    fixtures = [r for r in suite.fixture_results if r["case_id"] in uids]
    graded = sorted(graded, key=_gkey)
    graded_ids = {(g["case_id"]) for g in graded}
    skipped = []
    for c in cases:
        if not c.has_hard_assertions:
            skipped.append({"case_id": c.uid, "what": "hard assertions", "reason": "rubric-only case"})
        if graded and c.uid not in graded_ids:
            skipped.append({"case_id": c.uid, "what": "response grading", "reason": "no response supplied"})
    if executable is None and any(c.kind == "executable" for c in cases):
        executable = {"status": "not-run", "reason": "pass --run-executable (needs the example-test dependencies)"}
    dataset = provenance(suite, cases)
    return {
        "schema": REPORT_SCHEMA,
        "schema_version": SCHEMA_VERSION,
        "harness_version": el.HARNESS_VERSION,
        "grader_version": eg.GRADER_VERSION,
        "generated_at": generated_at,
        "nondeterministic_fields": list(NONDETERMINISTIC_FIELDS),
        "mode": mode,
        "environment": environment,
        "dependencies": {"python": environment.get("python_version"), "harness": el.HARNESS_VERSION,
                         "third_party": "none (stdlib only)"},
        "provenance": report_prov,
        "dataset": dataset,
        "validation": {"ok": not suite.errors, "errors": suite.errors, "warnings": suite.warnings},
        "input_errors": input_errors,
        "fixture_self_tests": {"total": len(fixtures), "ok": sum(r["ok"] for r in fixtures),
                               "failed": sum(not r["ok"] for r in fixtures), "results": fixtures},
        "graded": graded,
        "executable": executable,
        "skipped": skipped,
        "summary": summarize(graded),
        "skill_uplift": skill_uplift(graded),
        "uplift": {layer: eg.paired_uplift(graded, layer) for layer in eg.LAYER_OUTCOMES},
        "verdict_rules": list(eg.VERDICT_RULES),
        "baseline": dict(UNMEASURED_BASELINE),
        "model_evaluation": model_evaluation_status(mode, graded, model_run),
        "model_run": model_run,
        "measurement_notes": [
            "Layers are reported separately (hard = regex assertions, rubric = per-item verdicts from labels or "
            "a judge, subjective = raw judge output) and combined only by verdict_rules.",
            "A regex-only pass is 'structural-pass', never 'correct': hard assertions check concept presence, "
            "not correctness of reasoning.",
            "The judge, if any, is uncalibrated against human labels until a human-labelled sample exists "
            "(run_evals.py --grade-labelled).",
            "Rates carry Wilson 95% intervals; they are descriptive and no significance is claimed.",
        ],
    }


def dump(doc: dict) -> str:
    return json.dumps(doc, indent=2, sort_keys=True, ensure_ascii=False) + "\n"


# --------------------------------------------------------------------------
# Compare
# --------------------------------------------------------------------------

def _key_str(g: dict) -> tuple[str, str]:
    s = g.get("sample") or 0
    return (g["case_id"], g["condition"] if not s else f"{g['condition']}#{s}")


def _index(report: dict) -> dict[tuple[str, str], dict]:
    return {_key_str(g): g for g in report.get("graded") or []}


def compare(base: dict, new: dict) -> dict[str, Any]:
    regressions, improvements = [], []
    b_idx, n_idx = _index(base), _index(new)
    b_cases = (base.get("dataset") or {}).get("cases") or {}
    n_cases = (new.get("dataset") or {}).get("cases") or {}
    changed = sorted(c for c in set(b_cases) & set(n_cases) if b_cases[c]["sha256"] != n_cases[c]["sha256"])
    # A gate must not be passable by deleting or weakening what used to pass:
    # a passing response missing from NEW, a case whose hard assertions were
    # removed, and a passing assertion that no longer exists are regressions too.
    for key in sorted(set(b_idx) - set(n_idx)):
        b = b_idx[key]
        if b["status"] == "ok" and b["hard"]["gradable"] and b["hard"]["passed"]:
            regressions.append({"case_id": key[0], "condition": key[1], "case_changed": key[0] in changed,
                                "assertion": None, "base": "pass", "new": "missing"})
    for key in sorted(set(b_idx) & set(n_idx)):
        b, n = b_idx[key], n_idx[key]
        meta = {"case_id": key[0], "condition": key[1], "case_changed": key[0] in changed}
        if b["status"] == "ok" and n["status"] != "ok":
            regressions.append({**meta, "assertion": None, "base": "graded", "new": n["status"]})
            continue
        if b["status"] != "ok" or n["status"] != "ok" or not b["hard"]["gradable"]:
            continue
        if not n["hard"]["gradable"]:
            regressions.append({**meta, "assertion": None, "base": "pass" if b["hard"]["passed"] else "fail",
                                "new": "not-gradable"})
            continue
        b_as = {a["id"]: a for a in b["hard"]["assertions"]}
        n_as = {a["id"]: a for a in n["hard"]["assertions"]}
        for aid in sorted(set(b_as) - set(n_as)):
            if b_as[aid]["passed"]:
                regressions.append({**meta, "assertion": aid, "pattern": b_as[aid]["pattern"],
                                    "base": "pass", "new": "missing"})
        for aid in sorted(set(b_as) & set(n_as)):
            if b_as[aid]["passed"] and not n_as[aid]["passed"]:
                regressions.append({**meta, "assertion": aid, "pattern": n_as[aid]["pattern"],
                                    "base": "pass", "new": "fail"})
            elif not b_as[aid]["passed"] and n_as[aid]["passed"]:
                improvements.append({**meta, "assertion": aid, "base": "fail", "new": "pass"})
        if b["hard"]["passed"] and not n["hard"]["passed"] and not any(
                r["case_id"] == key[0] and r["condition"] == key[1] for r in regressions):
            regressions.append({**meta, "assertion": None, "base": "pass", "new": "fail"})
    b_fx = {(r["case_id"], r["fixture"]): r for r in (base.get("fixture_self_tests") or {}).get("results", [])}
    n_fx = {(r["case_id"], r["fixture"]): r for r in (new.get("fixture_self_tests") or {}).get("results", [])}
    fixture_regressions = [{"case_id": k[0], "fixture": k[1], "new": "missing" if k not in n_fx else "fail"}
                           for k in sorted(b_fx) if b_fx[k]["ok"] and (k not in n_fx or not n_fx[k]["ok"])]
    # NEW must itself be a sound measurement: an invalid suite or bad response input
    # cannot certify "no regressions" just because nothing comparable was graded.
    new_problems = []
    n_val = new.get("validation") or {}
    if n_val.get("ok") is not True:
        new_problems.append(f"NEW report's eval suite failed validation ({len(n_val.get('errors') or [])} error(s))")
    if new.get("input_errors"):
        new_problems.append(f"NEW report has {len(new['input_errors'])} response input error(s)")
    b_up, n_up = base.get("skill_uplift") or {}, new.get("skill_uplift") or {}
    delta_change = (round(n_up["delta"] - b_up["delta"], 6)
                    if b_up.get("status") == "measured" and n_up.get("status") == "measured" else None)

    def side(r: dict) -> dict:
        return {"harness_version": r.get("harness_version"), "schema_version": r.get("schema_version"),
                "generated_at": r.get("generated_at"),
                "git_revision": (r.get("environment") or {}).get("git_revision"),
                "dataset_sha256": (r.get("dataset") or {}).get("dataset_sha256"),
                "model": (r.get("model_run") or {}).get("model"), "graded": len(r.get("graded") or [])}

    has_base = bool(b_idx)
    return {
        "schema": COMPARE_SCHEMA, "schema_version": SCHEMA_VERSION, "harness_version": el.HARNESS_VERSION,
        "base": side(base), "new": side(new),
        "baseline": ({"status": "measured", "reason": "base report contains graded responses"} if has_base else
                     {"status": "unmeasured", "reason": "base report contains no graded responses; only "
                      "fixture self-tests can be compared"}),
        "regressions": regressions, "improvements": improvements, "fixture_regressions": fixture_regressions,
        "new_report_problems": new_problems,
        "missing_in_new": [f"{c}|{k}" for c, k in sorted(set(b_idx) - set(n_idx))],
        "added_in_new": [f"{c}|{k}" for c, k in sorted(set(n_idx) - set(b_idx))],
        "changed_cases": changed,
        "skill_uplift": {"base": b_up or None, "new": n_up or None, "delta_change": delta_change},
        "subjective": {"note": "LLM-judge results are shown for context only and never gate regressions.",
                       "base": (base.get("summary") or {}).get("subjective"),
                       "new": (new.get("summary") or {}).get("subjective")},
        "summary": {"regressions": len(regressions), "fixture_regressions": len(fixture_regressions),
                    "new_report_problems": len(new_problems), "improvements": len(improvements),
                    "changed_cases": len(changed)},
    }


def compare_failed(result: dict) -> bool:
    return bool(result["regressions"] or result["fixture_regressions"] or result["new_report_problems"])


# --------------------------------------------------------------------------
# Labelled-set grading (calibration / held-out measurement of the graders)
# --------------------------------------------------------------------------

GRADING_DIR = ("evals", "_grading")


def labelled_condition(text: str) -> str:
    """Judge-request condition for a labelled answer: a replay judge keys its rows by
    (case_id, "labelled:<first 12 hex of the answer's sha256>", 0)."""
    return "labelled:" + _sha(text)[:12]


def _judge_labelled(runner: ModelRun, case: el.Case, text: str, cache: dict) -> dict:
    key = (case.uid, _sha(text))
    if key not in cache:  # the judge and layered graders share one judge call per item
        cache[key] = runner.grade(case, text, labelled_condition(text), 0, "labelled-set")
    return cache[key]


def judge_grader(runner: ModelRun, cache: dict) -> eg.Grader:
    def grade(case: el.Case, text: str) -> tuple[str, dict]:
        res = _judge_labelled(runner, case, text, cache)
        if res["status"] != "graded":
            return "abstain", {"judge_status": res["status"]}
        return ("pass" if res["rubric_pass"] else "fail"), {"judge_status": "graded"}
    return grade


def layered_grader(runner: ModelRun, cache: dict) -> eg.Grader:
    def grade(case: el.Case, text: str) -> tuple[str, dict]:
        res = _judge_labelled(runner, case, text, cache)
        hard = eg.structural(case, text)
        verdict = eg.combine("ok", hard, eg.rubric_layer(case, _sha(text), judge=res))
        final = verdict["final"]
        if final == "correct":
            return "pass", {"final": final}
        if final in ("fail", "incorrect", "disputed", "structural-fail"):
            return "fail", {"final": final}
        return "abstain", {"final": final}
    return grade


def _fmt(p: dict) -> str:
    if p["rate"] is None:
        return "n/a (n=0)"
    lo, hi = p["ci95"]
    return f"{p['rate']:.3f} [{lo:.3f}, {hi:.3f}] (n={p['n']})"


def load_labelled_target(args: argparse.Namespace, suite: el.EvalSuite) -> dict[str, Any]:
    """Resolve --grade-labelled / --grade-heldout to its items (filtered by --skill) and their checks."""
    target = args.grade_labelled or "heldout"
    root = args.root.resolve()
    errors: list[str] = []
    warnings: list[str] = []
    manifest_path: Path | None = None
    if target == "dev-fixtures":
        items, exclude = eg.dev_fixture_items(suite), None
        partition = "dev"
    else:
        partition = target if target in eg.PARTITIONS else None
        manifest_path = root.joinpath(*GRADING_DIR, target, "manifest.jsonl") if partition else Path(target)
        if not manifest_path.is_file():
            items, exclude = [], None
            warnings.append(f"no labelled set at {el.rel(manifest_path, root)}")
        else:
            items, errors, warnings = eg.load_labelled(manifest_path)
            if partition:
                wrong = [it["item_id"] for it in items if it["partition"] != partition]
                if wrong:
                    errors.append(f"{len(wrong)} item(s) in the {partition} manifest are not partition "
                                  f"{partition!r}: {', '.join(wrong[:10])}")
            # Anything not dev must be independent of the tuned fixtures.
            exclude = eg.dev_texts(suite) if any(it["partition"] != "dev" for it in items) else None
    if args.skill:
        items = [it for it in items if it["skill"] in args.skill]
    return {"target": target, "partition": partition, "manifest_path": manifest_path, "items": items,
            "exclude": exclude, "errors": errors, "warnings": warnings}


def grade_labelled(args: argparse.Namespace, suite: el.EvalSuite, runner: ModelRun | None,
                   log: Callable[[str], None], *, loaded: dict[str, Any] | None = None,
                   environment: dict[str, Any] | None = None) -> tuple[dict[str, Any], bool]:
    loaded = loaded or load_labelled_target(args, suite)
    target, partition, manifest_path = loaded["target"], loaded["partition"], loaded["manifest_path"]
    items, exclude, errors = loaded["items"], loaded["exclude"], loaded["errors"]
    warnings = list(loaded["warnings"])
    root = args.root.resolve()
    graders: dict[str, eg.Grader] = {"regex": eg.regex_grader}
    judge = runner.judge if runner else None
    if judge and errors:
        warnings.append("judge not run: the manifest has errors (no judge calls are spent on an invalid set)")
        judge = None
    if judge:
        cache: dict = {}
        graders["judge"] = judge_grader(runner, cache)
        graders["layered"] = layered_grader(runner, cache)
    scored = eg.score_labelled(items, suite, graders, exclude_texts=exclude)
    status = eg.calibration_status(items, scored)
    dataset = provenance(suite, suite.cases)
    prov = report_provenance(args, dataset, environment or environment_info(root), "", judge,
                             schema=eg.CALIBRATION_SCHEMA, schema_version=eg.CALIBRATION_SCHEMA_VERSION)
    prov["manifest_sha256"] = _file_sha(manifest_path)
    doc = {
        "schema": eg.CALIBRATION_SCHEMA, "schema_version": eg.CALIBRATION_SCHEMA_VERSION,
        "grader_version": eg.GRADER_VERSION, "harness_version": el.HARNESS_VERSION,
        "target": target, "partition": partition,
        "manifest": {"path": el.rel(manifest_path, root) if manifest_path else None,
                     "sha256": _file_sha(manifest_path)},
        "dataset_sha256": dataset["dataset_sha256"], "provenance": prov,
        "status": status, "errors": errors, "warnings": warnings, **scored,
        "judge": judge.describe() if judge else None,
        "definitions": {
            "positive": "final_label 'pass' (a correct answer)",
            "tpr": "correct answers the grader passes", "fnr": "correct answers the grader fails",
            "tnr": "wrong answers the grader fails", "fpr": "wrong answers the grader passes (the gate's miss rate)",
            "excluded": "unresolved labels, unknown cases and dev-fixture duplicates are counted, never scored",
            "ci95": "Wilson score interval; descriptive, no significance claimed",
        },
        "notes": ["Labels with label_source 'agent-adjudicated' come from model labelers, not people: agreement "
                  "with them is not agreement with humans.",
                  "dev-fixtures items are the tuned evals/ fixtures; 100% there is expected by construction."],
    }
    log(f"run_evals: labelled set {target}: {status['status']}; items {scored['counts']['items']}, usable "
        f"{scored['counts']['usable']}, excluded {scored['counts']['excluded']}")
    for name, res in scored["graders"].items():
        a = res["aggregate"]
        log(f"  {name}: TPR {_fmt(a['tpr'])}  TNR {_fmt(a['tnr'])}  FPR {_fmt(a['fpr'])}  FNR {_fmt(a['fnr'])}  "
            f"abstained {a['abstained']}  not-gradable {res['not_gradable']}")
    for e in errors:
        log(f"  error: {e}")
    return doc, bool(errors)


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description="Run the ml-ai-skills behavioral eval suite (see docs/EVALUATION.md).")
    ap.add_argument("--root", type=Path, default=sl.REPO_ROOT, help="repository root (default: this repo)")
    ap.add_argument("--out", type=Path, help="write the JSON report here (default: stdout)")
    ap.add_argument("--skill", action="append", help="only cases of this skill (repeatable)")
    ap.add_argument("--case", action="append", help="only this case id or <skill>/<id> (repeatable)")
    ap.add_argument("--responses", type=Path, help="JSONL of recorded responses to grade offline")
    ap.add_argument("--rubric-labels", type=Path,
                    help="JSONL of per-item rubric labels (e.g. human review) keyed by case_id + response_sha256")
    ap.add_argument("--strict", action="store_true",
                    help="exit 1 if any graded response fails a hard assertion or any model attempt is not ok")
    ap.add_argument("--compare", nargs=2, type=Path, metavar=("BASE", "NEW"), help="regression-compare two reports")
    x = ap.add_argument_group("executable cases")
    x.add_argument("--run-executable", action="store_true", help="run tests/examples ids of executable cases")
    x.add_argument("--python", default=sys.executable, help="interpreter for --run-executable")
    x.add_argument("--executable-timeout", type=float, default=900.0, help="seconds per test id")
    x.add_argument("--slow-examples", action="store_true",
                   help=f"set {SLOW_EXAMPLES_ENV}=1 for the tests, so opt-in slow tests run instead of skipping")
    x.add_argument("--require-example-deps", action="store_true",
                   help=f"set {REQUIRE_EXAMPLE_DEPS_ENV}=1: a missing example dependency fails instead of skipping")
    x.add_argument("--require-executed", action="store_true",
                   help="exit 1 if any requested executable test was skipped (skipped is not evidence)")
    c = ap.add_argument_group("labelled-set grading (grader calibration and held-out measurement; offline)")
    c.add_argument("--grade-labelled", metavar="SET",
                   help="score the graders against a labelled set: heldout, calibration, dev-fixtures, or a "
                        "manifest.jsonl path (format: evals/_grading/README.md)")
    c.add_argument("--grade-heldout", action="store_true",
                   help="shorthand for --grade-labelled heldout (evals/_grading/heldout/manifest.jsonl)")
    g = ap.add_argument_group("model mode (opt-in; paid providers need a cost ceiling; see docs/EVALUATION.md)")
    g.add_argument("--provider", choices=PROVIDERS)
    g.add_argument("--model")
    g.add_argument("--allow-network", action="store_true")
    g.add_argument("--max-cost-usd", type=float, help="hard spend ceiling for the whole run (answers + judge)")
    g.add_argument("--price-input-per-mtok", type=float, help="USD per million input tokens (anthropic: required)")
    g.add_argument("--price-output-per-mtok", type=float, help="USD per million output tokens (anthropic: required)")
    g.add_argument("--allow-partial-budget", action="store_true",
                   help="start even if the worst-case estimate exceeds --max-cost-usd (the ceiling still stops it)")
    g.add_argument("--dry-run", action="store_true", help="print the request plan and cost estimate; send nothing")
    g.add_argument("--conditions", default="with_skill,without_skill",
                   type=lambda s: [x.strip() for x in s.split(",") if x.strip()])
    g.add_argument("--samples", type=int, default=1, help="answers per case and condition (default 1)")
    g.add_argument("--temperature", type=float, default=None,
                   help="sent only if given; newer models/API versions reject non-default sampling parameters")
    g.add_argument("--max-tokens", type=int, default=4096)
    g.add_argument("--timeout", type=float, default=120.0, help="per-request timeout, seconds")
    g.add_argument("--max-retries", type=int, default=4, help="retries on 408/429/5xx/529 and connection errors")
    g.add_argument("--backoff-base", type=float, default=1.0, help="seconds; exponential backoff base")
    g.add_argument("--backoff-cap", type=float, default=60.0, help="seconds; cap on any single wait")
    g.add_argument("--endpoint", default=API_URL, help="Messages API URL (https only)")
    g.add_argument("--anthropic-version", default=DEFAULT_ANTHROPIC_VERSION)
    g.add_argument("--api-key-env", default=DEFAULT_API_KEY_ENV, help="environment variable holding the API key")
    g.add_argument("--claude-cli-path", default="claude", help="claude executable for --provider claude-cli")
    g.add_argument("--claude-cli-arg", action="append", help="extra argument for the claude CLI (repeatable)")
    g.add_argument("--no-cli-budget-flag", action="store_true",
                   help="do not pass --max-budget-usd to the claude CLI (for builds without the flag)")
    g.add_argument("--replay-file", type=Path, help="recorded answers for --provider replay")
    g.add_argument("--save-responses", type=Path, help="also write model responses as re-gradable JSONL")
    g.add_argument("--run-dir", type=Path, help="directory for per-attempt records (manifest.json, records.jsonl)")
    g.add_argument("--resume", action="store_true", help="reuse completed attempts in --run-dir whose provenance matches")
    j = ap.add_argument_group("judge (optional; configured separately from the answering model)")
    j.add_argument("--judge-provider", choices=PROVIDERS, help="default: --provider")
    j.add_argument("--judge-model", help="optional model judge for the rubric (layer 3)")
    j.add_argument("--judge-max-tokens", type=int, default=2048)
    j.add_argument("--judge-temperature", type=float, default=None)
    j.add_argument("--judge-price-input-per-mtok", type=float)
    j.add_argument("--judge-price-output-per-mtok", type=float)
    j.add_argument("--judge-replay-file", type=Path, help="recorded judge outputs for --judge-provider replay")
    return ap


def parse_args(argv: list[str] | None) -> argparse.Namespace:
    return build_parser().parse_args(argv)


def _now_utc() -> str:
    return _dt.datetime.now(_dt.timezone.utc).replace(microsecond=0).isoformat()


def main(argv: list[str] | None = None, *, env: dict | None = None, transport: Transport | None = None,
         sleep: Callable[[float], None] = time.sleep, now: Callable[[], str] | None = None,
         environment: dict | None = None, stdout=None, stderr=None, backend_factory: BackendFactory | None = None,
         cli_runner=None) -> int:
    args = parse_args(argv)
    env = os.environ if env is None else env
    stdout = stdout or sys.stdout
    stderr = stderr or sys.stderr
    now = now or _now_utc
    secrets = eb.secret_values(env, (args.api_key_env,))

    def log(msg: str) -> None:
        print(eb.redact(msg, secrets), file=stderr)

    def emit(doc: dict) -> None:
        text = eb.redact(dump(doc), secrets)
        if args.out:
            args.out.write_text(text, encoding="utf-8", newline="\n")
        else:
            stdout.write(text)

    if args.compare:
        try:
            base, new = (json.loads(p.read_text(encoding="utf-8")) for p in args.compare)
        except (OSError, json.JSONDecodeError) as e:
            log(f"run_evals: cannot read report: {e}")
            return 2
        for p, r in zip(args.compare, (base, new), strict=True):
            if not isinstance(r, dict) or r.get("schema") != REPORT_SCHEMA:
                log(f"run_evals: {p} is not a {REPORT_SCHEMA} report")
                return 2
            if r.get("schema_version") not in READABLE_SCHEMA_VERSIONS:
                log(f"run_evals: {p} has schema_version {r.get('schema_version')!r}; this harness reads "
                    f"{READABLE_SCHEMA_VERSIONS}")
                return 2
        result = compare(base, new)
        emit(result)
        return 1 if compare_failed(result) else 0

    problems = usage_problems(args)
    if problems:
        log("run_evals: usage error: " + "; ".join(problems))
        return 2
    labelled = bool(args.grade_labelled or args.grade_heldout)
    answer_given = _flags_given(args, ANSWER_DESTS)
    judge_given = _flags_given(args, JUDGE_DESTS)
    shared_given = _flags_given(args, SHARED_DESTS)
    model_mode = bool(answer_given or (shared_given and not judge_given))
    if model_mode:
        missing = model_preconditions(args, env)
        if missing:
            given = answer_given + shared_given
            head = ("no model configured" if args.provider is None or "--model <model id>" in missing
                    else "model mode refused")
            log(f"run_evals: not-run: {head}. Refusing to call a model API. Model-mode option(s) given: "
                f"{', '.join(given)}. Missing: {', '.join(missing)}. Nothing was sent; see docs/EVALUATION.md.")
            return 2
        if args.responses or labelled:
            log("run_evals: --responses / --grade-* (offline grading) and model mode are mutually exclusive")
            return 2
        if not args.conditions or any(c not in CONDITIONS for c in args.conditions):
            log(f"run_evals: --conditions must be drawn from {CONDITIONS}")
            return 2
    if judge_given:
        if not (model_mode or args.responses or labelled):
            log("run_evals: not-run: judge options need responses to grade (model mode, --responses, or "
                "--grade-labelled); nothing was sent")
            return 2
        jmissing = judge_preconditions(args, env)
        if jmissing:
            log(f"run_evals: not-run: judge refused. Judge option(s) given: {', '.join(judge_given)}. "
                f"Missing: {', '.join(jmissing)}. Nothing was sent; see docs/EVALUATION.md.")
            return 2

    def make(role: str) -> eb.Backend:
        if backend_factory:
            return backend_factory(role, args)
        return make_backend(role, args, env, transport=transport, sleep=sleep, cli_runner=cli_runner)

    input_errors: list[str] = []
    for path in [p for p in (args.replay_file if args.provider == "replay" else None,
                             args.judge_replay_file if judge_given else None) if p]:
        if not path.is_file():
            log(f"run_evals: usage error: {path} does not exist")
            return 2
        input_errors += eb.load_replay(path)[1]

    root = args.root.resolve()
    suite = el.load_suite(root)
    cases = select_cases(suite.cases, args.skill, args.case)
    if (args.skill or args.case) and not cases:
        log("run_evals: --skill/--case selected no cases")
        return 2
    rubric_labels = None
    if args.rubric_labels:
        rubric_labels, errs = eg.load_rubric_labels(args.rubric_labels)
        input_errors += errs

    judge = None
    budget = eb.Budget(args.max_cost_usd)
    if judge_given:
        judge = eg.Judge(make("judge"), max_tokens=args.judge_max_tokens, temperature=args.judge_temperature)

    def judge_only_gate(pairs: list[tuple[el.Case, str]], plan_cases: list[el.Case]) -> int | None:
        """Preflight for a judge-only run (--responses / --grade-*): print the worst case, honour --dry-run,
        refuse above the ceiling. None means go ahead."""
        worst = judge_worst_case(judge, pairs)
        preflight = {"requests_planned": len(pairs), "answer_requests": 0, "judge_requests_max": len(pairs),
                     "worst_case_usd": worst, "ceiling_usd": args.max_cost_usd, "estimate_note": ESTIMATE_NOTE}
        log(f"run_evals: judge-only - up to {len(pairs)} request(s) to {judge.backend.provider} (judge "
            f"{judge.backend.model}); worst-case cost {_money(worst, 'unknown (unpriced)')}, ceiling "
            f"{_money(args.max_cost_usd, 'none (free backend)')}")
        if args.dry_run:
            emit(plan_doc(args, suite, plan_cases, preflight, None, judge, environment or environment_info(root),
                          now()))
            return 0
        return 2 if over_ceiling(args, judge.backend.paid, worst, log) else None

    if labelled:
        if judge and input_errors:
            log("run_evals: input errors; not spending judge calls:\n  " + "\n  ".join(input_errors))
            return 1
        loaded = load_labelled_target(args, suite)
        if judge and not loaded["errors"]:
            usable, _, _ = eg.usable_items(loaded["items"], suite, exclude_texts=loaded["exclude"])
            by_uid = {c.uid: c for c in suite.cases}
            gate = judge_only_gate([(by_uid[it["case_id"]], it["answer_text"]) for it in usable],
                                   [by_uid[u] for u in sorted({it["case_id"] for it in usable})])
            if gate is not None:
                return gate
        runner = ModelRun(None, judge, budget, None, log, now) if judge else None
        doc, bad = grade_labelled(args, suite, runner, log, loaded=loaded,
                                  environment=environment or environment_info(root))
        doc["generated_at"] = now()
        if judge:
            doc["budget"] = budget.to_dict()
        emit(doc)
        return 1 if bad else 0

    graded: list[dict] = []
    model_run = None
    mode = "offline"

    if args.responses:
        rows, errs = load_responses(args.responses, {c.uid for c in cases})
        input_errors += errs
        by_uid = {c.uid: c for c in cases}
        if judge:
            if input_errors:
                log("run_evals: input errors; not spending judge calls:\n  " + "\n  ".join(input_errors))
                return 1
            gate = judge_only_gate([(by_uid[r["case_id"]], r["response"]) for r in rows if r["status"] == "ok"],
                                   cases)
            if gate is not None:
                return gate
        runner = ModelRun(None, judge, budget, None, log, now) if judge else None
        for r in rows:
            case = by_uid[r["case_id"]]
            jres = (runner.grade(case, r["response"], r["condition"], r["sample"], "recorded")
                    if runner and r["status"] == "ok" else None)
            graded.append(graded_entry(case, r["condition"], r["response"], "recorded", sample=r["sample"],
                                       status=r["status"], rubric_labels=rubric_labels, judge=jres,
                                       judge_configured=judge is not None))
        if judge:
            model_run = judge_only_meta(args, judge, budget)

    if model_mode:
        if suite.errors or any(not r["ok"] for r in suite.fixture_results) or input_errors:
            log("run_evals: eval suite or inputs are invalid (run scripts/validate_evals.py); not spending API "
                "calls on it")
            return 1
        backend = make("answer")
        reqs = plan_requests(cases, suite, args)
        worst = worst_case_total(reqs, backend, judge, {c.uid: c for c in cases})
        n_calls = len(reqs) * (2 if judge else 1)
        preflight = {"requests_planned": n_calls, "answer_requests": len(reqs),
                     "judge_requests_max": len(reqs) if judge else 0, "worst_case_usd": worst,
                     "ceiling_usd": args.max_cost_usd, "estimate_note": ESTIMATE_NOTE}
        target = backend.client.url if isinstance(backend, eb.AnthropicBackend) else backend.provider
        log(f"run_evals: model mode - up to {n_calls} request(s) to {target} (model {backend.model}"
            f"{', judge ' + judge.backend.model if judge else ''}); worst-case cost "
            f"{_money(worst, 'unknown (unpriced)')}, ceiling {_money(args.max_cost_usd, 'none (free backend)')}")
        if args.dry_run:
            emit(plan_doc(args, suite, cases, preflight, backend, judge, environment or environment_info(root),
                          now()))
            return 0
        if over_ceiling(args, backend.paid or bool(judge and judge.backend.paid), worst, log):
            return 2
        store = None
        generated_at = now()
        if args.run_dir:
            store = RunStore(args.run_dir, secrets)
            err = store.open(resume=args.resume, manifest={
                "schema": RUN_SCHEMA, "schema_version": 1, "created_at": generated_at,
                "provenance": report_provenance(args, provenance(suite, cases), environment or environment_info(root),
                                                generated_at, judge),
                "backend": backend.identity(), "judge": judge.identity() if judge else None})
            if err:
                log(f"run_evals: usage error: {err}")
                return 2
        mode = "model"
        runner = ModelRun(backend, judge, budget, store, log, now)
        graded = run_model(cases, suite, args, runner, rubric_labels)
        model_run = model_meta(args, runner, store, preflight)
        if args.save_responses:
            args.save_responses.write_text(eb.redact("".join(
                json.dumps({"case_id": g["case_id"], "condition": g["condition"], "sample": g["sample"],
                            "response": g["response"], **({"status": g["status"]} if g["status"] != "ok" else {})},
                           ensure_ascii=False) + "\n" for g in graded if g["status"] in eb.COMPLETED_STATUSES),
                secrets), encoding="utf-8", newline="\n")

    env_overrides = {}
    if args.slow_examples:
        env_overrides[SLOW_EXAMPLES_ENV] = "1"
    if args.require_example_deps:
        env_overrides[REQUIRE_EXAMPLE_DEPS_ENV] = "1"
    executable = (run_executable(cases, root, args.python, args.executable_timeout, env_overrides=env_overrides)
                  if args.run_executable else None)
    generated_at = now()
    environment = environment or environment_info(root)
    report = build_report(suite, cases, mode=mode, graded=graded, input_errors=input_errors, executable=executable,
                          model_run=model_run, environment=environment, generated_at=generated_at,
                          report_prov=report_provenance(args, provenance(suite, cases), environment, generated_at,
                                                        judge))
    emit(report)
    if mode == "model" and args.run_dir:
        (args.run_dir / "report.json").write_text(eb.redact(dump(report), secrets), encoding="utf-8", newline="\n")

    failed = bool(suite.errors or input_errors or report["fixture_self_tests"]["failed"])
    if executable:
        for r in executable["results"]:
            label = r.get("status_label", r["status"])
            log(f"run_evals: executable {r['test_id']}: {label}"
                + (f" - {r['detail']}" if r["status"] != "passed" else ""))
        if executable["counts"]["failed"]:
            failed = True
        if args.require_executed and executable["counts"]["skipped"]:
            log(f"run_evals: --require-executed: {executable['counts']['skipped']} executable test(s) were skipped "
                "(not evidence)")
            failed = True
    if mode == "model":
        me = report["model_evaluation"]
        log(f"run_evals: model run {'complete' if me['complete'] else 'INCOMPLETE'}: {me['by_status']}")
    if args.strict and any(g["status"] != "ok" or g["hard"]["passed"] is False for g in graded):
        failed = True
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
