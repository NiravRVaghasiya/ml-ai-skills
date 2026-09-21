# Formal Skill Specification

This is the binding contract every `SKILL.md` must satisfy. `scripts/validate_skills.py`
enforces the parts of it that are mechanically checkable; the rest (Phase 15 audit
judgments like "is the scope appropriate") is enforced by human/agent review and
recorded in `docs/SKILL-AUDIT.md`.

Allowed enum values live in exactly one place: `schema/allowed_values.py`. If you're
about to hardcode a list of domains or risk levels somewhere else, import it from
there instead.

## Frontmatter contract

```yaml
name: kebab-case-name          # MUST equal the folder name
display_name: Human Readable Name
description: >                  # "Use when the user wants to <X>." + 3+ quoted
  Use when the user wants to <X>. Trigger phrases: "<p1>", "<p2>", "<p3>".
  NOT for <adjacent thing owned by another skill>.
type: workflow                 # workflow | reference
domain: classical-ml           # see schema/allowed_values.py DOMAIN
level: beginner                # beginner | intermediate | advanced
lifecycle: stable              # stable | draft | deprecated
risk_level: low                # low | medium | high | critical
evidence_level: established-practice   # primary | official-documentation |
                                        # established-practice | heuristic | opinion
last_verified: 2026-09-21      # YYYY-MM-DD — see "What last_verified means" below
capabilities:                  # routing tags — short noun/verb phrases, used by
  - thing-one                  # scripts/router.py; NOT free-form keywords, be
  - thing-two                  # specific enough to disambiguate from siblings
requires:                      # HARD prerequisites — leave empty unless the skill
  - other-skill-slug           # genuinely cannot be applied without another skill's
                                # output/context already established. Most skills
                                # have none; use `related` for soft pointers instead.
conflicts:                     # skills that give contradictory guidance for the
                                # same situation. Expected to be empty for nearly
                                # every skill; exists so a real audit finding has
                                # somewhere structured to go instead of prose.
related:
  - sibling-skill-one
  - sibling-skill-two
inputs: One-line description of what the skill expects as input.
outputs: One-line description of what the skill produces.
version_constraints:           # OPTIONAL — only for skills whose Workflow embeds
  - "package: note"            # code against a specific package/API surface. See
                                # "version_constraints honesty rule" below.
---
```

### Field-by-field notes

