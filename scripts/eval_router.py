#!/usr/bin/env python3
"""Score scripts/router.py against the labelled routing benchmark.

Reads evals/routing/benchmark.json (see evals/routing/README.md for the label
guidelines and exact metric definitions), runs every case through the
router's public `route(task, top_k=...)` interface, and reports precision,
recall, top-1 accuracy, task-level selection accuracy, false-activation rate
(no-skill category, and over every case that expects no skill), unnecessary
activations, forbidden-selection rate, abstention, no-skill precision/recall
and 95% Wilson intervals — per split and per category — plus per-skill
precision/recall, a selection confusion matrix and the ids of every misrouted
case.

`--gate` runs the CI routing gate (integrity, this commit's baseline, and no
regression against a base commit's benchmark + baseline); see run_gate().

Deterministic: no randomness, no network, no model calls. Output ordering is
fixed (benchmark order for cases, fixed category/split order for tables).

Usage:
    python scripts/eval_router.py                       # human table, all cases
    python scripts/eval_router.py --split test --json   # machine-readable
    python scripts/eval_router.py --min-task-accuracy 0.6 --max-false-activation 0.2
    python scripts/eval_router.py --router path/to/other_router.py   # A/B a variant
    python scripts/eval_router.py --gate --base-sha "$BASE_SHA" --event-name push   # CI gate

Exit codes: 0 = ok, 1 = a --min-*/--max-* threshold failed (or the --gate
failed), 2 = the benchmark file is missing/unreadable/malformed, or a CLI
usage error.
"""
from __future__ import annotations

import argparse
import contextlib
import datetime
import functools
import hashlib
import importlib.util
import json
import math
import os
import re
import shlex
import subprocess
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
# Paraphrase family and label provenance. Optional in validate_benchmark() so an
# older benchmark (a base commit's, in the CI gate) still validates per case;
# split_integrity_errors() requires the first three on the current benchmark.
PROVENANCE_KEYS = ("family", "labellers", "adjudication")
OPTIONAL_CASE_KEYS = (*PROVENANCE_KEYS, "label_review")
#: single-labeller = one person labelled it, nobody reviewed it; agreed = independent
#: reviewers matched the labels; adjudicated = reviewers disagreed and the labels were
#: settled; disputed = disagreement recorded in label_review.unresolved, not settled.
ADJUDICATION_STATUSES = ("single-labeller", "agreed", "adjudicated", "disputed")
LABEL_REVIEW_KEYS = ("reviewers", "agreement", "unresolved")
AGREEMENT_LEVELS = ("full", "partial", "none")
FAMILY_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
LABEL_KEYS = ("expected", "acceptable", "forbidden")
PREREQ_REASON = "pulled in as a hard prerequisite"
NONE_LABEL = "(none)"  # confusion-matrix row/column for "nothing expected" / "nothing selected"

# Coverage requirements the benchmark must keep satisfying as it is edited.
MIN_CASES = 150
MIN_PER_CATEGORY = 15
MIN_EXPECTED_PER_SKILL = 2
# Each split must exercise every skill at least once, or a held-out number
# says nothing about the skills it never asks for.
MIN_EXPECTED_PER_SKILL_PER_SPLIT = 1
TEST_FRACTION_RANGE = (0.35, 0.45)

# Direction of each headline metric, used by thresholds, the regression floor and
# the CI gate. negative_false_activation_rate counts every case with an empty
# `expected` (adversarial homonyms included), so a recall gain bought by
# activating more often on off-topic text fails the floor.
HIGHER_IS_BETTER = ("precision", "recall", "top1_accuracy", "task_accuracy")
LOWER_IS_BETTER = ("false_activation_rate", "mean_unnecessary_activations", "forbidden_selection_rate",
                   "negative_false_activation_rate")
HEADLINE_METRICS = HIGHER_IS_BETTER + LOWER_IS_BETTER
# Proportions reported with a 95% Wilson score interval: metric -> (successes, trials) count keys.
PROPORTIONS = {
    "precision": ("selected_correct", "selected"),
    "recall": ("expected_selected", "expected"),
    "top1_accuracy": ("top1_correct", "top1_cases"),
    "task_accuracy": ("task_correct", "cases"),
    "false_activation_rate": ("no_skill_activated", "no_skill_cases"),
    "negative_false_activation_rate": ("negatives_activated", "negative_cases"),
    "forbidden_selection_rate": ("forbidden_violated", "forbidden_cases"),
    "abstention_rate": ("abstained", "cases"),
    "positive_abstention_rate": ("positives_abstained", "positive_cases"),
    "no_skill_precision": ("abstained_on_negative", "abstained"),
    "no_skill_recall": ("abstained_on_negative", "negative_cases"),
}
WILSON_Z = 1.959964  # two-sided 95%

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
        extra = sorted(set(case) - set(CASE_KEYS) - set(OPTIONAL_CASE_KEYS))
        if missing:
            errors.append(f"{where}: missing keys {missing}")
        if extra:
            errors.append(f"{where}: unknown keys {extra}")
        errors.extend(f"{where}: {e}" for e in _provenance_field_errors(case))
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


