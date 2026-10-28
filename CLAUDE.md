# CLAUDE.md — Operating Manual

This repository is a **library of ML/AI skill files** (Markdown). Your job is to fill it
out to a consistent, production-grade standard by working through `INDEX.md`.

Read this file fully before writing anything. Then read the three source-of-truth files:
`README.md` (conventions), `INDEX.md` (the build checklist), and the two gold-standard
examples below.

---

## Project context
Each "skill" is a self-contained folder with a single `SKILL.md`. There are **two types**:

- **Workflow skills [W]** — actionable procedures the agent *executes*. MUST contain
  `## Overview` **and** `## Workflow` (numbered steps with runnable, copy-paste code).
- **Reference skills [R]** — explainer/knowledge cards the agent *loads for context*.
  MUST contain `## Overview` and `## Key Concepts` (no Workflow section).

## Source of truth — do not deviate
- **Template:** `_TEMPLATE/SKILL.md` — clone this for every new skill.
- **Gold standard (workflow):** `data-preprocessing/SKILL.md` — match this depth, code
  density, and gotcha quality for every [W] skill.
- **Gold standard (reference):** `attention-mechanisms/SKILL.md` — match this for every [R] skill.
- **Catalog:** `INDEX.md` — the authoritative list of skills, their type, and status.

## Required front-matter (YAML) — every SKILL.md
The full formal contract — every field, its meaning, and allowed enum values —
lives in **`docs/SKILL-SPEC.md`**, which is binding; this is a quick reference.
Enum values are centralized in `schema/allowed_values.py` — never hardcode a
competing list elsewhere.

```yaml
name: kebab-case-name          # MUST equal the folder name
display_name: Human Readable Name
description: >                  # discovery-critical: "Use when the user wants to…"
  Use when the user wants to impute missing values or encode categorical columns.
  Trigger phrases: "handle missing values", "encode categorical features",
  "build a preprocessing pipeline". NOT for creating new features (see feature-engineering).
type: workflow                 # workflow | reference — MUST match the [W]/[R] tag in INDEX.md
domain: classical-ml           # foundations|classical-ml|deep-learning|llm|specialized|mlops|responsible-ai|security
level: beginner                # beginner | intermediate | advanced
lifecycle: stable              # stable | draft | deprecated
risk_level: low                # low|medium|high|critical — harm if followed without review, not a quality score
evidence_level: established-practice   # primary|official-documentation|established-practice|heuristic|opinion
last_verified: 2026-09-21      # YYYY-MM-DD — last review date, NOT a live-verification claim
capabilities: [specific-tag-one, specific-tag-two]   # feeds scripts/router.py — be specific, not generic
requires:                      # rare hard prerequisites; leave empty (key, no value) if none
conflicts:                     # rare contradicting siblings; leave empty (key, no value) if none
related: [sibling-a, sibling-b]
inputs: One-line description of what this skill expects.
outputs: One-line description of what this skill produces.
```

The description is plain text: no angle-bracket placeholders or tags (the validator's
`description-xml-tag` rule rejects anything that looks like `<tag>`, as Claude's Agent
Skills rules require), at most 1,024 characters, and at least 3 quoted trigger phrases.

Any heuristic (a rule with real exceptions) MUST be phrased conditionally in
the body — "as a rule of thumb, above roughly N..., consider..." — never as an
unqualified universal law. See `docs/EVIDENCE.md` for the test and three
worked before/after examples. After editing a skill, run
`python scripts/validate_skills.py` and fix everything it reports.

## Section contract
- **Every skill:** `## Overview` (2–4 sentences: what it covers, when it fires, what the
  user walks away with).
- **Workflow skills:** `## Workflow` → `## Gotchas` → `## References`.
- **Reference skills:** `## Key Concepts` → `## Gotchas` → `## References`.
- `## References` = 2–4 authoritative links (papers, official docs), each with a one-line reason.

