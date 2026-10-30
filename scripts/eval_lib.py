"""Behavioral-eval core: case loading, validation, hard-assertion grading,
fixture self-tests, per-skill plans, and coverage. See evals/SCHEMA.md.

Stdlib-only, like the rest of scripts/ (see skills_lib.py). Two kinds of
judgement are kept strictly apart everywhere in this module and in the
reports built on it:

* **hard assertions** — `must_include` / `must_not_include` regexes graded
  deterministically by `grade_response`. Same text in, same verdict out.
* **subjective rubric** — `expected_behavior` / `must_not` prose, graded by a
  human or an (optional, explicitly configured) LLM judge. Nothing here turns
  rubric prose into a pass/fail; run_evals.py records judge verdicts under a
  separate `subjective` key and never folds them into hard pass rates. The
  explicit rules that combine the layers into one verdict live in
  eval_grading.py (VERDICT_RULES).

Every function takes a repository `root` so tests can point it at a
throwaway tree instead of the live repo other people are editing.
"""
from __future__ import annotations

import ast
import hashlib
import json
import re
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import skills_lib as sl

HARNESS_VERSION = "1.2.0"  # 1.2.0: layered grading, report schema 2, backends, run directories

EVAL_CATEGORIES = ("happy_path", "edge_case", "adversarial", "misconception", "failure_recovery", "ambiguity")
NON_HAPPY_PATH_CATEGORIES = tuple(c for c in EVAL_CATEGORIES if c != "happy_path")
CASE_KINDS = ("response", "executable")
# Closed set so a typo ("api_key") can't silently mean "no prerequisite".
CASE_REQUIRES = ("network", "api-key", "gpu", "example-deps", "large-download")
ASSERTION_STRATEGIES = ("hard-assertions", "executable", "rubric")

REQUIRED_CASE_KEYS = ("skill", "category", "kind", "input", "expected_behavior")
SCALAR_CASE_KEYS = ("id", "skill", "category", "kind", "input", "notes")
LIST_CASE_KEYS = ("expected_behavior", "must_not", "must_include", "must_not_include", "executable_tests",
                  "requires", "artifacts", "limitations")
CASE_KEYS = frozenset(SCALAR_CASE_KEYS + LIST_CASE_KEYS)

PLAN_LIST_KEYS = ("negative_cases", "rubric", "prerequisites", "execution_constraints", "expected_artifacts",
                  "known_limitations", "unsupported_use_cases")
PLAN_REQUIRED_KEYS = ("skill", "type", "representative_tasks", "assertion_strategy") + PLAN_LIST_KEYS
PLAN_OPTIONAL_KEYS = ("non_happy_path_exemption",)
PLAN_KEYS = frozenset(PLAN_REQUIRED_KEYS + PLAN_OPTIONAL_KEYS)

ID_RE = re.compile(r"^[a-z0-9][a-z0-9_\-]*$")
FIXTURE_RE = re.compile(r"^(?P<stem>[a-z0-9][a-z0-9_\-]*)\.(?P<expect>pass|fail)(?:\.(?P<label>[a-z0-9_\-]+))?\.md$")

# Content-free texts no must_include pattern may match: a pattern that does
# would "pass" a response that says nothing about the topic. A must_not_include
# pattern that matches one would fail harmless text — also over-broad.
NEUTRAL_PROBES = (
    "",
    "Thanks for the question.",
    "Lorem ipsum dolor sit amet, consectetur adipiscing elit, sed do eiusmod tempor incididunt.",
    "I am not able to help with that request.",
)

PATTERN_FLAGS = re.IGNORECASE
_EVIDENCE_CHARS = 80

# Typographic variants a model emits interchangeably; markdown emphasis and
# code ticks are dropped so `**data leakage**` and "data leakage" grade alike.
_TRANSLATE = str.maketrans({
    "‘": "'", "’": "'", "‛": "'", "′": "'",
    "“": '"', "”": '"',
    "‐": "-", "‑": "-", "‒": "-", "–": "-", "—": "-", "―": "-", "−": "-",
    "*": None, "`": None,
})