def _is_str_list(val: Any, nonempty: bool = False) -> bool:
    return (isinstance(val, list) and all(isinstance(s, str) and s.strip() for s in val)
            and len(set(val)) == len(val) and (bool(val) or not nonempty))


def _provenance_field_errors(case: dict) -> list[str]:
    """Shape of the optional family / label-provenance fields, when present."""
    errors: list[str] = []
    fam = case.get("family")
    if "family" in case and not (isinstance(fam, str) and FAMILY_RE.match(fam)):
        errors.append(f"'family' must be a lowercase kebab-case string, got {fam!r}")
    if "labellers" in case and not _is_str_list(case["labellers"], nonempty=True):
        errors.append("'labellers' must be a non-empty list of distinct labeller ids")
    status = case.get("adjudication")
    if "adjudication" in case and status not in ADJUDICATION_STATUSES:
        errors.append(f"'adjudication' must be one of {list(ADJUDICATION_STATUSES)}, got {status!r}")
    review = case.get("label_review")
    if "label_review" not in case:
        if status in ("agreed", "adjudicated", "disputed"):
            errors.append(f"adjudication {status!r} needs a 'label_review' record")
        return errors
    if not isinstance(review, dict) or set(review) != set(LABEL_REVIEW_KEYS):
        return errors + [f"'label_review' must be an object with exactly the keys {list(LABEL_REVIEW_KEYS)}"]
    if not _is_str_list(review["reviewers"], nonempty=True):
        errors.append("'label_review.reviewers' must be a non-empty list of distinct reviewer ids")
    agreement = review["agreement"]
    if not (agreement in AGREEMENT_LEVELS or (isinstance(agreement, (int, float)) and not isinstance(agreement, bool)
                                              and 0 <= agreement <= 1)):
        errors.append(f"'label_review.agreement' must be one of {list(AGREEMENT_LEVELS)} or a number in [0, 1]")
    if not _is_str_list(review["unresolved"]):
        errors.append("'label_review.unresolved' must be a list of distinct strings")
    elif review["unresolved"] and status != "disputed":
        errors.append("'label_review.unresolved' is non-empty, so adjudication must be 'disputed'")
    elif status == "disputed" and not review["unresolved"]:
        errors.append("adjudication 'disputed' needs at least one 'label_review.unresolved' entry")
    if status == "single-labeller":
        errors.append("a reviewed case ('label_review' present) cannot be 'single-labeller'")
    if status == "agreed" and agreement != "full" and agreement != 1:
        errors.append("adjudication 'agreed' needs 'label_review.agreement' of 'full' (or 1)")
    return errors


def split_integrity_errors(bench: Any) -> list[str]:
    """Requirements for the current benchmark that an older (base-commit) benchmark
    need not meet: every case names its paraphrase `family` and its label
    provenance, and no family spans dev and test (a family with any dev case is
    a tuning family, so its paraphrases cannot be held out). Assumes
    validate_benchmark() passed."""
    cases = bench.get("cases") if isinstance(bench, dict) else None
    if not isinstance(cases, list) or not all(isinstance(c, dict) for c in cases):
        return ["benchmark structure invalid; fix validate_benchmark() errors first"]
    errors: list[str] = []
    splits_of: dict[str, dict[str, list[str]]] = {}
    for c in cases:
        missing = [k for k in PROVENANCE_KEYS if k not in c]
        if missing:
            errors.append(f"{c.get('id')}: missing keys {missing}")
        if isinstance(c.get("family"), str):
            splits_of.setdefault(c["family"], {}).setdefault(str(c.get("split")), []).append(str(c.get("id")))
    for fam, by_split in sorted(splits_of.items()):
        if len(by_split) > 1:
            parts = "; ".join(f"{s}: {', '.join(ids)}" for s, ids in sorted(by_split.items()))
            errors.append(f"family {fam!r} spans splits ({parts}); move the whole family to dev")
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
                "family": case.get("family") or case["id"],
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


def wilson_interval(k: int, n: int, z: float = WILSON_Z) -> tuple[float | None, float | None]:
    """Wilson score interval for k successes in n trials (Wilson 1927). (None, None) when n == 0."""
    if n <= 0:
        return None, None
    p = k / n
    denom = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / denom
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / denom
    return round(max(0.0, centre - half), 4), round(min(1.0, centre + half), 4)


