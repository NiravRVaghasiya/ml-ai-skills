# Master Prompt — paste into Claude Code CLI

> Run from the repo root (`ml-ai-skills/`). This is the single kickoff prompt.

---

You are building out an ML/AI skills library in this repository. Before doing anything,
read `CLAUDE.md`, `README.md`, `INDEX.md`, and the two gold-standard examples
(`data-preprocessing/SKILL.md` = workflow standard, `attention-mechanisms/SKILL.md` =
reference standard). Treat `CLAUDE.md` as binding.

Your task: author every unchecked (`⬜`) skill in `INDEX.md` — all 28 — one folder per
skill, each containing a single `SKILL.md` cloned from `_TEMPLATE/SKILL.md`. Work
autonomously in one pass, without pausing for confirmation between skills or domains.

Rules (from CLAUDE.md — enforce them):
- Match the type tag in INDEX.md exactly: `[W]` skills need `## Overview` + `## Workflow`
  (numbered steps, runnable copy-paste code); `[R]` skills need `## Overview` +
  `## Key Concepts` (no Workflow).
- Front-matter `name` must equal the folder name and the INDEX slug; `type` must match the tag.
- `description` must be trigger-oriented: "Use when the user wants to…", 3+ concrete
  trigger phrases, and a "NOT for…" clause pointing to the correct sibling skill.
- `## Gotchas` = 3+ specific, real pitfalls. `## References` = 2+ real, resolvable links.
- Keep skills focused; link siblings via `related` instead of duplicating content.
- Do NOT modify README.md, _TEMPLATE/SKILL.md, or the two gold-standard examples.
- Match the depth and code density of the gold-standard examples — no thin stubs.

Process (single autonomous pass, in INDEX order — Foundations → Classical ML →
Deep Learning → LLMs → Specialized → MLOps → Responsible AI):
1. Work top-to-bottom through every `⬜` item without stopping.
2. For each skill: create `<slug>/`, copy `_TEMPLATE/SKILL.md` into `<slug>/SKILL.md`,
   and fill it to the Definition of Done in CLAUDE.md.
3. After each skill, flip its `⬜` to `✅` in INDEX.md and update the progress counter.
4. Commit per skill (or per domain) with message `add(<domain>): <slug> skill`.
5. Only after ALL 28 skills are written, run ONE comprehensive self-check across the
   whole repo: verify every SKILL.md has correct front-matter, folder-name/slug/type
   consistency with INDEX.md, the right section set for its type, ≥3 gotchas, and
   ≥2 references. Produce a final report listing every skill and pass/fail, fix all
   failures, and confirm INDEX.md shows 30/30 ✅.

Do not stop until the entire library is complete and the final self-check passes.
Begin now.