def normalize_response(text: str) -> str:
    """Canonical form every hard assertion is matched against (documented in evals/SCHEMA.md)."""
    text = unicodedata.normalize("NFKC", text).translate(_TRANSLATE)
    return re.sub(r"\s+", " ", text).strip()


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def read_text_lf(path: Path) -> str:
    """UTF-8 text with CRLF/CR normalized to LF, so a Windows (core.autocrlf) checkout
    parses and hashes exactly like a Linux one."""
    return path.read_bytes().decode("utf-8").replace("\r\n", "\n").replace("\r", "\n")


def sha256_file(path: Path) -> str:
    """sha256 of the LF-normalized text (platform-independent dataset hashes)."""
    return sha256_bytes(read_text_lf(path).encode("utf-8"))


def rel(path: Path, root: Path) -> str:
    try:
        return path.resolve().relative_to(root.resolve()).as_posix()
    except ValueError:
        return path.as_posix()


# --------------------------------------------------------------------------
# Cases
# --------------------------------------------------------------------------

@dataclass
class Fixture:
    path: Path
    expect: str  # "pass" | "fail"
    label: str | None

    @property
    def name(self) -> str:
        return self.path.name


@dataclass
class Case:
    path: Path
    data: dict[str, Any]
    diagnostics: list[sl.Diagnostic]
    sha256: str
    fixtures: list[Fixture] = field(default_factory=list)

    @property
    def stem(self) -> str:
        return self.path.stem

    @property
    def skill_dir(self) -> str:
        return self.path.parent.name

    @property
    def id(self) -> str:
        value = self.data.get("id")
        return value if isinstance(value, str) and value else self.stem

    @property
    def uid(self) -> str:
        """Globally unique id used in reports and response files: `<skill>/<id>`."""
        return f"{self.skill_dir}/{self.id}"

    def str_list(self, key: str) -> list[str]:
        value = self.data.get(key)
        return [v for v in value if isinstance(v, str)] if isinstance(value, list) else []

    @property
    def must_include(self) -> list[str]:
        return self.str_list("must_include")

    @property
    def must_not_include(self) -> list[str]:
        return self.str_list("must_not_include")

    @property
    def has_hard_assertions(self) -> bool:
        return bool(self.must_include or self.must_not_include)

    @property
    def category(self) -> str | None:
        return self.data.get("category")

    @property
    def kind(self) -> str | None:
        return self.data.get("kind")


def evals_dir(root: Path) -> Path:
    return root / "evals"


def load_case(path: Path) -> Case:
    text = read_text_lf(path)
    parsed = sl.parse_frontmatter_with_diagnostics(text)
    return Case(path=path, data=parsed.data, diagnostics=parsed.diagnostics, sha256=sha256_bytes(text.encode("utf-8")))


def load_cases(root: Path) -> list[Case]:
    cases = [load_case(p) for p in sorted(evals_dir(root).glob("*/*.yaml"))]
    by_key = {(c.skill_dir, c.stem): c for c in cases}
    for md in sorted(evals_dir(root).glob("*/*.md")):
        m = FIXTURE_RE.match(md.name)
        if m and (md.parent.name, m.group("stem")) in by_key:
            by_key[(md.parent.name, m.group("stem"))].fixtures.append(
                Fixture(path=md, expect=m.group("expect"), label=m.group("label")))
    return cases


# --------------------------------------------------------------------------
# Hard-assertion grading
# --------------------------------------------------------------------------

def compile_pattern(pattern: str) -> re.Pattern[str]:
    return re.compile(pattern, PATTERN_FLAGS)


def _evidence(m: re.Match[str] | None) -> str | None:
    if m is None:
        return None
    text = m.group(0)
    return text if len(text) <= _EVIDENCE_CHARS else text[:_EVIDENCE_CHARS] + "..."


def grade_response(case: Case, response: str) -> dict[str, Any]:
    """Grade `response` against the case's hard assertions only.

    `passed` is True iff every must_include pattern matches and no
    must_not_include pattern matches. `gradable` is False when the case has
    no hard assertions (rubric-only) — such a case is never reported as
    passed or failed on hard assertions. Raises re.error for a bad pattern
    (validate first).
    """
    text = normalize_response(response)
    assertions = []
    for i, pattern in enumerate(case.must_include):
        m = compile_pattern(pattern).search(text)
        assertions.append({"id": f"must_include[{i}]", "type": "must_include", "pattern": pattern,
                           "passed": m is not None, "evidence": _evidence(m)})
    for i, pattern in enumerate(case.must_not_include):
        m = compile_pattern(pattern).search(text)
        assertions.append({"id": f"must_not_include[{i}]", "type": "must_not_include", "pattern": pattern,
                           "passed": m is None, "evidence": _evidence(m)})
    gradable = bool(assertions)
    return {"gradable": gradable, "passed": gradable and all(a["passed"] for a in assertions),
            "assertions": assertions}