def compute_metrics(results: list[dict]) -> dict:
    """Micro-averaged metrics over `results` (see evals/routing/README.md for each definition)."""
    sel_total = sel_ok = exp_total = exp_hit = 0
    top1_n = top1_hit = task_ok = 0
    noskill_n = noskill_fa = forb_n = forb_hit = unnecessary = 0
    neg_n = neg_fa = abstained = abstained_neg = pos_n = pos_abstained = 0
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
            pos_n += 1
            pos_abstained += not sel
        else:
            # Every case that needs no skill, whatever its category: activating means
            # selecting something its `acceptable` list does not tolerate.
            neg_n += 1
            neg_fa += any(s not in acc for s in sel)
            abstained_neg += not sel
        abstained += not sel
        if r["category"] == "no-skill":
            noskill_n += 1
            noskill_fa += bool(sel)
        if forb:
            forb_n += 1
            forb_hit += any(s in forb for s in sel)
        task_ok += case_outcome(r)[0]
    n = len(results)
    counts = {
        "cases": n, "selected": sel_total, "selected_correct": sel_ok, "expected": exp_total,
        "expected_selected": exp_hit, "top1_cases": top1_n, "top1_correct": top1_hit,
        "task_correct": task_ok, "no_skill_cases": noskill_n, "no_skill_activated": noskill_fa,
        "forbidden_cases": forb_n, "forbidden_violated": forb_hit, "unnecessary": unnecessary,
        "negative_cases": neg_n, "negatives_activated": neg_fa, "abstained": abstained,
        "abstained_on_negative": abstained_neg, "positive_cases": pos_n, "positives_abstained": pos_abstained,
    }
    intervals = {}
    for metric, (k_key, n_key) in PROPORTIONS.items():
        low, high = wilson_interval(counts[k_key], counts[n_key])
        intervals[metric] = {"k": counts[k_key], "n": counts[n_key], "low": low, "high": high}
    return {
        "n_cases": n,
        "n_families": len({r.get("family") or r["id"] for r in results}),
        "precision": _ratio(sel_ok, sel_total),
        "recall": _ratio(exp_hit, exp_total),
        "top1_accuracy": _ratio(top1_hit, top1_n),
        "task_accuracy": _ratio(task_ok, n),
        "false_activation_rate": _ratio(noskill_fa, noskill_n),
        "negative_false_activation_rate": _ratio(neg_fa, neg_n),
        "mean_unnecessary_activations": _ratio(unnecessary, n),
        "forbidden_selection_rate": _ratio(forb_hit, forb_n),
        "abstention_rate": _ratio(abstained, n),
        "positive_abstention_rate": _ratio(pos_abstained, pos_n),
        "no_skill_precision": _ratio(abstained_neg, abstained),
        "no_skill_recall": _ratio(abstained_neg, neg_n),
        "mean_selected": _ratio(sel_total, n),
        "intervals": intervals,
        "counts": counts,
    }


def per_class_metrics(results: list[dict]) -> dict[str, dict]:
    """Per skill: `support` = cases expecting it; `recall` = share of those that selected
    it; `precision` = share of the cases selecting it where it was expected or
    acceptable (the per-skill form of the headline precision); `forbidden_selected` =
    cases that selected it although it was forbidden."""
    slugs = sorted({s for r in results for key in ("expected", "acceptable", "forbidden", "selected")
                    for s in r[key]})
    out: dict[str, dict] = {}
    for slug in slugs:
        support = sum(1 for r in results if slug in r["expected"])
        chosen = [r for r in results if slug in r["selected"]]
        ok = sum(1 for r in chosen if slug in r["expected"] or slug in r["acceptable"])
        hit = sum(1 for r in chosen if slug in r["expected"])
        out[slug] = {"support": support, "selected": len(chosen), "selected_correct": ok,
                     "expected_selected": hit, "precision": _ratio(ok, len(chosen)), "recall": _ratio(hit, support),
                     "forbidden_selected": sum(1 for r in chosen if slug in r["forbidden"])}
    return out


def confusion_matrix(results: list[dict]) -> dict[str, dict[str, int]]:
    """Multi-label selection confusion: for each case, every expected skill (or
    NONE_LABEL when nothing is expected) is a row, and every selected skill (or
    NONE_LABEL when nothing was selected) adds 1 to that row's column. A perfect
    router puts all mass on columns that are the row itself, a skill co-expected
    in the same case (a correct multi-skill selection of [a, b] adds a->b and
    b->a), an acceptable skill, or a prerequisite."""
    matrix: dict[str, dict[str, int]] = {}
    for r in results:
        for row in r["expected"] or [NONE_LABEL]:
            cells = matrix.setdefault(row, {})
            for col in r["selected"] or [NONE_LABEL]:
                cells[col] = cells.get(col, 0) + 1
    return {row: dict(sorted(cols.items())) for row, cols in sorted(matrix.items())}


def top_confusions(results: list[dict], limit: int = 10) -> list[dict]:
    """The most frequent (expected-or-NONE_LABEL -> selected) pairs where the selected
    skill was neither expected nor acceptable, plus missed expected skills
    (-> NONE_LABEL when the case selected nothing)."""
    pairs: dict[tuple[str, str], list[str]] = {}
    for r in results:
        ok = set(r["expected"]) | set(r["acceptable"])
        wrong = [s for s in r["selected"] if s not in ok]
        for row in r["expected"] or [NONE_LABEL]:
            for col in wrong or ([NONE_LABEL] if not r["selected"] and r["expected"] else []):
                pairs.setdefault((row, col), []).append(r["id"])
    ranked = sorted(pairs.items(), key=lambda kv: (-len(kv[1]), kv[0]))[:limit]
    return [{"expected": e, "selected": s, "count": len(ids), "ids": ids} for (e, s), ids in ranked]


