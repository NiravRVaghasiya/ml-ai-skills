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
```yaml
name: kebab-case-name          # MUST equal the folder name
display_name: Human Readable Name
description: >                  # discovery-critical: "Use when the user wants to…"
  Use when the user wants to <X>. Trigger phrases: "<p1>", "<p2>", "<p3>".
  NOT for <adjacent thing owned by another skill>.
type: workflow                 # workflow | reference — MUST match the [W]/[R] tag in INDEX.md
domain: classical-ml           # foundations|classical-ml|deep-learning|llm|specialized|mlops|responsible-ai
level: beginner                # beginner | intermediate | advanced
related: [sibling-a, sibling-b]
```

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
- [ ] Flip the item's `⬜` to `✅` in `INDEX.md` and update the progress counter.

## Workflow (how to work)
1. Open `INDEX.md`. Work top-to-bottom through unchecked (`⬜`) items.
2. For each skill: `mkdir <slug>/`, copy `_TEMPLATE/SKILL.md` into `<slug>/SKILL.md`, fill it.
3. Match the gold-standard example that corresponds to the skill's type.
4. Keep skills **focused** — link to siblings via `related` instead of duplicating content
   (e.g. `data-preprocessing` must not re-teach pandas — that's `python-for-ml`).
5. After each skill, update `INDEX.md` (checkbox + progress counter). Commit per skill or
   per domain with message `add(<domain>): <slug> skill`.

## Guardrails
- Do **not** edit `README.md`, `_TEMPLATE/SKILL.md`, or the two gold-standard examples
  unless explicitly told to.
- Do **not** invent references — if you can't cite a real, well-known source, note it and move on.
- Do **not** collapse multiple skills into one file, and do **not** nest folders deeper than one level.
- Prefer accuracy over length. A tight, correct skill beats a padded one.

## Conventions
- Naming: lowercase-kebab, domain-prefixed only where it aids sorting (`llm-rag-pipeline`).
- Code: Python 3.10+, prefer scikit-learn / PyTorch / Hugging Face idioms; runnable as-is.
- Math: inline LaTeX where it clarifies (`\( … \)`), never as decoration.
- One `SKILL.md` per folder. No `README` inside skill folders.
