#!/usr/bin/env python3
"""Fail unless every runnable python block of every SKILL.md was executed.

tests/examples/harness.py appends one JSON line per ``run_blocks`` call to the
file named by ML_AI_SKILLS_EXAMPLE_COVERAGE: ``{"skill", "blocks",
"total_blocks", "skill_sha256"}`` plus, from schema "ml-ai-skills/example-coverage"
version 2, where and by which test it ran (``git_revision``, ``git_dirty`` for
tracked files, ``git_untracked_files``, ``python``, ``platform``, ``recorded_at``,
``env``, ``test_id``, ``test_module``).
Run the example suite with that variable set, then run this script on the file.
It checks, for every skill in the tree:

  * every python block that is not ``# illustrative`` (first non-blank line,
    the harness convention) appears in some record's ``blocks``;
  * every record for the skill was made against the current SKILL.md: its
    ``total_blocks`` equals the current python block count and its
    ``skill_sha256`` (LF-normalised SHA-256) equals the current file's. A stale
    record counts for nothing and is reported.

A malformed line (including a provenance field of the wrong type or an unknown
``schema`` / ``schema_version``), a record without ``skill_sha256``, a record for a
slug that is not a skill folder, or an empty / missing file is an error: the gate
fails closed rather than vouching for coverage it could not tie to this tree. The
provenance fields are reported (``--json`` "provenance"), not compared: the SKILL.md
hash is what ties a record to the tree.

This gate judges one coverage file and fails on any stale record even when other
records cover the skill (CI truncates the file before the run, so a stale line means
the file was reused). scripts/quality_report.py summarises per skill instead: current
records that cover every runnable block decide, and stale lines are listed beside them.

    ML_AI_SKILLS_EXAMPLE_COVERAGE=cov.jsonl python -m unittest discover -s tests/examples -t tests
    python scripts/check_example_coverage.py cov.jsonl
    python scripts/check_example_coverage.py cov.jsonl --json

Exit codes: 0 = every runnable block executed; 1 = a block was not executed or a
record is stale/malformed/unknown; 2 = the coverage file is missing, unreadable or
has no records.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import skills_lib as sl  # noqa: E402

COVERAGE_ENV = "ML_AI_SKILLS_EXAMPLE_COVERAGE"  # same name as tests/examples/harness.py
ILLUSTRATIVE_MARKER = "# illustrative"  # same convention as tests/examples/harness.py
COVERAGE_SCHEMA = "ml-ai-skills/example-coverage"  # harness.COVERAGE_SCHEMA
COVERAGE_SCHEMA_VERSIONS = (2,)                    # records without "schema" are the earlier format
# Optional provenance fields (harness schema v2) and the types they must have when present.
PROVENANCE_TYPES = {"git_revision": (str, type(None)), "git_dirty": (bool, type(None)),
                    "git_untracked_files": (int, type(None)), "python": (str,),
                    "platform": (str,), "recorded_at": (str,), "env": (dict,), "test_id": (str, type(None)),
                    "test_module": (str, type(None))}


def is_illustrative(code: str) -> bool:
    for line in code.splitlines():
        if line.strip():
            return line.strip().lower().startswith(ILLUSTRATIVE_MARKER)
    return False


def skill_sha256(skill_md: Path) -> str:
    return hashlib.sha256(skill_md.read_bytes().replace(b"\r\n", b"\n")).hexdigest()


def inventory(root: Path) -> dict[str, dict]:
    """slug -> {"total_blocks", "runnable" (sorted indices), "sha256"} for every skill."""
    out = {}
    for skill in sl.load_all_skills(root):
        blocks = [code for _, code in skill.code_blocks(lang="python")]
        out[skill.slug] = {
            "total_blocks": len(blocks),
            "runnable": [i for i, code in enumerate(blocks) if not is_illustrative(code)],
            "sha256": skill_sha256(skill.path),
        }
    return out


def evaluate(lines: list[str], inv: dict[str, dict]) -> dict:
    """Return {"errors": [...], "skills": {slug: {...}}, "provenance": {...}} for the coverage lines."""
    errors: list[str] = []
    executed: dict[str, set[int]] = {slug: set() for slug in inv}
    prov = {"records": 0, "legacy_records": 0, "records_without_revision": 0, "git_revisions": set(),
            "python": set(), "platforms": set()}
    for n, line in enumerate(lines, 1):
        if not line.strip():
            continue
        try:
            rec = json.loads(line)
            slug = rec["skill"]
            blocks = {int(i) for i in rec["blocks"]}
            total = rec["total_blocks"]
            sha = rec["skill_sha256"]
            if not isinstance(slug, str) or not isinstance(total, int) or not isinstance(sha, str):
                raise TypeError("wrong field type")
            if "schema" in rec and (rec["schema"] != COVERAGE_SCHEMA
                                    or rec.get("schema_version") not in COVERAGE_SCHEMA_VERSIONS):
                raise ValueError(f"unsupported schema {rec.get('schema')!r} version {rec.get('schema_version')!r}")
            for key, types_ in PROVENANCE_TYPES.items():
                if key in rec and not isinstance(rec[key], types_):
                    raise TypeError(f"{key} has type {type(rec[key]).__name__}")
        except (json.JSONDecodeError, KeyError, TypeError, ValueError, AttributeError) as exc:
            errors.append(f"line {n}: malformed coverage record ({type(exc).__name__}: {exc})")
            continue
        prov["records"] += 1
        prov["legacy_records"] += "schema" not in rec
        if rec.get("git_revision"):
            prov["git_revisions"].add(rec["git_revision"])
        else:
            prov["records_without_revision"] += 1
        for key, field in (("python", "python"), ("platforms", "platform")):
            if rec.get(field):
                prov[key].add(rec[field])
        cur = inv.get(slug)
        if cur is None:
            errors.append(f"line {n}: coverage record for {slug!r}, which is not a skill folder in this tree")
            continue
        if total != cur["total_blocks"]:
            errors.append(f"line {n}: {slug}: record has total_blocks {total}, the current SKILL.md has "
                          f"{cur['total_blocks']} python block(s) (stale record)")
            continue
        if sha != cur["sha256"]:
            errors.append(f"line {n}: {slug}: record skill_sha256 differs from the current SKILL.md (stale record)")
            continue
        out_of_range = sorted(i for i in blocks if not 0 <= i < total)
        if out_of_range:
            errors.append(f"line {n}: {slug}: block indices {out_of_range} out of range (0..{total - 1})")
        executed[slug] |= {i for i in blocks if 0 <= i < total}

    skills = {}
    for slug, cur in sorted(inv.items()):
        missing = [i for i in cur["runnable"] if i not in executed[slug]]
        skills[slug] = {"runnable": cur["runnable"], "executed": sorted(executed[slug]), "missing": missing}
        if missing:
            errors.append(f"{slug}: runnable python block(s) {missing} were never executed by tests/examples "
                          f"(of {cur['total_blocks']} block(s); mark documentation-only blocks '# illustrative')")
    return {"errors": errors, "skills": skills,
            "provenance": {k: sorted(v) if isinstance(v, set) else v for k, v in prov.items()}}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("coverage", nargs="?", type=Path, default=None,
                    help=f"coverage jsonl (default: ${COVERAGE_ENV})")
    ap.add_argument("--root", type=Path, default=sl.REPO_ROOT, help="repo root holding the skill folders")
    ap.add_argument("--json", action="store_true", help="print the per-skill result as JSON")
    args = ap.parse_args(argv)

    path = args.coverage or (Path(os.environ[COVERAGE_ENV]) if os.environ.get(COVERAGE_ENV) else None)
    if path is None:
        print(f"usage error: pass the coverage file or set {COVERAGE_ENV}", file=sys.stderr)
        return 2
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeDecodeError) as exc:
        print(f"coverage error: cannot read {path}: {exc}", file=sys.stderr)
        return 2
    if not any(line.strip() for line in lines):
        print(f"coverage error: {path} has no records (was {COVERAGE_ENV} set for the example run?)",
              file=sys.stderr)
        return 2

    result = evaluate(lines, inventory(args.root.resolve()))
    if args.json:
        print(json.dumps(result, indent=2, sort_keys=True))
    else:
        for err in result["errors"]:
            print(f"COVERAGE ERROR: {err}")
        n_run = sum(len(s["runnable"]) for s in result["skills"].values())
        n_skills = sum(1 for s in result["skills"].values() if s["runnable"])
        if not result["errors"]:
            print(f"every runnable python block executed: {n_run} block(s) across {n_skills} skill(s)")
        p = result["provenance"]
        print(f"provenance: {p['records']} record(s); git revision(s) {', '.join(p['git_revisions']) or 'none'}"
              f" ({p['records_without_revision']} without); python {', '.join(p['python']) or 'unknown'}; "
              f"platform {', '.join(p['platforms']) or 'unknown'}")
    return 1 if result["errors"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
