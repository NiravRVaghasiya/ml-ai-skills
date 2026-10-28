#!/usr/bin/env python3
"""Score scripts/router.py against the labelled routing benchmark.

Reads evals/routing/benchmark.json (see evals/routing/README.md for the label
guidelines and exact metric definitions), runs every case through the
router's public `route(task, top_k=...)` interface, and reports precision,
recall, top-1 accuracy, task-level selection accuracy, false-activation rate,
unnecessary activations and forbidden-selection rate — per split and per
category — plus the ids of every misrouted case.

Deterministic: no randomness, no network, no model calls. Output ordering is
fixed (benchmark order for cases, fixed category/split order for tables).

Usage:
    python scripts/eval_router.py                       # human table, all cases
    python scripts/eval_router.py --split test --json   # machine-readable
    python scripts/eval_router.py --min-task-accuracy 0.6 --max-false-activation 0.2
    python scripts/eval_router.py --router path/to/other_router.py   # A/B a variant

Exit codes: 0 = ok, 1 = a --min-*/--max-* threshold failed,
2 = the benchmark file is missing/unreadable/malformed, or a CLI usage error.
"""
from __future__ import annotations

import argparse
import contextlib
import datetime
import functools
import hashlib
import importlib.util
import json
import shlex
import sys
from pathlib import Path
from typing import Any, Callable, Iterable

SCRIPTS_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS_DIR))
import skills_lib as sl  # noqa: E402

ROUTING_DIR = sl.REPO_ROOT / "evals" / "routing"
BENCHMARK_PATH = ROUTING_DIR / "benchmark.json"
BASELINE_PATH = ROUTING_DIR / "baseline.json"

CATEGORIES = ("typical", "paraphrase", "ambiguous", "multi-skill", "no-skill", "conflicting", "adversarial")
SPLITS = ("dev", "test")
CASE_KEYS = ("id", "query", "category", "split", "expected", "acceptable", "forbidden", "notes")
LABEL_KEYS = ("expected", "acceptable", "forbidden")
PREREQ_REASON = "pulled in as a hard prerequisite"

# Coverage requirements the benchmark must keep satisfying as it is edited.
MIN_CASES = 150
MIN_PER_CATEGORY = 15
MIN_EXPECTED_PER_SKILL = 2
# Each split must exercise every skill at least once, or a held-out number
# says nothing about the skills it never asks for.
MIN_EXPECTED_PER_SKILL_PER_SPLIT = 1
TEST_FRACTION_RANGE = (0.35, 0.45)

# Direction of each headline metric, used by thresholds and the regression floor.
HIGHER_IS_BETTER = ("precision", "recall", "top1_accuracy", "task_accuracy")
LOWER_IS_BETTER = ("false_activation_rate", "mean_unnecessary_activations", "forbidden_selection_rate")
HEADLINE_METRICS = HIGHER_IS_BETTER + LOWER_IS_BETTER

RouteFn = Callable[..., tuple]


# --------------------------------------------------------------------------- benchmark

def load_benchmark(path: Path = BENCHMARK_PATH) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def benchmark_sha256(path: Path = BENCHMARK_PATH) -> str:
    # Normalise newlines so a CRLF checkout on Windows hashes the same as LF.
    return hashlib.sha256(path.read_bytes().replace(b"\r\n", b"\n")).hexdigest()


def prerequisite_closure(slug: str, requires: dict[str, list[str]]) -> set[str]:
    out: set[str] = set()
    stack = list(requires.get(slug, []))
    while stack:
        dep = stack.pop()
        if dep not in out:
            out.add(dep)
            stack.extend(requires.get(dep, []))
    return out


def requires_map(skills: Iterable[sl.Skill]) -> dict[str, list[str]]:
    return {s.slug: list(s.frontmatter.get("requires") or []) for s in skills}