def evaluate(cases: list[dict], route_fn: RouteFn, top_k: int = 5, split: str = "all",
             requires: dict[str, list[str]] | None = None) -> dict:
    """`requires` (slug -> hard prerequisites) defaults to the repo's skills."""
    if top_k < 1:
        raise ValueError(f"top_k must be >= 1, got {top_k}")
    if split != "all":
        cases = [c for c in cases if c["split"] == split]
    results = run_cases(cases, route_fn, top_k, requires)
    scoped = {s: [r for r in results if r["split"] == s] for s in SPLITS}
    scoped["all"] = results
    if split != "all":
        scoped = {split: scoped[split]}
    by_split = {s: compute_metrics(rs) for s, rs in scoped.items()}
    categories = {cat: compute_metrics([r for r in results if r["category"] == cat]) for cat in CATEGORIES}
    split_categories = {s: {cat: compute_metrics([r for r in rs if r["category"] == cat]) for cat in CATEGORIES}
                        for s, rs in scoped.items() if s != "all"}
    misrouted = []
    for r in results:
        ok, reasons = case_outcome(r)
        if not ok:
            misrouted.append({"id": r["id"], "category": r["category"], "split": r["split"],
                              "selected": r["selected"], "reasons": reasons})
    return {"top_k": top_k, "split": split, "splits": by_split, "categories": categories,
            "split_categories": split_categories,
            "per_class": {s: per_class_metrics(rs) for s, rs in scoped.items()},
            "confusion": {s: confusion_matrix(rs) for s, rs in scoped.items()},
            "top_confusions": {s: top_confusions(rs) for s, rs in scoped.items()},
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
    "max_negative_false_activation": ("negative_false_activation_rate", "max"),
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
           ("negFA", "negative_false_activation_rate"),
           ("unneeded", "mean_unnecessary_activations"), ("forbid", "forbidden_selection_rate"),
           ("abstain", "abstention_rate"), ("sel/q", "mean_selected"))
CI_ROWS = ("precision", "recall", "top1_accuracy", "task_accuracy", "false_activation_rate",
           "negative_false_activation_rate", "forbidden_selection_rate", "abstention_rate",
           "positive_abstention_rate", "no_skill_precision", "no_skill_recall")


def _table(title: str, rows: list[tuple[str, dict]]) -> list[str]:
    head = f"{title:14s}" + "".join(f"{h:>10s}" for h, _ in COLUMNS)
    lines = [head, "-" * len(head)]
    for name, m in rows:
        cells = [str(m[k]) if k == "n_cases" else _fmt(m[k]) for _, k in COLUMNS]
        lines.append(f"{name:14s}" + "".join(f"{c:>10s}" for c in cells))
    return lines


def _ci_table(name: str, m: dict) -> list[str]:
    lines = [f"{name}: {m['n_cases']} cases in {m['n_families']} families; 95% Wilson intervals "
             "(cases in one family are correlated, so treat these as optimistic)"]
    for metric in CI_ROWS:
        iv = m["intervals"][metric]
        span = "n/a" if iv["low"] is None else f"[{iv['low']:.3f}, {iv['high']:.3f}]"
        lines.append(f"  {metric:32s} {_fmt(m[metric]):>7s}  {iv['k']:>4d}/{iv['n']:<4d} {span}")
    return lines


def render_text(report: dict) -> str:
    lines = [f"router benchmark: top_k={report['top_k']} split={report['split']}", ""]
    lines += _table("split", list(report["splits"].items()))
    lines.append("")
    lines += _table("category", [(c, report["categories"][c]) for c in CATEGORIES
                                 if report["categories"][c]["n_cases"]])
    lines.append("")
    for name, m in report["splits"].items():
        lines += _ci_table(name, m)
        lines.append("")
    scope = report["split"]
    lines.append(f"per-skill [{scope}] (support = cases expecting it):")
    lines.append(f"  {'skill':28s} {'support':>7s} {'recall':>7s} {'sel':>5s} {'prec':>7s} {'forbid':>6s}")
    for slug, pc in report["per_class"][scope].items():
        lines.append(f"  {slug:28s} {pc['support']:>7d} {_fmt(pc['recall']):>7s} {pc['selected']:>5d} "
                     f"{_fmt(pc['precision']):>7s} {pc['forbidden_selected']:>6d}")
    lines.append("")
    lines.append(f"top confusions [{scope}] (expected -> wrongly selected, or -> {NONE_LABEL} when nothing was):")
    for c in report["top_confusions"][scope]:
        lines.append(f"  {c['expected']:28s} -> {c['selected']:28s} x{c['count']}  ({', '.join(c['ids'])})")
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


# --------------------------------------------------------------------------- CI gate