## Definition of Done (per skill)
- [ ] Folder name == front-matter `name` == `INDEX.md` slug.
- [ ] `type` matches the `[W]`/`[R]` tag in `INDEX.md`.
- [ ] `description` is trigger-oriented ("Use when…") with 3+ concrete phrases and a NOT-for clause.
- [ ] Correct section set for the type (Workflow vs Key Concepts).
- [ ] Workflow skills: every step has runnable code; no pseudo-code placeholders.
- [ ] `## Gotchas` has ≥3 real, specific pitfalls (not generic advice).
- [ ] `## References` has ≥2 real, resolvable links.
- [ ] `related` lists real sibling slugs that exist (or are planned) in `INDEX.md`.
- [ ] `lifecycle`/`risk_level`/`evidence_level`/`last_verified`/`capabilities`/`requires`/`conflicts`/`inputs`/`outputs` are all present and justified (see `docs/SKILL-SPEC.md`, `docs/EVIDENCE.md`).
- [ ] No claim with real exceptions is phrased as a universal law — heuristics are conditional in the body.
- [ ] `python scripts/validate_skills.py --warnings` reports zero errors for this skill, and `python scripts/check_freshness.py` reports no STALE finding.
- [ ] Code-bearing skills: `tests/examples/test_<slug_with_underscores>.py` runs the skill's own blocks via `tests/examples/harness.py` (blocks that cannot run standalone start with `# illustrative`), and passes in the example environment (`requirements-examples.txt`; see `docs/EXAMPLE-TESTS.md`).
- [ ] `evals/<slug>/` has a `plan.json`, at least one non-happy-path case, and pass/fail fixtures, including an on-topic wrong answer that a `must_not_include` pattern catches. Workflow skills also have a `kind: executable` case. `python scripts/validate_evals.py --strict --coverage` reports zero errors and warnings (see `evals/SCHEMA.md`).
- [ ] `evals/routing/benchmark.json` has cases expecting this skill in both the dev and test splits; `python scripts/eval_router.py --split dev` was checked, and any re-baseline is deliberate and explained (see `evals/routing/README.md`).
- [ ] Checkable API/version claims the skill relies on are recorded in `docs/claims.json` with the verification actually performed (`unverified` is fine); `python scripts/check_claims.py` reports zero errors.
- [ ] Generated files are regenerated, never hand-edited: `python scripts/generate_index.py --fix`, `python scripts/generate_plugins.py --write`, `python scripts/generate_readme_stats.py --write`, `python scripts/build_dependency_graph.py --write`, `python scripts/generate_schema.py --write` (needed after any `schema/allowed_values.py` change), then `python scripts/check_generated.py` reports no drift.
- [ ] `python scripts/check_contract.py --check` passes (after `--write` for an intended contract change; removing or renaming a skill is breaking, see `docs/RELEASE.md`), and `CHANGELOG.md` has an `[Unreleased]` entry.

## Workflow (how to work)
1. Open `INDEX.md`. Work top-to-bottom through unchecked (`⬜`) items.
2. For each skill: `mkdir <slug>/`, copy `_TEMPLATE/SKILL.md` into `<slug>/SKILL.md`, fill it.
3. Match the gold-standard example that corresponds to the skill's type.
4. Keep skills **focused** — link to siblings via `related` instead of duplicating content
   (e.g. `data-preprocessing` must not re-teach pandas — that's `python-for-ml`).
5. After each skill, run the Definition-of-Done commands above (`generate_index.py --fix`
   updates `INDEX.md`'s checkbox and progress counter). Commit per skill or per domain with
   message `add(<domain>): <slug> skill`. `CONTRIBUTING.md` walks through the same steps
   with examples and a review checklist.

## Guardrails
- Do **not** edit `README.md`, `_TEMPLATE/SKILL.md`, or the two gold-standard examples
  unless explicitly told to.
- Do **not** invent references — if you can't cite a real, well-known source, note it and move on.
- Do **not** collapse multiple skills into one file, and do **not** nest folders deeper than one level.
- Prefer accuracy over length. A tight, correct skill beats a padded one.
- Do **not** claim more than was run: "tested", "verified" and "executed against X" need the
  run that backs them. Passing the validator shows structure only, not that the advice is right.

## Conventions
- Naming: lowercase-kebab, domain-prefixed only where it aids sorting (`llm-rag-pipeline`).
- Code: Python 3.10+, prefer scikit-learn / PyTorch / Hugging Face idioms; runnable as-is.
- Math: inline LaTeX where it clarifies (`\( … \)`), never as decoration.
- One `SKILL.md` per folder. No `README` inside skill folders.
