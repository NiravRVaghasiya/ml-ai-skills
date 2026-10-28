#!/usr/bin/env python3
"""Validate evals/ — case structure, hard-assertion lint, fixture self-tests,
per-skill plan.json files — and optionally report coverage. See evals/SCHEMA.md.

This does NOT run any model. It proves three things about the eval suite
itself, so a broken case fails fast instead of silently grading nothing:

1. every case file is well-formed and points at a real skill (and real
   tests/examples test ids for `kind: executable`);
2. every hard assertion is a valid, non-vacuous regex;
3. every case with hard assertions is satisfiable (its `.pass*.md` fixtures
   pass) and discriminating (its `.fail*.md` fixtures fail); warnings flag
   patterns that match every wrong answer and cases whose wrong answers are
   all concept-free strawmen (no on-topic fail fixture). Advisory warnings
   flag must_not_include patterns that no fail fixture triggers (never shown
   to fire); --strict does not promote those yet, --strict-advisories does.

Usage:
    python scripts/validate_evals.py                          # errors -> exit 1
    python scripts/validate_evals.py --coverage               # + coverage table
    python scripts/validate_evals.py --coverage --json        # machine-readable
    python scripts/validate_evals.py --require-full-coverage  # gaps -> exit 1
    python scripts/validate_evals.py --strict                 # warnings -> exit 1
    python scripts/validate_evals.py --strict-advisories      # advisory warnings -> exit 1
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import eval_lib as el  # noqa: E402
import skills_lib as sl  # noqa: E402


def _coverage_table(cov: dict) -> list[str]:
    lines = [f"{'skill':<28} {'plan':<5} {'cases':>5} {'non-happy':>9} {'hard':>5} {'exec':>5}  missing"]
    for r in cov["skills"]:
        lines.append(f"{r['skill']:<28} {'yes' if r['has_plan'] else 'no':<5} {r['cases']:>5} "
                     f"{r['non_happy_path_cases']:>9} {r['hard_assertion_cases']:>5} {r['executable_cases']:>5}  "
                     f"{', '.join(r['missing']) or '-'}")
    s = cov["summary"]
    lines.append(f"\ncoverage: {s['fully_covered']}/{s['skills_total']} skills fully covered "
                 f"(plan {s['with_plan']}, cases {s['with_cases']}, non-happy-path {s['with_non_happy_path']}, "
                 f"hard assertions {s['with_hard_assertions']}, executable {s['with_executable']}); "
                 f"{s['cases_with_hard_assertions']}/{s['cases_total']} cases have hard assertions")
    return lines


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--root", type=Path, default=sl.REPO_ROOT, help="repository root (default: this repo)")
    ap.add_argument("--coverage", action="store_true", help="print per-skill coverage")
    ap.add_argument("--require-full-coverage", action="store_true",
                    help="exit 1 if any skill lacks plan.json, a case, or a non-happy-path case")
    ap.add_argument("--strict", action="store_true", help="treat warnings as errors")
    ap.add_argument("--strict-advisories", action="store_true",
                    help="also treat advisory warnings (unexercised must_not_include patterns) as errors")
    ap.add_argument("--json", action="store_true", help="print a JSON document instead of text")
    args = ap.parse_args(argv)

    suite = el.load_suite(args.root.resolve())
    cov = el.coverage(suite)
    fixtures_ok = sum(r["ok"] for r in suite.fixture_results)
    gaps = cov["gaps"] if args.require_full_coverage else []
    failed = (bool(suite.errors) or bool(gaps) or (args.strict and bool(suite.warnings))
              or (args.strict_advisories and bool(suite.advisories)))

    if args.json:
        doc = {"harness_version": el.HARNESS_VERSION, "ok": not failed, "errors": suite.errors,
               "warnings": suite.warnings, "advisories": suite.advisories,
               "fixture_self_tests": {"total": len(suite.fixture_results), "ok": fixtures_ok},
               "coverage": cov if (args.coverage or args.require_full_coverage) else None}
        print(json.dumps(doc, indent=2, sort_keys=True))
        return 1 if failed else 0

    for a in suite.advisories:
        print(f"EVAL WARNING (advisory): {a}")
    for w in suite.warnings:
        print(f"EVAL WARNING: {w}")
    for e in suite.errors:
        print(f"EVAL ERROR: {e}")
    if args.coverage or args.require_full_coverage:
        print("\n".join(_coverage_table(cov)))
    for g in gaps:
        print(f"EVAL COVERAGE GAP: {g['skill']}: missing {', '.join(g['missing'])}")
    if suite.errors:
        print(f"\n{len(suite.errors)} error(s) across {len(suite.cases)} case file(s)")
    if failed:
        if args.strict and suite.warnings and not suite.errors:
            print(f"\n{len(suite.warnings)} warning(s) (--strict)")
        if args.strict_advisories and suite.advisories and not suite.errors:
            print(f"\n{len(suite.advisories)} advisory warning(s) (--strict-advisories)")
        return 1

    covered = sorted({c.skill_dir for c in suite.cases})
    print(f"{len(suite.cases)} eval case(s) OK, covering {len(covered)}/{len(suite.skills)} skills: "
          f"{', '.join(covered)}")
    print(f"fixture self-tests: {fixtures_ok}/{len(suite.fixture_results)} OK; "
          f"{len(suite.plans)} plan.json file(s) OK; {len(suite.warnings)} warning(s); "
          f"{len(suite.advisories)} advisory warning(s)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