def lint_patterns(case: Case) -> tuple[list[str], list[str]]:
    """(errors, warnings) for the case's regexes, independent of fixtures."""
    errors: list[str] = []
    warnings: list[str] = []
    for key in ("must_include", "must_not_include"):
        seen: set[str] = set()
        for i, pattern in enumerate(case.str_list(key)):
            label = f"{key}[{i}] {pattern!r}"
            if pattern in seen:
                warnings.append(f"{label} is duplicated")
            seen.add(pattern)
            if not pattern.strip():
                errors.append(f"{label} is empty")
                continue
            try:
                rx = compile_pattern(pattern)
            except re.error as e:
                errors.append(f"{label} is not a valid Python regex: {e}")
                continue
            if rx.search(""):
                errors.append(f"vacuous pattern: {label} matches the empty string, so it "
                              + ("passes any response" if key == "must_include" else "fails every response"))
                continue
            for probe in NEUTRAL_PROBES[1:]:
                if rx.search(normalize_response(probe)):
                    errors.append(f"vacuous pattern: {label} matches content-free text {probe!r}, so it "
                                  + ("cannot show the response addressed the topic" if key == "must_include"
                                     else "would fail harmless responses"))
                    break
    return errors, warnings


def self_test_fixtures(case: Case) -> tuple[list[dict[str, Any]], list[str], list[str]]:
    """Grade each fixture; pass fixtures must pass, fail fixtures must fail.

    Returns (results, errors, warnings). Two discrimination warnings (promote
    with validate_evals.py --strict):

    * non-discriminating pattern: a must_include pattern that also matches
      every fail fixture does not distinguish any known wrong answer.
    * concept-free fail fixtures: no fail fixture is *on-topic* (matches at
      least half of the must_include patterns). A wrong answer that never
      uses the topic's vocabulary fails every concept pattern trivially, so
      it shows nothing about the realistic threat: a response that names the
      right concepts and still reaches the wrong conclusion. Only an on-topic
      wrong answer exercises must_not_include and the concept patterns'
      precision.
    """
    results: list[dict[str, Any]] = []
    errors: list[str] = []
    warnings: list[str] = []
    fixtures = sorted(case.fixtures, key=lambda f: f.name)
    if not case.has_hard_assertions:
        if fixtures:
            errors.append("has .pass/.fail fixtures but no must_include/must_not_include to test them against")
        return results, errors, warnings
    expects = {f.expect for f in fixtures}
    if "pass" not in expects:
        errors.append(f"has hard assertions but no pass fixture ({case.stem}.pass.md) proving they are satisfiable")
    if "fail" not in expects:
        errors.append(f"has hard assertions but no fail fixture ({case.stem}.fail.md) proving they discriminate")
    fail_hits: dict[int, int] = {}
    n_fail = 0
    for fx in fixtures:
        try:
            grade = grade_response(case, read_text_lf(fx.path))
        except re.error:
            return results, errors, warnings  # reported by lint_patterns
        ok = grade["passed"] if fx.expect == "pass" else not grade["passed"]
        matched = sum(a["type"] == "must_include" and a["passed"] for a in grade["assertions"])
        results.append({"case_id": case.uid, "fixture": fx.name, "expected": fx.expect,
                        "observed": "pass" if grade["passed"] else "fail", "ok": ok,
                        "must_include_matched": matched, "on_topic": is_on_topic(matched, len(case.must_include)),
                        "assertions": grade["assertions"]})
        if not ok:
            if fx.expect == "pass":
                failed = [a["id"] for a in grade["assertions"] if not a["passed"]]
                errors.append(f"pass fixture {fx.name} fails hard assertion(s) {', '.join(failed)} "
                              "(assertions unsatisfiable or over-fitted)")
            else:
                errors.append(f"fail fixture {fx.name} passes every hard assertion (assertions do not "
                              "discriminate this wrong answer)")
        if fx.expect == "fail":
            n_fail += 1
            for a in grade["assertions"]:
                if a["type"] == "must_include" and a["passed"]:
                    idx = int(a["id"].split("[")[1].rstrip("]"))
                    fail_hits[idx] = fail_hits.get(idx, 0) + 1
    if n_fail:
        for idx in sorted(fail_hits):
            if fail_hits[idx] == n_fail:
                warnings.append(f"non-discriminating pattern: must_include[{idx}] {case.must_include[idx]!r} "
                                "also matches every fail fixture")
        n_inc = len(case.must_include)
        if n_inc and not any(r["on_topic"] for r in results if r["expected"] == "fail"):
            warnings.append(f"concept-free fail fixtures: no fail fixture matches at least {on_topic_threshold(n_inc)} "
                            f"of the {n_inc} must_include pattern(s), so discrimination is shown only against "
                            "off-topic wrong answers; add a <case>.fail.<label>.md that uses the topic's vocabulary "
                            "but reaches the wrong conclusion")
    return results, errors, warnings