- **`lifecycle`** — `stable` (default for anything meeting the Definition of Done
  below), `draft` (written but not yet evidence/freshness-reviewed to the `stable`
  bar), `deprecated` (kept for backward compatibility; the router should prefer
  whatever it's superseded by — track that via a note in the skill body, since
  `supersedes` was considered and dropped as a separate field to avoid a rarely-used
  duplicate of `conflicts`; if it's ever needed for real, add it back deliberately).
- **`risk_level`** — risk of *harm if the guidance/code is followed without
  additional review*, not a quality score. `model-deployment` is `high` even when
  perfectly written, because a mistake there can cause a production outage;
  `attention-mechanisms` is `low` because acting on it has no side effects.
- **`evidence_level`** — the *dominant* tier backing the skill's substantive claims,
  not a claim that every sentence is independently sourced:
  - `primary` — grounded in a specific original paper/standard.
  - `official-documentation` — grounded in vendor/framework docs as the primary source.
  - `established-practice` — widely taught/consensus knowledge, not tied to one paper.
  - `heuristic` — a rule of thumb with known exceptions (the skill body MUST phrase
    it conditionally, e.g. "as a starting point, above roughly N categories,
    consider..." — never as a universal law like "always do X").
  - `opinion` — a style/preference call with no objective backing; used rarely.
- **What `last_verified` means.** It is the date an agent/human last *reviewed this
  skill's structure and claims for accuracy to the best of the reviewer's
  knowledge*. It is explicitly **NOT** "this code was run against the current
  version of every referenced package/API today" — nobody in this repo's authoring
  process has had live network/package-registry access to confirm that. Treat it as
  a staleness clock, not a correctness certificate. See `docs/FRESHNESS.md`.
- **`version_constraints` honesty rule.** Every entry must make clear whether it
  was live-verified this session. In practice, it wasn't (see above) — entries are
  model-knowledge estimates of when an API/behavior became available (e.g. "scikit-
  learn: `OneHotEncoder(handle_unknown='ignore')` available since 0.20") and must be
  phrased as such, not asserted as a currently-verified fact. If you're not
  reasonably confident of a specific milestone, omit the entry rather than invent one
  — an absent `version_constraints` list is honest; a fabricated version number is not.
- **`requires` vs `related`.** `requires` is rare and strict: a directed, acyclic,
  "cannot be meaningfully applied without" relationship (checked by
  `scripts/build_dependency_graph.py`). `related` is the common case: a soft
  sibling pointer with no ordering or cycle constraint. When in doubt, use `related`.

## Section contract (unchanged from the original authoring pass)

- **Every skill:** `## Overview` (2–4 sentences).
- **Workflow skills:** `## Overview` → `## Workflow` → `## Gotchas` → `## References`.
  No `## Key Concepts` section.
- **Reference skills:** `## Overview` → `## Key Concepts` → `## Gotchas` → `## References`.
  No `## Workflow` section.
- `## Gotchas` ≥ 3 specific, real pitfalls. `## References` ≥ 2 real, resolvable links.

## Validation philosophy

`scripts/validate_skills.py` draws a hard line between ERROR (blocks CI) and
WARNING (informational):

- **ERROR** — anything the spec makes non-negotiable and that is checkable without
  false positives: required frontmatter present with valid enum values, correct
  section set for the declared type, minimum gotcha/reference counts, `related`/
  `requires`/`conflicts` pointing at real skills, no dependency cycles, `name`
  matching the folder, INDEX.md matching the actual skill set.
- **WARNING** — signals with a real false-positive rate given a hand-rolled parser
  and intentionally partial code snippets: a Python block that fails `ast.parse`
  (could be a deliberately truncated illustrative fragment), fewer code blocks than
  numbered steps (a step can legitimately continue the previous block's code),
  an unreachable reference URL when checked live (sites rate-limit/flake).

This split exists so CI failing actually means something is wrong, instead of
training everyone to ignore CI because it's noisy.

## What `evals/` is and is not

See `evals/SCHEMA.md`. In short: it is a structured set of behavioral test cases
with a sanity-checked format (`scripts/validate_evals.py` confirms every case is
well-formed and points at a real skill). It is **not** an automated benchmark
that runs a model and produces a pass rate — no such runner exists in this repo,
and no scores anywhere in this repository were produced by one. Grading a case
against a live agent transcript is a human/agent review step, done by a reader,
not a script.

## Known limitations of this spec/tooling (stated explicitly, not hidden)

1. **No live API/package verification.** `last_verified` and `version_constraints`
   are review-date and model-knowledge markers, not proof of current correctness.
   Closing this gap requires either network access during CI (adds flakiness/cost)
   or a scheduled external job that actually installs packages and imports/calls
   the referenced APIs — neither exists today.
2. **The router is a keyword/tag matcher, not a semantic retriever.** It will
   under- or over-match on vocabulary collisions between domains (e.g. "detection"
   meaning both "object detection" and "fraud detection" — see `docs/ROUTING.md`
   for the concrete failure case found during this repo's own testing). Fixing
   this well requires the `capabilities` tags to be precise and, likely eventually,
   an embedding-based fallback — not attempted here per the "don't overengineer"
   principle without a demonstrated need beyond one observed failure case.
3. **Evidence classification is skill-level, not claim-level.** `evidence_level`
   describes the dominant tier for a skill's claims as a whole; it does not tag
   every individual sentence. A skill classified `established-practice` can still
   contain a `primary`-sourced claim or a `heuristic` aside — the body text is
   expected to phrase heuristics conditionally regardless of the frontmatter tier.
4. **No automated behavioral grading.** See "What evals/ is and is not" above.