def validate_benchmark(bench: Any, valid_slugs: Iterable[str],
                       requires: dict[str, list[str]] | None = None) -> list[str]:
    """Per-case well-formedness. Returns human-readable error strings.

    If `requires` (slug -> hard prerequisites) is given, also checks that every
    prerequisite of an expected *or acceptable* skill is labelled
    expected/acceptable: the router always loads prerequisites with their
    dependent, so forbidding one would make the case unsatisfiable (or punish
    selecting an acceptable skill), and leaving it unlabelled would count
    correct routing as an unnecessary activation."""
    valid = set(valid_slugs)
    errors: list[str] = []
    if not isinstance(bench, dict):
        return ["benchmark root must be an object"]
    if bench.get("version") != 1:
        errors.append(f"unsupported benchmark version: {bench.get('version')!r} (expected 1)")
    cases = bench.get("cases")
    if not isinstance(cases, list) or not cases:
        return errors + ["'cases' must be a non-empty list"]

    seen: set[str] = set()
    for idx, case in enumerate(cases):
        where = f"case[{idx}]"
        if not isinstance(case, dict):
            errors.append(f"{where}: must be an object")
            continue
        cid = case.get("id")
        if isinstance(cid, str) and cid:
            where = cid
            if cid in seen:
                errors.append(f"{cid}: duplicate id")
            seen.add(cid)
        else:
            errors.append(f"{where}: 'id' must be a non-empty string")
        missing = [k for k in CASE_KEYS if k not in case]
        extra = sorted(set(case) - set(CASE_KEYS))
        if missing:
            errors.append(f"{where}: missing keys {missing}")
        if extra:
            errors.append(f"{where}: unknown keys {extra}")
        if not isinstance(case.get("query"), str) or not case.get("query", "").strip():
            errors.append(f"{where}: 'query' must be a non-empty string")
        if not isinstance(case.get("notes", ""), str):
            errors.append(f"{where}: 'notes' must be a string")
        if case.get("category") not in CATEGORIES:
            errors.append(f"{where}: invalid category {case.get('category')!r}")
        if case.get("split") not in SPLITS:
            errors.append(f"{where}: invalid split {case.get('split')!r}")

        labels: dict[str, list[str]] = {}
        for key in LABEL_KEYS:
            val = case.get(key, [])
            if not isinstance(val, list) or not all(isinstance(s, str) for s in val):
                errors.append(f"{where}: '{key}' must be a list of slug strings")
                continue
            if len(set(val)) != len(val):
                errors.append(f"{where}: '{key}' has duplicate slugs")
            for slug in val:
                if slug not in valid:
                    errors.append(f"{where}: '{key}' references unknown skill {slug!r}")
            labels[key] = val
        if len(labels) == len(LABEL_KEYS):
            exp, acc, forb = (set(labels[k]) for k in LABEL_KEYS)
            for a, b in (("expected", "forbidden"), ("expected", "acceptable"), ("acceptable", "forbidden")):
                both = sorted(set(labels[a]) & set(labels[b]))
                if both:
                    errors.append(f"{where}: {a} and {b} overlap: {both}")
            cat = case.get("category")
            if cat == "no-skill" and (exp or acc):
                errors.append(f"{where}: no-skill cases must have empty expected and acceptable")
            if cat in ("typical", "paraphrase", "ambiguous", "multi-skill", "conflicting") and not exp:
                errors.append(f"{where}: {cat} cases need at least one expected skill")
            if cat == "multi-skill" and len(exp) < 2:
                errors.append(f"{where}: multi-skill cases need at least two expected skills")
            if cat == "conflicting" and not forb:
                errors.append(f"{where}: conflicting cases need at least one forbidden skill")
            for kind, group in (("expected", exp), ("acceptable", acc)):
                for slug in sorted(group) if requires is not None else ():
                    for pre in sorted(prerequisite_closure(slug, requires)):
                        if pre in forb:
                            errors.append(f"{where}: forbids {pre!r}, a hard prerequisite of {kind} {slug!r}")
                        elif pre not in exp and pre not in acc:
                            errors.append(f"{where}: {pre!r} (prerequisite of {kind} {slug!r}) "
                                          "must be expected or acceptable")
    return errors