def unexercised_must_not_include(case: Case, results: list[dict[str, Any]]) -> list[str]:
    """Advisory: must_not_include patterns that no fail fixture triggers.

    A fail fixture already fails when a concept pattern misses, so a broken
    must_not_include regex (a typo, a missing space, a too-narrow qualifier)
    never shows up in the self-test. Only a wrong answer it actually catches
    proves it can fire. `results` are this case's self_test_fixtures results;
    nothing is reported when the case has no fail fixture (already an error)
    or a pattern did not compile (no results).
    """
    fails = [r for r in results if r["expected"] == "fail"]
    if not fails:
        return []
    fired = {a["id"] for r in fails for a in r["assertions"] if a["type"] == "must_not_include" and not a["passed"]}
    return [f"unexercised pattern: must_not_include[{i}] {p!r} matches no fail fixture, so nothing shows it "
            f"can fire; add a {case.stem}.fail.<label>.md wrong answer it catches, or fix or remove the pattern"
            for i, p in enumerate(case.must_not_include) if f"must_not_include[{i}]" not in fired]


def on_topic_threshold(n_must_include: int) -> int:
    """At least half of the concept patterns (rounded up)."""
    return (n_must_include + 1) // 2


def is_on_topic(matched: int, n_must_include: int) -> bool:
    return n_must_include > 0 and matched >= on_topic_threshold(n_must_include)


# --------------------------------------------------------------------------
# Executable test ids (resolved statically: no import, so missing optional
# deps never make validation fail or pass)
# --------------------------------------------------------------------------

def _class_methods(classes: dict[str, ast.ClassDef], name: str, seen: frozenset[str] = frozenset()) -> set[str]:
    cls = classes.get(name)
    if cls is None or name in seen:
        return set()
    methods = {n.name for n in cls.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}
    for base in cls.bases:
        if isinstance(base, ast.Name):
            methods |= _class_methods(classes, base.id, seen | {name})
    return methods