ZERO_SHA = "0" * 40
GIT_TIMEOUT = 60  # seconds per git call
PULL_REQUEST_EVENTS = ("pull_request", "pull_request_target")


class GateError(Exception):
    """The gate cannot run as configured (for example, the base commit is not in the clone)."""


def _git(root: Path, *args: str) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(["git", *args], cwd=root, capture_output=True, timeout=GIT_TIMEOUT)
    except FileNotFoundError as exc:
        raise GateError("git is not installed") from exc
    except subprocess.TimeoutExpired as exc:
        raise GateError(f"git {' '.join(args)} timed out after {GIT_TIMEOUT}s") from exc


def _rev(root: Path, rev: str) -> str | None:
    r = _git(root, "rev-parse", "--verify", "--quiet", f"{rev}^{{commit}}")
    return r.stdout.decode().strip() if r.returncode == 0 else None


def _is_ancestor(root: Path, a: str, b: str) -> bool:
    return _git(root, "merge-base", "--is-ancestor", a, b).returncode == 0


def resolve_base(root: Path, base_sha: str | None = None, event_name: str | None = None,
                 head_sha: str | None = None) -> tuple[str | None, str]:
    """The commit whose benchmark + baseline this commit must not regress against,
    and a one-line description of how it was chosen.

    * pull_request with HEAD a merge commit (GitHub's refs/pull/N/merge): its
      first parent, the base-branch side of the test merge. When `base_sha`
      (github.event.pull_request.base.sha) is given, it must be contained in that
      parent, so the "first parent is the base tip" assumption is checked rather
      than trusted; if it is not, the explicit base is used instead.
      Which merge commit this is can only be told with `head_sha`
      (github.event.pull_request.head.sha): HEAD is the synthetic merge only if
      its second parent is the PR head. If HEAD *is* the PR head (a head-only
      checkout, `ref: head.sha`), it is not the test merge even when it is a
      merge commit of the author's own, and the explicit-base rules below apply.
      Without `head_sha` every merge commit in a pull_request event is assumed to
      be the synthetic merge; a head-only checkout of a PR whose head is the
      author's merge commit, and whose first parent already contains base.sha,
      would then compare against that first parent instead of the base.
    * any other event with an explicit `base_sha` (push: github.event.before):
      that commit if it is an ancestor of HEAD, so every commit of a multi-commit
      push is covered; otherwise (force push, or a head-only PR checkout whose
      base moved on) the merge base of the two.
    * no usable explicit base (none given, or the all-zero SHA of a newly
      created branch): the first parent of HEAD, or None for a root commit.

    Raises GateError when an explicit base or head is not in the clone, when the
    base shares no history with HEAD, or when HEAD is neither the PR head nor a
    merge of it: a requested comparison must not be skipped silently."""
    explicit = (base_sha or "").strip()
    usable = bool(explicit) and set(explicit) != {"0"}
    head = _rev(root, "HEAD")
    if head is None:
        raise GateError("HEAD is not a commit")
    first_parent = _rev(root, "HEAD^1")
    second_parent = _rev(root, "HEAD^2")
    base = None
    if usable:
        base = _rev(root, explicit)
        if base is None:
            raise GateError(f"base commit {explicit} is not in this clone; fetch it first "
                            f"(actions/checkout fetch-depth: 0, or git fetch origin {explicit})")
    synthetic_merge = bool(first_parent and second_parent)
    if event_name in PULL_REQUEST_EVENTS and (head_sha or "").strip():
        pr_head = _rev(root, head_sha.strip())
        if pr_head is None:
            raise GateError(f"pull-request head {head_sha.strip()} is not in this clone")
        if pr_head == head:
            synthetic_merge = False  # head-only checkout: HEAD is the PR head itself
        elif second_parent != pr_head:
            raise GateError(f"HEAD {head[:12]} is neither the pull-request head {pr_head[:12]} nor a merge of it")
    if event_name in PULL_REQUEST_EVENTS and synthetic_merge:
        if base is None:
            return first_parent, f"first parent of the pull-request merge commit ({first_parent[:12]})"
        if _is_ancestor(root, base, first_parent):
            how = "is" if base == first_parent else "contains"
            return first_parent, (f"first parent of the pull-request merge commit ({first_parent[:12]}), which "
                                  f"{how} the explicit base {base[:12]}")
        # fall through: the merge commit's first parent is not the base side
    if base is not None:
        if base == head:
            return base, f"explicit base {base[:12]} (same as HEAD)"
        if _is_ancestor(root, base, head):
            return base, f"explicit base {base[:12]}"
        r = _git(root, "merge-base", base, head)
        merge_base = r.stdout.decode().strip() if r.returncode == 0 else ""
        if not merge_base:
            raise GateError(f"base commit {base[:12]} shares no history with HEAD")
        return merge_base, (f"merge base {merge_base[:12]} of HEAD and the explicit base {base[:12]} "
                            "(the base is not an ancestor of HEAD)")
    note = " (the explicit base was the all-zero SHA of a new branch)" if explicit and not usable else ""
    if first_parent is None:
        return None, "no parent commit" + note
    kind = "merge commit" if second_parent else "commit"
    return first_parent, f"first parent of HEAD ({first_parent[:12]}, HEAD is a {kind}){note}"


