# Quality gates

A skill is "done" when it passes every gate below. Gates marked **(CI)** are
mechanically enforced by `.github/workflows/ci.yml`; gates without that marker
require human/agent judgment recorded in `docs/SKILL-AUDIT.md`.

## Structural
- [ ] **(CI)** Frontmatter has every required key from `schema/allowed_values.py`
      `REQUIRED_KEYS`, with valid enum values (`scripts/validate_skills.py`).
- [ ] **(CI)** Folder name == frontmatter `name` == INDEX.md slug
      (`scripts/validate_skills.py`, `scripts/generate_index.py --check`).
- [ ] **(CI)** `related`/`requires`/`conflicts` reference real skill slugs, and
      `requires` contains no cycles (`scripts/validate_skills.py`,
      `scripts/build_dependency_graph.py`).

## Content
- [ ] Purpose is clear from `## Overview` alone (no need to read the whole file
      to know when this skill fires).
- [ ] Scope boundaries are explicit — the description's "NOT for..." clause
      names the correct sibling for every adjacent concern **(CI checks the
      clause exists; a human/agent checks it names the RIGHT sibling)**.
- [ ] **(CI)** Correct section set for the declared `type` — no `## Workflow` on
      a reference skill, no `## Key Concepts` on a workflow skill.
- [ ] Workflow steps are actionable and every step's code is runnable, not
      pseudo-code **(CI does a best-effort `ast.parse` check as a WARNING; a
      human/agent must confirm the code actually does what the step claims)**.

## Evidence
- [ ] `evidence_level` reflects the dominant tier of the skill's claims, not
      just copied from a sibling.
- [ ] Heuristics are phrased conditionally in the body ("as a starting point...",
      "above roughly N, consider...") — never as a universal rule ("always do X").
- [ ] **(CI)** `## References` has ≥2 real, resolvable links, each with a
      one-line reason.

## Execution
- [ ] Code examples were read for import correctness and realistic API usage
      by a human/agent reviewer — **(CI)** only catches syntax errors as
      WARNINGs and known-dead APIs as `STALE` errors (`scripts/check_freshness.py`);
      it cannot confirm semantic correctness.

## Evaluation
- [ ] At least the highest-traffic/highest-risk skills have a representative
      case in `evals/<slug>/` covering at least one non-happy-path category
      (edge_case/adversarial/misconception/failure_recovery/ambiguity) — **(CI)**
      only validates case-file structure (`scripts/validate_evals.py`), not
      behavior; see `evals/SCHEMA.md`.

## Maintenance
- [ ] **(CI, informational only)** `last_verified` is set and not more than 180
      days old (`scripts/check_freshness.py` `REVIEW_DUE`).
- [ ] Code-bearing skills declare `version_constraints` for the packages their
      Workflow actually imports, honestly labeled per docs/SKILL-SPEC.md.

## Security
- [ ] If the skill's Workflow executes code with real-world side effects
      (deployment, deletion, sending data externally, granting tool access),
      `risk_level` is `medium` or higher and the Gotchas name the specific
      failure mode, not just "be careful."
- [ ] If the skill touches retrieved/third-party content reaching a model's
      context, it references [[ai-ml-security]] rather than re-deriving
      injection guidance inline.
