#!/usr/bin/env python3
"""Validate the structured claims registry (docs/claims.json). Offline, stdlib-only.

Checks, per claim: schema (required fields, enums, dates), the skill exists,
dates are real and at most one day in the future (timezone slack), the
evidence fits the verification kind (a runtime-test must name an existing
tests/**/test_*.py -- and optionally ::Class::method, resolved with `ast` so the
method must really sit in that class; human-review needs a reviewer and a
source), status agrees with verification, and last_reviewed is within the
freshness policy for that claim's skill (else REVIEW_DUE). Also checks that
every `claim` id referenced by schema/freshness_policy.json known_stale_patterns
exists here, and reports REVIEW_DUE for a STALE pattern (which fails CI) whose
backing claim is missing, unverified or disputed.

Usage:
    python scripts/check_claims.py
    python scripts/check_claims.py --json
    python scripts/check_claims.py --claims PATH --policy PATH --today 2026-10-27
    python scripts/check_claims.py --strict   # REVIEW_DUE also fails

Exit codes: 0 = valid (REVIEW_DUE findings are informational unless --strict);
1 = validation errors (or REVIEW_DUE with --strict); 2 = registry or policy
file missing / not JSON / policy invalid.
"""
from __future__ import annotations

import argparse
import ast
import datetime as dt
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import check_freshness as cf  # noqa: E402
import skills_lib as sl  # noqa: E402

DEFAULT_CLAIMS_PATH = sl.REPO_ROOT / "docs" / "claims.json"
STATUS = ("current", "review-due", "disputed", "unverified")
_NULLABLE_STR = {"type": ["string", "null"], "minLength": 1}

CLAIM_SCHEMA = {
    "type": "object",
    "required": ["id", "skill", "claim", "verification", "evidence", "source_url", "source_accessed",
                 "applies_to", "reviewer", "status", "last_reviewed"],
    "additionalProperties": False,
    "properties": {
        "id": {"type": "string", "pattern": cf.av.SLUG_PATTERN},
        "skill": {"type": ["string", "null"], "pattern": cf.av.SLUG_PATTERN},
        "claim": {"type": "string", "minLength": 1},
        "verification": {"type": "string", "enum": list(cf.CLAIM_VERIFICATION)},
        "evidence": {"type": "string", "minLength": 1},
        "source_url": {"type": ["string", "null"], "pattern": r"^https?://\S+$"},
        "source_accessed": {"type": ["string", "null"], "format": "date"},
        "applies_to": {
            "type": "object",
            "required": ["package", "api", "version"],
            "additionalProperties": False,
            "properties": {"package": _NULLABLE_STR, "api": {"type": "string", "minLength": 1},
                           "version": _NULLABLE_STR},
        },
        "reviewer": _NULLABLE_STR,
        "status": {"type": "string", "enum": list(STATUS)},
        "last_reviewed": {"type": "string", "format": "date"},
        "notes": _NULLABLE_STR,
    },
}
REGISTRY_SCHEMA = {
    "type": "object",
    "required": ["registry_version", "claims"],
    "additionalProperties": False,
    "properties": {"$comment": {"type": "string"}, "registry_version": {"const": 1},
                   "claims": {"type": "array", "items": {"type": "object"}}},
}

# tests/<path>.py optionally followed by ::Class or ::Class::method
_TEST_REF_RE = re.compile(r"(tests/[A-Za-z0-9_./-]+?\.py)((?:::[A-Za-z_][A-Za-z0-9_]*)*)")
# Matches unittest discovery (-p "test_*.py"): a helper module such as tests/testutil.py runs no tests.
_TEST_FILE_RE = re.compile(r"^test_[A-Za-z0-9_]*\.py$")
# A runtime-test claim recorded at local time in a zone ahead of UTC can be "tomorrow" for a UTC
# CI runner; one day of slack keeps that from failing CI while still catching real typos.
FUTURE_SLACK = dt.timedelta(days=1)


def _resolve_test_names(tree: ast.Module, names: list[str]) -> str | None:
    """Walk Class -> method through the AST; return the first name that is not where the ref says."""
    scope = tree.body
    for name in names:
        node = next((n for n in scope if isinstance(n, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef))
                     and n.name == name), None)
        if node is None:
            return name
        scope = node.body if isinstance(node, ast.ClassDef) else []
    return None