def _show(root: Path, sha: str, rel: str) -> bytes | None:
    r = _git(root, "show", f"{sha}:{rel}")
    return r.stdout if r.returncode == 0 else None


def adapt_parent_benchmark(bench: dict, skills: list[sl.Skill]) -> tuple[dict, list[str], list[str]]:
    """A base commit's benchmark with its labels carried over to this tree: a case
    expecting a skill that no longer exists, or a conflicting case whose forbidden
    skills are all gone, is dropped; removed slugs leave acceptable/forbidden; a hard
    prerequisite this tree added becomes acceptable unless the base forbade it.
    Returns (adapted benchmark, dropped ids, relabelled ids)."""
    valid, requires = {s.slug for s in skills}, requires_map(skills)
    kept, dropped, relabelled = [], [], []
    for case in bench.get("cases") or []:
        if not isinstance(case, dict) or not all(isinstance(case.get(k), list) for k in LABEL_KEYS):
            kept.append(case)  # malformed: validate_benchmark reports it
            continue
        if any(s not in valid for s in case["expected"]) or (
                case.get("category") == "conflicting" and case["forbidden"]
                and not any(s in valid for s in case["forbidden"])):
            dropped.append(str(case.get("id")))
            continue
        case = {**case, "acceptable": [s for s in case["acceptable"] if s in valid],
                "forbidden": [s for s in case["forbidden"] if s in valid]}
        labelled = set(case["expected"]) | set(case["acceptable"])
        new = sorted({p for s in labelled for p in prerequisite_closure(s, requires)} - labelled
                     - set(case["forbidden"]))
        if new:
            case["acceptable"] = case["acceptable"] + new
            relabelled.append(str(case.get("id")))
        kept.append(case)
    return {**bench, "cases": kept}, dropped, relabelled


ACCEPTED_REGRESSION_KEYS = ("base_benchmark_sha256", "metric", "floor", "accepted_value", "cases", "reason")


def accepted_regression_errors(baseline: dict) -> list[str]:
    """Problems with baseline.json's optional `accepted_regressions` list (see accept_regressions)."""
    entries = baseline.get("accepted_regressions", [])
    if not isinstance(entries, list):
        return ["'accepted_regressions' must be a list"]
    metrics = {m for m, _kind in THRESHOLD_FLAGS.values()}
    errors = []
    for i, e in enumerate(entries):
        where = f"accepted_regressions[{i}]"
        if not isinstance(e, dict) or set(e) != set(ACCEPTED_REGRESSION_KEYS):
            errors.append(f"{where} must have exactly the keys {list(ACCEPTED_REGRESSION_KEYS)}")
            continue
        if not (isinstance(e["base_benchmark_sha256"], str) and re.fullmatch(r"[0-9a-f]{64}", e["base_benchmark_sha256"])):
            errors.append(f"{where}.base_benchmark_sha256 must be a sha256 hex digest")
        if e["metric"] not in metrics:
            errors.append(f"{where}.metric must be one of {sorted(metrics)}")
        if not all(isinstance(e[k], (int, float)) and not isinstance(e[k], bool) for k in ("floor", "accepted_value")):
            errors.append(f"{where}.floor and accepted_value must be numbers")
        if not _is_str_list(e["cases"], nonempty=True):
            errors.append(f"{where}.cases must list the base-benchmark case ids that regressed")
        if not (isinstance(e["reason"], str) and len(e["reason"].strip()) >= 20):
            errors.append(f"{where}.reason must explain the trade-off")
    return errors


def accept_regressions(failures: list[tuple[str, float]], baseline: dict, base_bench_sha: str) -> tuple[list, list]:
    """Split no-regression failures [(metric, measured)] into (still failing, accepted).

    A failure is accepted only when this commit's baseline.json records an
    `accepted_regressions` entry for the same base benchmark (by sha256), the same
    metric, and exactly the measured value. The acceptance is part of the reviewed
    diff, cannot cover a further drop, and stops applying once the base moves on."""
    entries = baseline.get("accepted_regressions", []) if not accepted_regression_errors(baseline) else []
    still, accepted = [], []
    for metric, val in failures:
        match = [e for e in entries if e["base_benchmark_sha256"] == base_bench_sha and e["metric"] == metric
                 and val is not None and round(float(e["accepted_value"]), 4) == round(float(val), 4)]
        (accepted if match else still).append((metric, val, match[0] if match else None))
    return still, accepted


def _floors(baseline: dict) -> dict[str, float]:
    """Threshold flags for every gated metric the baseline records on the test split."""
    floor = (baseline.get("metrics") or {}).get("test") or {}
    return {flag: floor[metric] for flag, (metric, _kind) in THRESHOLD_FLAGS.items() if floor.get(metric) is not None}