def coverage_errors(bench: Any, valid_slugs: Iterable[str]) -> list[str]:
    """Benchmark-level coverage requirements (size, balance, per-skill and
    per-split coverage). Assumes validate_benchmark() passed; structurally
    broken input is reported rather than crashing."""
    cases = bench.get("cases") if isinstance(bench, dict) else None
    if not isinstance(cases, list) or not all(isinstance(c, dict) for c in cases):
        return ["benchmark structure invalid; fix validate_benchmark() errors first"]
    errors: list[str] = []
    if len(cases) < MIN_CASES:
        errors.append(f"only {len(cases)} cases; need >= {MIN_CASES}")
    for cat in CATEGORIES:
        n = sum(1 for c in cases if c.get("category") == cat)
        if n < MIN_PER_CATEGORY:
            errors.append(f"category {cat!r} has {n} cases; need >= {MIN_PER_CATEGORY}")
    for slug in sorted(valid_slugs):
        n = sum(1 for c in cases if slug in c.get("expected", []))
        if n < MIN_EXPECTED_PER_SKILL:
            errors.append(f"skill {slug!r} is expected in {n} case(s); need >= {MIN_EXPECTED_PER_SKILL}")
        for split in SPLITS:
            n = sum(1 for c in cases if c.get("split") == split and slug in c.get("expected", []))
            if n < MIN_EXPECTED_PER_SKILL_PER_SPLIT:
                errors.append(f"skill {slug!r} is expected in {n} {split} case(s); "
                              f"need >= {MIN_EXPECTED_PER_SKILL_PER_SPLIT}")
    # Every category that has real-task cases must have some in both splits, so
    # recall/top-1 are defined for it on the held-out split too.
    for cat in CATEGORIES:
        if not any(c.get("category") == cat and c.get("expected") for c in cases):
            continue
        for split in SPLITS:
            if not any(c.get("category") == cat and c.get("split") == split and c.get("expected") for c in cases):
                errors.append(f"category {cat!r} has no {split} case with expected skills")
    if cases:
        frac = sum(1 for c in cases if c.get("split") == "test") / len(cases)
        lo, hi = TEST_FRACTION_RANGE
        if not lo <= frac <= hi:
            errors.append(f"test split is {frac:.1%} of cases; want {lo:.0%}-{hi:.0%}")
    return errors


# --------------------------------------------------------------------------- routing

def selected_slugs(hits: Iterable[Any]) -> list[str]:
    """Every hit the router returned counts as selected — including hits whose
    reasons say they were 'pulled in as a hard prerequisite', because a
    consumer loads those into context just the same. Order preserved, deduped."""
    out: list[str] = []
    for h in hits:
        if h.slug not in out:
            out.append(h.slug)
    return out


def load_route_fn(router_path: Path | None = None) -> RouteFn:
    if router_path is None:
        import router  # noqa: PLC0415  (scripts/ is on sys.path)
        return router.route
    spec = importlib.util.spec_from_file_location("router_under_test", router_path)
    if spec is None or spec.loader is None:
        raise SystemExit(f"cannot import router from {router_path}")
    mod = importlib.util.module_from_spec(spec)
    # Register before exec: @dataclass resolves its module via sys.modules,
    # so an unregistered module crashes on the router's RouteHit dataclass.
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod.route


@contextlib.contextmanager
def cached_skill_loading():
    """The router re-reads every SKILL.md on each call (fine for one CLI query,
    ~15s for a full benchmark run). Skills cannot change mid-evaluation, so
    memoise the loader for the duration of the run; routing logic is untouched."""
    original = sl.load_all_skills
    cached = functools.lru_cache(maxsize=None)(original)
    sl.load_all_skills = cached
    try:
        yield
    finally:
        sl.load_all_skills = original


def prerequisite_hits(hits: list[Any], requires: dict[str, list[str]]) -> list[str]:
    """Selected slugs that are there as a hard prerequisite: either the router
    said so in `reasons`, or the slug is a declared prerequisite of another
    selected skill. The router orders prerequisites before their dependent even
    when they also scored on their own, so top-1 needs this to look past them."""
    sel = selected_slugs(hits)
    needed: set[str] = set()
    for slug in sel:
        needed |= prerequisite_closure(slug, requires)
    out: list[str] = []
    for h in hits:
        if h.slug in out:
            continue
        if h.slug in needed or any(PREREQ_REASON in str(r) for r in (getattr(h, "reasons", None) or [])):
            out.append(h.slug)
    return out