def _test_ref_errors(evidence: str, root: Path) -> list[str]:
    refs = _TEST_REF_RE.findall(evidence)
    if not refs:
        return ["runtime-test evidence must name a test file under tests/ (e.g. tests/examples/test_x.py::TestY)"]
    errs = []
    root = root.resolve()
    for rel, names in refs:
        path = (root / rel).resolve()
        if root not in path.parents:
            errs.append(f"evidence path {rel!r} escapes the repository")
            continue
        if not _TEST_FILE_RE.match(path.name):
            errs.append(f"evidence names {rel}, which is not a test module (test_*.py) that unittest discovers")
            continue
        if not path.is_file():
            errs.append(f"evidence names {rel}, which does not exist")
            continue
        parts = [n for n in names.split("::") if n]
        if not parts:
            continue
        try:
            tree = ast.parse(path.read_text(encoding="utf-8", errors="replace"), filename=str(path))
        except SyntaxError as e:
            errs.append(f"evidence names {rel}, which does not parse ({e.msg})")
            continue
        missing = _resolve_test_names(tree, parts)
        if missing is not None:
            ref = "::".join(parts)
            errs.append(f"evidence names {rel}::{ref}, but {missing!r} is not defined at that place "
                        "(top-level class/def, then a def inside that class)")
    return errs


def check_registry(data, skills: list[sl.Skill], policy: cf.Policy, today: dt.date,
                   root: Path = sl.REPO_ROOT) -> tuple[list[dict], list[dict]]:
    """Return (errors, findings); each item is {"claim", "code", "message"}."""
    errors: list[dict] = []
    findings: list[dict] = []

    def err(cid, code, msg):
        errors.append({"claim": cid, "level": "ERROR", "code": code, "message": msg})

    for e in sl.schema_errors(REGISTRY_SCHEMA, data):
        err(None, "registry-schema", e.message)
    if errors:
        return errors, findings

    by_slug = sl.by_slug(skills)
    seen: set[str] = set()
    for i, claim in enumerate(data["claims"]):
        cid = claim.get("id") if isinstance(claim.get("id"), str) else f"claims[{i}]"
        if claim.get("verification") == "static-pattern":
            err(cid, "static-pattern-is-not-verification",
                "verification 'static-pattern' is not allowed: a regex match is a heuristic; record the claim "
                "as 'unverified' until it is runtime-tested or reviewed")
        schema_errs = [e for e in sl.schema_errors(CLAIM_SCHEMA, claim, (f"claims[{i}]",))
                       if not (e.keyword == "enum" and claim.get("verification") == "static-pattern"
                               and e.path[-1:] == ("verification",))]
        for e in schema_errs:
            err(cid, f"schema-{e.keyword}", e.message)
        if schema_errs:
            continue

        if cid in seen:
            err(cid, "duplicate-id", f"id {cid!r} appears more than once")
        seen.add(cid)

        slug = claim["skill"]
        skill = by_slug.get(slug) if slug else None
        if slug and skill is None:
            err(cid, "unknown-skill", f"skill {slug!r} is not a skill folder in this repo")

        for key in ("source_accessed", "last_reviewed"):
            if claim[key] and dt.date.fromisoformat(claim[key]) > today + FUTURE_SLACK:
                err(cid, "date-in-future", f"{key} {claim[key]} is more than {FUTURE_SLACK.days} day after "
                                           f"today ({today.isoformat()})")
        if bool(claim["source_url"]) != bool(claim["source_accessed"]):
            err(cid, "source-without-access-date", "source_url and source_accessed must be set together")

        kind, status = claim["verification"], claim["status"]
        if kind == "runtime-test":
            for msg in _test_ref_errors(claim["evidence"], root):
                err(cid, "runtime-test-evidence", msg)
        if kind in ("runtime-test", "local-version-check") and not claim["applies_to"]["version"]:
            err(cid, "version-required", f"{kind} claims must record the version they ran against in applies_to")
        if kind == "local-version-check" and not claim["applies_to"]["package"]:
            err(cid, "package-required", "local-version-check claims must name the package in applies_to")
        if kind in ("automated-doc-check", "human-review") and not claim["source_url"]:
            err(cid, "source-required", f"{kind} claims must cite source_url and source_accessed")
        if kind == "human-review" and not claim["reviewer"]:
            err(cid, "reviewer-required", "human-review claims must name the reviewer")
        if status == "unverified" and kind != "unverified":
            err(cid, "status-mismatch", f"status 'unverified' contradicts verification {kind!r}")
        if kind == "unverified" and status not in ("unverified", "disputed"):
            err(cid, "status-mismatch", f"an unverified claim cannot have status {status!r}")

        max_age, rule = policy.max_age_for(slug, skill.frontmatter.get("domain") if skill else None,
                                           (skill.frontmatter.get("capabilities") or []) if skill else [])
        age = (today - dt.date.fromisoformat(claim["last_reviewed"])).days
        if age > max_age and status in ("current", "unverified"):
            findings.append({"claim": cid, "level": "REVIEW_DUE", "code": "review-overdue",
                             "message": f"last_reviewed is {age} days old (limit {max_age}, {rule}); re-check and "  # noqa: S608 - prose ("update ... set"), not SQL
                                        "update last_reviewed, or set status to 'review-due'"})
        elif status in ("review-due", "disputed"):
            findings.append({"claim": cid, "level": "REVIEW_DUE", "code": f"status-{status}",
                             "message": f"recorded status is {status!r}"})

    by_id = {c["id"]: c for c in data["claims"] if isinstance(c, dict) and isinstance(c.get("id"), str)}
    for p in policy.patterns:
        if p.claim and p.claim not in seen:
            err(p.claim, "policy-claim-missing",
                f"{policy.path} known_stale_patterns '{p.id}' cites claim {p.claim!r}, which is not in the registry")
            continue
        # A STALE pattern fails CI for every skill it matches. That is only justified when the
        # backing claim was actually checked, so a STALE pattern resting on nothing (or on an
        # unverified/disputed claim) is surfaced for review rather than silently trusted.
        if p.level == "STALE":
            backing = by_id.get(p.claim) if p.claim else None
            if backing is None:
                findings.append({"claim": None, "level": "REVIEW_DUE", "code": "stale-pattern-without-claim",
                                 "message": f"known_stale_patterns '{p.id}' is level STALE (fails CI) but cites "
                                            "no claim; add a verified claim or lower it to REVIEW_DUE"})
            elif backing.get("verification") == "unverified" or backing.get("status") == "disputed":
                findings.append({"claim": p.claim, "level": "REVIEW_DUE", "code": "stale-pattern-unverified",
                                 "message": f"known_stale_patterns '{p.id}' is level STALE (fails CI) but its "
                                            f"backing claim is {backing.get('verification')}/"
                                            f"{backing.get('status')}; verify it (runtime-test or human-review) "
                                            "or lower the pattern to REVIEW_DUE"})
    return errors, findings


