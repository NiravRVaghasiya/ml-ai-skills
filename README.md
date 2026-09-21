# ML / AI Skills Library

A curated, clone-able collection of Machine Learning & AI skill files. Two kinds of file live here:

- **Workflow skills** — actionable, step-by-step procedures the agent *executes* (train a model, build a RAG pipeline). File pattern: `<name>/SKILL.md` with `## Overview` + `## Workflow`.
- **Reference skills** — explainer/knowledge cards the agent *loads for context* (attention explained, bias-variance). File pattern: `<name>/SKILL.md` with `## Overview` + `## Key Concepts`.

## How to use this library
1. Copy `_TEMPLATE/SKILL.md` into a new folder named after the skill (lowercase-kebab).
2. Fill the front-matter — the `description` field is what makes the skill discoverable, so write it as *"use when the user wants to…"* with concrete trigger phrases.
3. Keep each skill focused. Link to sibling skills instead of duplicating content.
4. Embed runnable code in workflow skills — copy-paste, not just prose.

## Conventions
- **Naming:** lowercase-kebab, domain-prefixed where helpful (`llm-rag-pipeline`).
- **Required sections:** every skill needs `## Overview`. Workflow skills also need `## Workflow`.
- **Structure:** `## Overview` → `## Workflow` (or `## Key Concepts`) → `## Gotchas` → `## References`.
- **Depth:** progressive — don't re-explain pandas in a modeling skill.

## Fully-written examples
- `data-preprocessing/` — a complete **workflow** skill (clone this for procedures).
- `attention-mechanisms/` — a complete **reference** skill (clone this for explainers).

See `INDEX.md` for the full catalog and build checklist.