def run_gate(root: Path = sl.REPO_ROOT, base_sha: str | None = None, event_name: str | None = None,
             report_dir: Path | None = None, summary_path: Path | None = None,
             route_fn: RouteFn | None = None, head_sha: str | None = None) -> int:
    """The CI routing gate. Three checks, all on the test split, over every metric in
    THRESHOLD_FLAGS that the relevant baseline records:
      1. integrity: baseline.json's benchmark_sha256 matches benchmark.json;
      2. this tree's router meets this tree's baseline on this tree's benchmark
         (which must also pass coverage and split-integrity rules);
      3. no regression: this tree's router, on the BASE commit's benchmark (labels
         carried over by adapt_parent_benchmark), meets the BASE commit's baseline.
         Lowering baseline.json or deleting hard cases in the same change therefore
         does not lower the bar.
    `root` supplies the benchmark, the baseline, the skills used to validate labels
    and the git history; routing itself is `route_fn` (default: this checkout's
    router, which reads this checkout's skills). The base commit is chosen by
    resolve_base(base_sha, event_name, head_sha); pass an already-resolved commit
    as `base_sha` with no `event_name` to use it as is. A base that cannot be
    resolved (GateError) fails the gate; a resolved base with no baseline.json
    (it predates the routing benchmark), or no base at all (a root commit with no
    explicit base), skips check 3 with a ::warning:: and does not fail.
    Prints ::error::/::warning:: annotations, appends a summary to `summary_path`
    (GITHUB_STEP_SUMMARY), writes router-report.json / router-report-parent.json into
    `report_dir`. Returns 0 when every check that ran passed, else 1."""
    root = Path(root)
    report_dir = Path(report_dir) if report_dir is not None else Path.cwd()
    bench_path, baseline_path = root / "evals" / "routing" / "benchmark.json", root / "evals" / "routing" / "baseline.json"
    bench_rel, baseline_rel = "evals/routing/benchmark.json", "evals/routing/baseline.json"
    route_fn = route_fn or load_route_fn(None)
    summary: list[str] = []
    errors = 0
    skills = sl.load_all_skills(root)
    valid = {s.slug for s in skills}

    def check(label: str, bench: dict, baseline: dict, report_name: str, extra: dict | None = None,
              accept: tuple[dict, str] | None = None) -> list[str]:
        floors = _floors(baseline)
        print(f"[{label}] floors: {floors}", flush=True)
        report = evaluate(bench["cases"], route_fn, top_k=int(baseline.get("top_k", 5)), split="test")
        report.pop("cases")
        test = report["splits"]["test"]
        failures = check_thresholds(test, floors)
        accepted_notes: list[str] = []
        if accept is not None and failures:
            # Per-metric view of the same failures, so an explicit acceptance can match them.
            failing = [(THRESHOLD_FLAGS[f][0], test.get(THRESHOLD_FLAGS[f][0])) for f in floors
                       if check_thresholds(test, {f: floors[f]})]
            still, accepted = accept_regressions(failing, accept[0], accept[1])
            keep = {m for m, _v, _e in still}
            failures = [f for f in failures if f.split(" ", 1)[0] in keep]
            for metric, val, entry in accepted:
                note = (f"{metric} = {val} below the base floor {entry['floor']}, ACCEPTED by baseline.json "
                        f"accepted_regressions (cases {', '.join(entry['cases'])}): {entry['reason']}")
                print(f"::warning::routing trade-off [{label}]: {note}")
                accepted_notes.append(note)
        report.update(threshold_failures=failures, accepted_regressions=accepted_notes, **(extra or {}))
        (report_dir / report_name).write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        print(json.dumps({k: v for k, v in report["splits"]["test"].items() if k not in ("counts", "intervals")},
                         indent=2))
        for failure in failures:
            print(f"::error::routing regression [{label}]: {failure}")
        return failures

    try:
        head_baseline = json.loads(baseline_path.read_text(encoding="utf-8"))
        head_bench = load_benchmark(bench_path)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        print(f"::error::cannot read the routing benchmark or baseline: {exc}")
        _write_summary(summary_path, ["- FAIL: cannot read the routing benchmark or baseline"])
        return 1
    if head_baseline.get("benchmark_sha256") != benchmark_sha256(bench_path):
        print(f"::error::{baseline_rel} benchmark_sha256 does not match {bench_rel}: re-baseline after editing "
              "the benchmark")
        errors += 1
        summary.append("- integrity: FAIL (benchmark_sha256 mismatch)")
    else:
        summary.append("- integrity: pass")
    for problem in accepted_regression_errors(head_baseline):
        print(f"::error::{baseline_rel}: {problem}")
        errors += 1
        summary.append(f"- integrity: FAIL ({problem})")
    problems = (validate_benchmark(head_bench, valid, requires_map(skills)) or coverage_errors(head_bench, valid)
                + split_integrity_errors(head_bench))
    if problems:
        for p in problems:
            print(f"::error::{bench_rel} does not validate: {p}")
        errors += 1
        summary.append(f"- this commit's router vs this commit's baseline: benchmark did not validate "
                       f"({len(problems)} error(s); see the log)")
    else:
        failures = check("own baseline", head_bench, head_baseline, "router-report.json")
        errors += bool(failures)
        summary.append(f"- this commit's router vs this commit's baseline: {'; '.join(failures) or 'pass'}")

    try:
        base, how = resolve_base(root, base_sha, event_name, head_sha)
    except GateError as exc:
        print(f"::error::routing gate base commit: {exc}")
        errors += 1
        summary.append(f"- vs base: FAIL ({exc})")
        base, how = None, ""
    else:
        print(f"base commit: {how}", flush=True)
        parent_baseline = _show(root, base, baseline_rel) if base else None
        parent_bench = _show(root, base, bench_rel) if base else None
        if parent_baseline is None or parent_bench is None:
            print("::warning::no base commit with a routing baseline; the no-regression check did not run "
                  f"({how})")
            summary.append(f"- vs parent: NOT RUN (no base commit with a baseline; {how})")
        else:
            label = f"parent {base[:12]}"
            try:
                pbase, pbench = json.loads(parent_baseline), json.loads(parent_bench)
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                print(f"::error::{label} benchmark or baseline is not valid JSON: {exc}")
                errors += 1
                summary.append(f"- vs {label}: FAIL (unreadable benchmark or baseline)")
            else:
                adapted, dropped, relabelled = (adapt_parent_benchmark(pbench, skills) if isinstance(pbench, dict)
                                                else (pbench, [], []))
                problems = validate_benchmark(adapted, valid)
                if dropped:
                    print(f"::warning::{len(dropped)} {label} case(s) expect a skill this commit no longer has "
                          f"and were dropped from the no-regression check: {', '.join(dropped)}")
                if problems:
                    for p in problems:
                        print(f"::error::{label} benchmark does not validate against this commit's skills: {p}")
                    errors += 1
                    summary.append(f"- vs {label}: FAIL ({len(problems)} benchmark validation error(s); see the log)")
                else:
                    base_bench_sha = hashlib.sha256(parent_bench.replace(b"\r\n", b"\n")).hexdigest()
                    failures = check(label, adapted, pbase, "router-report-parent.json",
                                     {"dropped_cases": dropped, "relabelled_cases": relabelled, "base": how},
                                     accept=(head_baseline, base_bench_sha))
                    errors += bool(failures)
                    note = (f" ({len(dropped)} case(s) dropped for removed skills, {len(relabelled)} relabelled "
                            "for new prerequisites)") if dropped or relabelled else ""
                    summary.append(f"- this commit's router vs {label}'s benchmark + baseline: "
                                   f"{'; '.join(failures) or 'pass'}{note}")
                    summary.append(f"  - base commit: {how}")
    _write_summary(summary_path, summary)
    print("\n".join(summary))
    return 1 if errors else 0