def summarize(data) -> dict:
    claims = data.get("claims", []) if isinstance(data, dict) else []
    by_kind = {k: 0 for k in cf.CLAIM_VERIFICATION}
    by_status = {s: 0 for s in STATUS}
    for c in claims:
        if isinstance(c, dict):
            if c.get("verification") in by_kind:
                by_kind[c["verification"]] += 1
            if c.get("status") in by_status:
                by_status[c["status"]] += 1
    return {"claims": len(claims), "by_verification": by_kind, "by_status": by_status}


def _claim_label(claim) -> str:
    return "-" if claim is None else str(claim)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    ap.add_argument("--claims", type=Path, default=DEFAULT_CLAIMS_PATH)
    ap.add_argument("--policy", type=Path, default=cf.DEFAULT_POLICY_PATH)
    ap.add_argument("--root", type=Path, default=sl.REPO_ROOT, help="repo root (skills + tests/)")
    ap.add_argument("--today", type=dt.date.fromisoformat, default=None)
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--strict", action="store_true", help="REVIEW_DUE findings also exit 1")
    args = ap.parse_args(argv)

    skills = sl.load_all_skills(args.root)
    try:
        policy = cf.load_policy(args.policy, skills)
        data = json.loads(args.claims.read_text(encoding="utf-8"))
    except cf.PolicyError as e:
        print(f"ERROR {e}", file=sys.stderr)
        return 2
    except (OSError, json.JSONDecodeError) as e:
        print(f"ERROR {args.claims}: cannot read claims registry ({e})", file=sys.stderr)
        return 2

    today = args.today or dt.date.today()
    errors, findings = check_registry(data, skills, policy, today, args.root)
    summary = summarize(data)
    if args.json:
        print(json.dumps({"tool": "check_claims", "today": today.isoformat(), "summary": summary,
                          "verification_kinds": {k: cf.VERIFICATION_KINDS.get(k, "Not verified by any recorded "
                                                                                 "method.")
                                                 for k in cf.CLAIM_VERIFICATION},
                          "errors": errors, "findings": findings}, indent=2))
    else:
        # `claim` is None for registry-level problems and for a STALE pattern that cites no claim.
        for e in errors:
            print(f"ERROR      {_claim_label(e['claim']):44s} {e['message']}")
        for f in findings:
            print(f"REVIEW_DUE {_claim_label(f['claim']):44s} {f['message']}")
        kinds = ", ".join(f"{n} {k}" for k, n in summary["by_verification"].items())
        statuses = ", ".join(f"{n} {s}" for s, n in summary["by_status"].items())
        print(f"\n{summary['claims']} claim(s): {kinds}; status: {statuses}")
        print(f"{len(errors)} error(s), {len(findings)} review-due finding(s)")
        print("A claim's 'verification' records how it was checked on last_reviewed; it is not re-run by this "
              "script (runtime-test claims are only as current as the test suite run that backs them).")

    if errors:
        return 1
    return 1 if (args.strict and findings) else 0


if __name__ == "__main__":
    sys.exit(main())