def is_top1_correct(selected: list[str], expected: Iterable[str], prerequisites: Iterable[str] = ()) -> bool:
    """The first selected skill is expected, skipping leading hits that are
    prerequisites (a prerequisite that is itself expected still counts)."""
    exp, pre = set(expected), set(prerequisites)
    for slug in selected:
        if slug in exp:
            return True
        if slug not in pre:
            return False
    return False


def run_cases(cases: list[dict], route_fn: RouteFn, top_k: int,
              requires: dict[str, list[str]] | None = None) -> list[dict]:
    results = []
    with cached_skill_loading():
        if requires is None:
            requires = requires_map(sl.load_all_skills())
        for case in cases:
            _intent, hits = route_fn(case["query"], top_k=top_k)
            hits = list(hits)
            results.append({
                "id": case["id"],
                "category": case["category"],
                "split": case["split"],
                "selected": selected_slugs(hits),
                "prerequisites": prerequisite_hits(hits, requires),
                "expected": list(case["expected"]),
                "acceptable": list(case["acceptable"]),
                "forbidden": list(case["forbidden"]),
            })
    return results


# --------------------------------------------------------------------------- metrics

def case_outcome(r: dict) -> tuple[bool, list[str]]:
    """Task-level success for one case, plus the reasons it failed (if any).

    Success = every expected skill selected AND no forbidden skill selected AND,
    when nothing is expected, nothing outside `acceptable` selected (for
    no-skill cases `acceptable` is empty, so that means nothing selected)."""
    sel = set(r["selected"])
    reasons = []
    missing = [s for s in r["expected"] if s not in sel]
    if missing:
        reasons.append(f"missing {missing}")
    forbidden = [s for s in r["selected"] if s in set(r["forbidden"])]
    if forbidden:
        reasons.append(f"forbidden {forbidden}")
    if not r["expected"]:
        stray = [s for s in r["selected"] if s not in set(r["acceptable"])]
        if stray:
            reasons.append(f"nothing expected, {len(stray)} unacceptable selected")
    return not reasons, reasons


def _ratio(num: float, den: float) -> float | None:
    # None (shown as "n/a") rather than a made-up 0 or 1 when undefined.
    return round(num / den, 4) if den else None


def compute_metrics(results: list[dict]) -> dict:
    sel_total = sel_ok = exp_total = exp_hit = 0
    top1_n = top1_hit = task_ok = 0
    noskill_n = noskill_fa = forb_n = forb_hit = unnecessary = 0
    for r in results:
        sel = r["selected"]
        exp, acc, forb = set(r["expected"]), set(r["acceptable"]), set(r["forbidden"])
        sel_total += len(sel)
        sel_ok += sum(1 for s in sel if s in exp or s in acc)
        unnecessary += sum(1 for s in sel if s not in exp and s not in acc)
        exp_total += len(exp)
        exp_hit += sum(1 for s in exp if s in sel)
        if exp:
            top1_n += 1
            top1_hit += is_top1_correct(sel, exp, r.get("prerequisites", ()))
        if r["category"] == "no-skill":
            noskill_n += 1
            noskill_fa += bool(sel)
        if forb:
            forb_n += 1
            forb_hit += any(s in forb for s in sel)
        task_ok += case_outcome(r)[0]
    n = len(results)
    return {
        "n_cases": n,
        "precision": _ratio(sel_ok, sel_total),
        "recall": _ratio(exp_hit, exp_total),
        "top1_accuracy": _ratio(top1_hit, top1_n),
        "task_accuracy": _ratio(task_ok, n),
        "false_activation_rate": _ratio(noskill_fa, noskill_n),
        "mean_unnecessary_activations": _ratio(unnecessary, n),
        "forbidden_selection_rate": _ratio(forb_hit, forb_n),
        "mean_selected": _ratio(sel_total, n),
        "counts": {
            "selected": sel_total, "selected_correct": sel_ok, "expected": exp_total,
            "expected_selected": exp_hit, "top1_cases": top1_n, "top1_correct": top1_hit,
            "task_correct": task_ok, "no_skill_cases": noskill_n, "no_skill_activated": noskill_fa,
            "forbidden_cases": forb_n, "forbidden_violated": forb_hit, "unnecessary": unnecessary,
        },
    }