def resolve_test_id(test_id: str, root: Path) -> str | None:
    """Return None if `test_id` names an existing test under tests/examples, else an error message.

    Format: dotted unittest id relative to tests/ (the `-s tests` discovery
    root), e.g. `examples.test_rag_pipeline.TestRagPipelineFakeFaiss.test_x`.
    """
    parts = test_id.split(".")
    if len(parts) != 4 or parts[0] != "examples" or not all(re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", p) for p in parts):
        return (f"executable test id {test_id!r} must be `examples.<module>.<Class>.<test_method>` "
                "(a unittest id relative to tests/)")
    module = root / "tests" / "examples" / f"{parts[1]}.py"
    if not module.is_file():
        return f"executable test id {test_id!r}: no module {rel(module, root)}"
    try:
        tree = ast.parse(module.read_text(encoding="utf-8"))
    except SyntaxError as e:
        return f"executable test id {test_id!r}: cannot parse {rel(module, root)} ({e})"
    classes = {n.name: n for n in tree.body if isinstance(n, ast.ClassDef)}
    if parts[2] not in classes:
        return f"executable test id {test_id!r}: no class {parts[2]} in {rel(module, root)}"
    if parts[3] not in _class_methods(classes, parts[2]):
        return f"executable test id {test_id!r}: class {parts[2]} has no method {parts[3]}"
    if not parts[3].startswith("test"):
        return f"executable test id {test_id!r}: {parts[3]} is not a test method (must start with 'test')"
    return None


# --------------------------------------------------------------------------
# Case validation
# --------------------------------------------------------------------------

def validate_case(case: Case, valid_slugs: set[str], root: Path) -> tuple[list[str], list[str]]:
    errors: list[str] = []
    warnings: list[str] = []
    data = case.data
    for d in case.diagnostics:
        errors.append(f"could not parse: {d}")
    for key in sorted(set(data) - CASE_KEYS):
        errors.append(f"unknown field {key!r} (allowed: {', '.join(sorted(CASE_KEYS))})")
    for key in REQUIRED_CASE_KEYS:
        if data.get(key) in (None, "", []):
            errors.append(f"missing required field '{key}'")
    for key in SCALAR_CASE_KEYS:
        if key in data and data[key] is not None and not isinstance(data[key], str):
            errors.append(f"field '{key}' must be a string")
    for key in LIST_CASE_KEYS:
        value = data.get(key)
        if value is None:
            continue
        if not isinstance(value, list):
            errors.append(f"field '{key}' must be a list")
        elif any(not isinstance(v, str) or not v.strip() for v in value):
            errors.append(f"field '{key}' must contain only non-empty strings")

    skill = data.get("skill")
    if isinstance(skill, str) and skill:
        if skill not in valid_slugs:
            errors.append(f"skill '{skill}' does not exist as a folder")
        if case.skill_dir != skill:
            errors.append(f"lives under evals/{case.skill_dir}/ but declares skill: {skill}")
    if not ID_RE.match(case.stem):
        errors.append(f"file stem {case.stem!r} must match {ID_RE.pattern}")
    if "id" in data and data["id"] != case.stem:
        # Fixtures and plan.json negative_cases are keyed by stem; one name avoids ambiguity.
        errors.append(f"id {data['id']!r} must equal the file stem {case.stem!r}")
    category = data.get("category")
    if category and category not in EVAL_CATEGORIES:
        errors.append(f"category '{category}' not in {EVAL_CATEGORIES}")
    kind = data.get("kind")
    if kind and kind not in CASE_KINDS:
        errors.append(f"kind '{kind}' not in {CASE_KINDS}")
    tests = case.str_list("executable_tests")
    if kind == "executable" and not tests:
        errors.append("kind: executable requires a non-empty executable_tests list")
    if kind == "response" and tests:
        errors.append("executable_tests is only meaningful for kind: executable")
    for test_id in tests:
        problem = resolve_test_id(test_id, root)
        if problem:
            errors.append(problem)
    for req in case.str_list("requires"):
        if req not in CASE_REQUIRES:
            errors.append(f"requires value {req!r} not in {CASE_REQUIRES}")
    if not case.has_hard_assertions and kind == "response":
        warnings.append("rubric-only case: no must_include/must_not_include, so it is never graded automatically")
    pe, pw = lint_patterns(case)
    errors += pe
    warnings += pw
    return errors, warnings


def stray_files(root: Path, cases: list[Case], slugs: set[str]) -> list[str]:
    """Files under evals/<skill-slug>/ that no case, fixture rule, or plan explains.

    Only skill-named directories are checked: other evals/ subdirectories are
    auxiliary datasets owned by other tools (e.g. evals/routing/, the
    scripts/eval_router.py benchmark), not behavioral cases. A *.yaml or
    plan.json in such a directory is still an error (unknown skill).
    """
    known = {(c.skill_dir, c.stem) for c in cases}
    problems = []
    for p in sorted(evals_dir(root).glob("*/*")):
        if p.parent.name not in slugs:
            continue
        if p.is_dir() or p.suffix == ".yaml" or p.name == "plan.json":
            continue
        m = FIXTURE_RE.match(p.name)
        if m and (p.parent.name, m.group("stem")) in known:
            continue
        if m:
            problems.append(f"{rel(p, root)}: fixture has no matching case {m.group('stem')}.yaml")
        else:
            problems.append(f"{rel(p, root)}: unexpected file (allowed: <case>.yaml, <case>.pass[.label].md, "
                            "<case>.fail[.label].md, plan.json)")
    return problems


# --------------------------------------------------------------------------
# Plans
# --------------------------------------------------------------------------

def load_plan(path: Path) -> tuple[dict[str, Any] | None, str | None]:
    try:
        data = json.loads(read_text_lf(path))
    except (json.JSONDecodeError, UnicodeDecodeError) as e:
        return None, f"not valid JSON: {e}"
    if not isinstance(data, dict):
        return None, "top level must be a JSON object"
    return data, None


def _str_list_ok(value: Any) -> bool:
    return isinstance(value, list) and all(isinstance(v, str) and v.strip() for v in value)


def validate_plan(plan: dict[str, Any], slug: str, skill: sl.Skill | None,
                  cases: list[Case]) -> tuple[list[str], list[str]]:
    errors: list[str] = []
    warnings: list[str] = []
    for key in sorted(set(plan) - PLAN_KEYS):
        errors.append(f"unknown field {key!r} (allowed: {', '.join(sorted(PLAN_KEYS))})")
    for key in PLAN_REQUIRED_KEYS:
        if key not in plan:
            errors.append(f"missing required field '{key}'")
    if plan.get("skill") != slug:
        errors.append(f"skill {plan.get('skill')!r} must equal the directory name {slug!r}")
    if skill is None:
        errors.append(f"skill '{slug}' does not exist as a folder")
    elif "type" in plan and plan["type"] != skill.frontmatter.get("type"):
        errors.append(f"type {plan['type']!r} does not match {slug}/SKILL.md type {skill.frontmatter.get('type')!r}")

    tasks = plan.get("representative_tasks")
    if "representative_tasks" in plan:
        if not isinstance(tasks, list) or not tasks:
            errors.append("representative_tasks must be a non-empty list")
        else:
            for i, t in enumerate(tasks):
                if (not isinstance(t, dict) or set(t) != {"task", "expected_outcome"}
                        or not all(isinstance(v, str) and v.strip() for v in t.values())):
                    errors.append(f"representative_tasks[{i}] must be an object with exactly non-empty string "
                                  "fields 'task' and 'expected_outcome'")
    for key in PLAN_LIST_KEYS:
        if key in plan and not _str_list_ok(plan[key]):
            errors.append(f"'{key}' must be a list of non-empty strings")

    strategy = plan.get("assertion_strategy")
    if isinstance(strategy, str):
        strategy = [strategy]
    if "assertion_strategy" in plan:
        if (not isinstance(strategy, list) or not strategy or len(set(strategy)) != len(strategy)
                or any(s not in ASSERTION_STRATEGIES for s in strategy)):
            errors.append(f"assertion_strategy must be one or a list of unique values from {ASSERTION_STRATEGIES}")
            strategy = []
    else:
        strategy = []
    if "hard-assertions" in strategy and not any(c.has_hard_assertions for c in cases):
        errors.append("assertion_strategy includes 'hard-assertions' but no case has must_include/must_not_include")
    if "executable" in strategy and not any(c.kind == "executable" for c in cases):
        errors.append("assertion_strategy includes 'executable' but no case has kind: executable")
    if "rubric" in strategy and not plan.get("rubric"):
        errors.append("assertion_strategy includes 'rubric' but rubric is empty")
    if plan.get("rubric") and "rubric" not in strategy:
        warnings.append("rubric is non-empty but 'rubric' is not in assertion_strategy")

    by_id = {c.id: c for c in cases}
    for item in plan.get("negative_cases") or []:
        if not isinstance(item, str):
            continue
        if ID_RE.match(item):  # a bare id refers to a case; a description has spaces
            case = by_id.get(item)
            if case is None:
                errors.append(f"negative_cases entry {item!r} looks like a case id but evals/{slug}/{item}.yaml "
                              "does not exist (use a sentence for a planned, not-yet-written case)")
            elif case.category == "happy_path":
                errors.append(f"negative_cases entry {item!r} is a happy_path case")
    exemption = plan.get("non_happy_path_exemption")
    if exemption is not None and (not isinstance(exemption, str) or len(exemption.strip()) < 20):
        errors.append("non_happy_path_exemption must be a sentence explaining why no non-happy-path case applies")
    return errors, warnings


# --------------------------------------------------------------------------
# Whole-repo validation + coverage
# --------------------------------------------------------------------------

@dataclass
class EvalSuite:
    root: Path
    skills: list[sl.Skill]
    cases: list[Case]
    plans: dict[str, dict[str, Any]]
    errors: list[str]
    warnings: list[str]
    fixture_results: list[dict[str, Any]]
    # Kept out of `warnings` (run_evals.py validation) but promoted by validate_evals.py
    # --strict and --strict-advisories; the shipped tree has none (tests/test_evals.py).
    advisories: list[str] = field(default_factory=list)

    def skill_by_slug(self) -> dict[str, sl.Skill]:
        return sl.by_slug(self.skills)


def load_suite(root: Path) -> EvalSuite:
    """Load and validate everything under evals/; never raises for bad content."""
    skills = sl.load_all_skills(root)
    slugs = {s.slug for s in skills}
    by_slug = sl.by_slug(skills)
    cases = load_cases(root)
    errors: list[str] = []
    warnings: list[str] = []
    fixture_results: list[dict[str, Any]] = []
    advisories: list[str] = []
    if not cases:
        errors.append("no eval case files found under evals/*/*.yaml")
    seen_uids: dict[str, Path] = {}
    for case in cases:
        where = rel(case.path, root)
        ce, cw = validate_case(case, slugs, root)
        if case.uid in seen_uids:
            ce.append(f"duplicate case id {case.uid} (also {rel(seen_uids[case.uid], root)})")
        seen_uids[case.uid] = case.path
        results, fe, fw = self_test_fixtures(case)  # skips itself if a pattern does not compile
        fixture_results += results
        ce += fe
        cw += fw
        errors += [f"{where}: {e}" for e in ce]
        warnings += [f"{where}: {w}" for w in cw]
        advisories += [f"{where}: {a}" for a in unexercised_must_not_include(case, results)]
    errors += stray_files(root, cases, slugs)

    plans: dict[str, dict[str, Any]] = {}
    for path in sorted(evals_dir(root).glob("*/plan.json")):
        slug = path.parent.name
        plan, problem = load_plan(path)
        if problem:
            errors.append(f"{rel(path, root)}: {problem}")
            continue
        pe, pw = validate_plan(plan, slug, by_slug.get(slug), [c for c in cases if c.skill_dir == slug])
        errors += [f"{rel(path, root)}: {e}" for e in pe]
        warnings += [f"{rel(path, root)}: {w}" for w in pw]
        plans[slug] = plan
    fixture_results.sort(key=lambda r: (r["case_id"], r["fixture"]))
    return EvalSuite(root, skills, cases, plans, errors, warnings, fixture_results, advisories)


def coverage(suite: EvalSuite) -> dict[str, Any]:
    """Per-skill eval coverage over every skill in the repo (not just covered ones)."""
    rows = []
    for skill in sorted(suite.skills, key=lambda s: s.slug):
        cases = [c for c in suite.cases if c.skill_dir == skill.slug]
        plan = suite.plans.get(skill.slug)
        exempt = bool(plan and plan.get("non_happy_path_exemption"))
        non_happy = sum(1 for c in cases if c.category in NON_HAPPY_PATH_CATEGORIES)
        missing = []
        if plan is None:
            missing.append("plan.json")
        if not cases:
            missing.append("case")
        if not non_happy and not exempt:
            missing.append("non-happy-path case")
        rows.append({
            "skill": skill.slug,
            "type": skill.frontmatter.get("type"),
            "has_plan": plan is not None,
            "cases": len(cases),
            "categories": sorted({c.category for c in cases if isinstance(c.category, str)}),
            "non_happy_path_cases": non_happy,
            "non_happy_path_exempt": exempt,
            "hard_assertion_cases": sum(1 for c in cases if c.has_hard_assertions),
            "fixture_backed_cases": sum(1 for c in cases if c.has_hard_assertions and c.fixtures),
            "executable_cases": sum(1 for c in cases if c.kind == "executable"),
            "missing": missing,
        })
    total = len(rows)
    summary = {
        "skills_total": total,
        "with_plan": sum(r["has_plan"] for r in rows),
        "with_cases": sum(r["cases"] > 0 for r in rows),
        "with_non_happy_path": sum(r["non_happy_path_cases"] > 0 for r in rows),
        "with_hard_assertions": sum(r["hard_assertion_cases"] > 0 for r in rows),
        "with_executable": sum(r["executable_cases"] > 0 for r in rows),
        "fully_covered": sum(not r["missing"] for r in rows),
        "cases_total": len(suite.cases),
        "cases_with_hard_assertions": sum(c.has_hard_assertions for c in suite.cases),
    }
    return {"summary": summary, "skills": rows,
            "gaps": [{"skill": r["skill"], "missing": r["missing"]} for r in rows if r["missing"]]}