def _write_summary(path: Path | None, lines: list[str]) -> None:
    if path is None:
        env = os.environ.get("GITHUB_STEP_SUMMARY")
        path = Path(env) if env else None
    if path is not None:
        with open(path, "a", encoding="utf-8") as fh:
            fh.write("### Routing benchmark gate (test split)\n" + "\n".join(lines) + "\n")


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
    gate = ap.add_argument_group("CI gate (--gate ignores the options above)")
    gate.add_argument("--gate", action="store_true",
                      help="run the routing gate: integrity, own baseline, and no regression vs a base commit")
    gate.add_argument("--base-sha", default=None,
                      help="base commit: github.event.pull_request.base.sha or github.event.before "
                           "(empty or all zeros: first parent of HEAD)")
    gate.add_argument("--event-name", default=None, help="github.event_name (pull_request, push, ...)")
    gate.add_argument("--head-sha", default=None,
                      help="github.event.pull_request.head.sha: tells GitHub's test merge commit from a "
                           "head-only checkout (see resolve_base)")
    gate.add_argument("--report-dir", type=Path, default=Path("."), help="where router-report*.json are written")
    gate.add_argument("--summary-file", type=Path, default=None,
                      help="markdown summary to append to (default: $GITHUB_STEP_SUMMARY if set)")
    args = ap.parse_args(argv)

    if args.gate:
        return run_gate(sl.REPO_ROOT, base_sha=args.base_sha, event_name=args.event_name,
                        report_dir=args.report_dir, summary_path=args.summary_file, head_sha=args.head_sha)

    try:
        bench = load_benchmark(args.benchmark)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        print(f"benchmark error: cannot read {args.benchmark}: {exc}", file=sys.stderr)
        return 2
    skills = sl.load_all_skills()
    valid = {s.slug for s in skills}
    # Coverage checks assume a structurally valid file, so run them only after
    # per-case validation passes (otherwise they could crash and exit 1, not 2).
    problems = (validate_benchmark(bench, valid, requires_map(skills))
                or coverage_errors(bench, valid) + split_integrity_errors(bench))
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