def evaluate(cases: list[dict], route_fn: RouteFn, top_k: int = 5, split: str = "all",
             requires: dict[str, list[str]] | None = None) -> dict:
    """`requires` (slug -> hard prerequisites) defaults to the repo's skills."""
    if top_k < 1:
        raise ValueError(f"top_k must be >= 1, got {top_k}")
    if split != "all":
        cases = [c for c in cases if c["split"] == split]
    results = run_cases(cases, route_fn, top_k, requires)
    by_split = {s: compute_metrics([r for r in results if r["split"] == s]) for s in SPLITS}
    by_split["all"] = compute_metrics(results)
    if split != "all":
        by_split = {split: by_split[split]}
    categories = {cat: compute_metrics([r for r in results if r["category"] == cat]) for cat in CATEGORIES}
    misrouted = []
    for r in results:
        ok, reasons = case_outcome(r)
        if not ok:
            misrouted.append({"id": r["id"], "category": r["category"], "split": r["split"],
                              "selected": r["selected"], "reasons": reasons})
    return {"top_k": top_k, "split": split, "splits": by_split, "categories": categories,
            "misrouted": misrouted, "cases": results}


# --------------------------------------------------------------------------- thresholds

THRESHOLD_FLAGS = {
    "min_precision": ("precision", "min"),
    "min_recall": ("recall", "min"),
    "min_top1": ("top1_accuracy", "min"),
    "min_task_accuracy": ("task_accuracy", "min"),
    "max_false_activation": ("false_activation_rate", "max"),
    "max_unnecessary": ("mean_unnecessary_activations", "max"),
    "max_forbidden_rate": ("forbidden_selection_rate", "max"),
}


def check_thresholds(metrics: dict, thresholds: dict[str, float | None]) -> list[str]:
    failures = []
    for flag, limit in thresholds.items():
        if limit is None:
            continue
        metric, kind = THRESHOLD_FLAGS[flag]
        val = metrics.get(metric)
        if val is None:
            # An undefined metric can't demonstrate it meets a bar.
            failures.append(f"{metric} is undefined (n/a); --{flag.replace('_', '-')} {limit} cannot be met")
        elif kind == "min" and val < limit:
            failures.append(f"{metric} = {val} < required minimum {limit}")
        elif kind == "max" and val > limit:
            failures.append(f"{metric} = {val} > allowed maximum {limit}")
    return failures


# --------------------------------------------------------------------------- output

def _fmt(v: float | None) -> str:
    return "n/a" if v is None else f"{v:.3f}"


COLUMNS = (("n", "n_cases"), ("prec", "precision"), ("recall", "recall"), ("top1", "top1_accuracy"),
           ("task", "task_accuracy"), ("falseAct", "false_activation_rate"),
           ("unneeded", "mean_unnecessary_activations"), ("forbid", "forbidden_selection_rate"),
           ("sel/q", "mean_selected"))


def _table(title: str, rows: list[tuple[str, dict]]) -> list[str]:
    head = f"{title:14s}" + "".join(f"{h:>10s}" for h, _ in COLUMNS)
    lines = [head, "-" * len(head)]
    for name, m in rows:
        cells = [str(m[k]) if k == "n_cases" else _fmt(m[k]) for _, k in COLUMNS]
        lines.append(f"{name:14s}" + "".join(f"{c:>10s}" for c in cells))
    return lines


