---
name: skill-name-here
display_name: Human Readable Name
description: >
  Use when the user wants to <do X>. Trigger phrases: "<phrase 1>",
  "<phrase 2>", "<phrase 3>". NOT for <adjacent thing that belongs to another skill>.
type: workflow            # workflow | reference
domain: classical-ml      # foundations | classical-ml | deep-learning | llm | specialized | mlops | responsible-ai | security
level: intermediate       # beginner | intermediate | advanced
lifecycle: stable         # stable | draft | deprecated
risk_level: low           # low | medium | high | critical — harm if followed without review, NOT a quality score
evidence_level: established-practice   # primary | official-documentation | established-practice | heuristic | opinion
last_verified: 2026-09-21 # YYYY-MM-DD — date of last human/agent review, NOT a live-verification claim
capabilities:             # 3-6 SPECIFIC kebab-case routing tags, not generic words (feeds scripts/router.py)
  - specific-tag-one
  - specific-tag-two
requires:                 # HARD prerequisites only (rare) — leave the key with nothing after it if none
conflicts:                # skills that give contradictory guidance for the same situation (rare) — same as above
related:
  - sibling-skill-one
  - sibling-skill-two
inputs: One-line description of what this skill expects as input.
outputs: One-line description of what this skill produces.
# version_constraints:    # OPTIONAL — only for code-heavy workflow skills; see docs/SKILL-SPEC.md's honesty rule
#   - "package: specific API, stable since version X (model-knowledge estimate, not live-verified)"
---

## Overview
<2–4 sentences: what this skill covers, when it fires, and what the user walks away with.>

<!-- ===== For WORKFLOW skills, keep this section. Delete for reference skills. ===== -->
## Workflow
1. **Step name** — what to do and why.
   ```python
   # runnable, copy-paste snippet
   ```
2. **Step name** — ...
3. **Step name** — ...

<!-- ===== For REFERENCE skills, use this section instead of Workflow. ===== -->
## Key Concepts
- **Concept** — crisp explanation + when it matters.
- **Concept** — ...

## Gotchas
- Common mistake and how to avoid it.
- Edge case worth flagging.

## References
- [Title](https://…) — one line on why it's worth reading.