def render_text(report: dict) -> str:
    lines = [f"router benchmark: top_k={report['top_k']} split={report['split']}", ""]
    lines += _table("split", list(report["splits"].items()))
    lines.append("")
    lines += _table("category", [(c, report["categories"][c]) for c in CATEGORIES
                                 if report["categories"][c]["n_cases"]])
    lines.append("")
    lines.append(f"misrouted cases ({len(report['misrouted'])}):")
    for m in report["misrouted"]:
        lines.append(f"  {m['id']:8s} [{m['split']}] {'; '.join(m['reasons'])}  selected={m['selected']}")
    return "\n".join(lines)


def build_baseline(bench_path: Path, route_fn: RouteFn, top_k: int, date: str, note: str, command: str) -> dict:
    bench = load_benchmark(bench_path)
    report = evaluate(bench["cases"], route_fn, top_k=top_k, split="all")
    return {
        "note": note,
        "date": date,
        "command": command,
        "top_k": top_k,
        "benchmark_sha256": benchmark_sha256(bench_path),
        "metrics": {s: {k: report["splits"][s][k] for k in HEADLINE_METRICS} for s in (*SPLITS, "all")},
    }


def _positive_int(text: str) -> int:
    # A negative top_k would silently slice to "all but the last N" hits and 0
    # selects nothing (trivially passing --max-false-activation), so reject both.
    try:
        val = int(text)
    except ValueError:
        raise argparse.ArgumentTypeError(f"invalid int value: {text!r}") from None
    if val < 1:
        raise argparse.ArgumentTypeError(f"must be >= 1, got {val}")
    return val


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--benchmark", type=Path, default=BENCHMARK_PATH)
    ap.add_argument("--router", type=Path, default=None,
                    help="path to an alternative router module exposing route(task, top_k=...)")
    ap.add_argument("--top-k", type=_positive_int, default=5)
    ap.add_argument("--split", choices=("dev", "test", "all"), default="all")
    ap.add_argument("--json", action="store_true", help="print the full report as JSON")
    ap.add_argument("--show-cases", action="store_true", help="include per-case results in --json output")
    for flag in THRESHOLD_FLAGS:
        ap.add_argument("--" + flag.replace("_", "-"), dest=flag, type=float, default=None)
    ap.add_argument("--write-baseline", type=Path, default=None,
                    help="write headline metrics for dev/test/all to this JSON file (re-baselining)")
    ap.add_argument("--baseline-note", default="")
    ap.add_argument("--baseline-date", default=datetime.date.today().isoformat())
    args = ap.parse_args(argv)

    try:
        bench = load_benchmark(args.benchmark)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        print(f"benchmark error: cannot read {args.benchmark}: {exc}", file=sys.stderr)
        return 2
    skills = sl.load_all_skills()
    valid = {s.slug for s in skills}
    # Coverage checks assume a structurally valid file, so run them only after
    # per-case validation passes (otherwise they could crash and exit 1, not 2).
    problems = validate_benchmark(bench, valid, requires_map(skills)) or coverage_errors(bench, valid)
    if problems:
        for p in problems:
            print(f"benchmark error: {p}", file=sys.stderr)
        return 2

    route_fn = load_route_fn(args.router)

    if args.write_baseline:
        cmd = shlex.join(["python", "scripts/eval_router.py", *(argv if argv is not None else sys.argv[1:])])
        base = build_baseline(args.benchmark, route_fn, args.top_k, args.baseline_date, args.baseline_note, cmd)
        args.write_baseline.write_text(json.dumps(base, indent=2) + "\n", encoding="utf-8", newline="\n")
        print(f"wrote {args.write_baseline}")

    report = evaluate(bench["cases"], route_fn, top_k=args.top_k, split=args.split)
    failures = check_thresholds(report["splits"][args.split],
                                {f: getattr(args, f) for f in THRESHOLD_FLAGS})

    if args.json:
        out = dict(report)
        if not args.show_cases:
            out.pop("cases")
        out["threshold_failures"] = failures
        print(json.dumps(out, indent=2, sort_keys=True))
    else:
        print(render_text(report))
        if failures:
            print("\nTHRESHOLD FAILURES:")
            for f in failures:
                print(f"  {f}")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
