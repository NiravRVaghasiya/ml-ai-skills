# ML / AI Skills Library

A curated, clone-able collection of **38** Machine Learning & AI skill files — one self-contained `SKILL.md` per folder, organized across 8 domains — plus the tooling that makes the collection routable, validated, evidence-audited, and testable rather than just a pile of Markdown. See "Architecture" below for how the pieces fit together, and "What this is NOT" for the claims this repo deliberately does not make.

Two kinds of skill live here:

- **⚙️ Workflow skills (W)** — actionable, step-by-step procedures the agent *executes* (train a model, build a RAG pipeline). Pattern: `<name>/SKILL.md` with `## Overview` + `## Workflow` (numbered steps with runnable, copy-paste code).
- **📖 Reference skills (R)** — explainer/knowledge cards the agent *loads for context* (attention explained, bias–variance). Pattern: `<name>/SKILL.md` with `## Overview` + `## Key Concepts`.

## Library at a glance

| | Count |
|---|---|
| **Total skills** | **38** |
| Workflow (⚙️ W) | 25 |
| Reference (📖 R) | 13 |
| Domains | 8 |
| Levels | beginner (5) · intermediate (30) · advanced (3) |
| Risk level | low (24) · medium (12) · high (2) |
| Evidence level | established-practice (25) · official-documentation (10) · primary (3) |

Risk/evidence counts are self-reported per-skill frontmatter, audited once (2026-09-21) — see `docs/SKILL-AUDIT.md`, not a live guarantee. Regenerate with:
```bash
python3 -c "
import sys; sys.path.insert(0, 'scripts')
import skills_lib as sl
from collections import Counter
skills = sl.load_all_skills()
print(Counter(s.frontmatter.get('risk_level') for s in skills))
"
```

Repo layout:

```
ml-ai-skills/
├── README.md            ← you are here (conventions + full catalog)
├── INDEX.md             ← master checklist + build status (kept in sync by scripts/generate_index.py)
├── CLAUDE.md            ← operating manual for authoring a skill
├── _TEMPLATE/SKILL.md   ← clone this for every new skill
├── docs/                ← formal spec, evidence policy, routing/freshness/reproducibility docs, audit report
├── schema/               ← allowed_values.py — single source of truth for every enum
├── scripts/              ← validator, router, dependency-graph builder, freshness scanner, index sync
├── tests/                ← unit tests for the scripts above (stdlib unittest, no extra deps)
├── evals/                ← structured behavioral test cases per skill (see evals/SCHEMA.md)
├── .github/workflows/    ← CI: validation, index/graph consistency, tests, eval-file sanity
└── <skill-slug>/SKILL.md   × 38
```

## Architecture

This repo is built around one pipeline, and every piece of tooling below exists
to support one stage of it:

```
USER TASK → SKILL ROUTER → SKILL DEPENDENCY GRAPH → EVIDENCE-BACKED SKILLS
          → WORKFLOW EXECUTION → TOOL/CODE EXECUTION → EVALUATION → VERIFIED OUTPUT
```

- **Router** (`scripts/router.py`) — maps free text to an ordered list of
  relevant skills without loading the whole library. Deterministic, keyword/tag-
  based, no embeddings. See `docs/ROUTING.md` for the algorithm and its
  documented failure mode.
- **Dependency graph** (`scripts/build_dependency_graph.py`) — makes
  `requires`/`related`/`conflicts` relationships explicit and checks for cycles/
  orphans. Rendered at `docs/SKILL-GRAPH.md`.
- **Evidence-backed skills** — every skill's frontmatter declares an
  `evidence_level` and `risk_level`; heuristics are phrased conditionally in the
  body, not as universal laws. See `docs/EVIDENCE.md`.
- **Validation** (`scripts/validate_skills.py`) — enforces the frontmatter/
  section contract in `docs/SKILL-SPEC.md` and fails CI on real problems, not on
  noise (see that doc's "Validation philosophy").
- **Freshness** (`scripts/check_freshness.py`) — flags known-dead APIs and
  stale review dates. Explicitly does not (and cannot, without live network
  access) verify current correctness — see `docs/FRESHNESS.md`.
- **Evaluation** (`evals/`) — structured behavioral test cases per skill,
  covering happy-path/edge/adversarial/misconception/failure-recovery/
  ambiguity categories. Structurally validated by `scripts/validate_evals.py`;
  behaviorally graded by a human/agent reader, not automated — see
  `evals/SCHEMA.md` for exactly what is and isn't automated here.

## Catalog

Legend: **⚙️ W** = workflow skill · **📖 R** = reference skill

### 1. Foundations

| Skill | Type | Level | What it covers |
|---|---|---|---|
| [`ml-math-essentials`](ml-math-essentials/SKILL.md) | 📖 R | beginner | A compact cheat-reference for the three branches of math that show up constantly in ML: linear algebra (how… |
| [`ml-problem-framing`](ml-problem-framing/SKILL.md) | ⚙️ W | intermediate | Converts an ambiguous business request ("reduce churn," "flag bad transactions") into a precisely specified,… |
| [`python-for-ml`](python-for-ml/SKILL.md) | 📖 R | beginner | The idioms and gotchas of numpy and pandas that separate fast, correct numerical Python from code that silently… |
| [`statistics-for-ml`](statistics-for-ml/SKILL.md) | 📖 R | intermediate | Covers the statistical toolkit ML practitioners lean on outside of the model-fitting step itself: knowing which… |

### 2. Classical ML

| Skill | Type | Level | What it covers |
|---|---|---|---|
| [`data-preprocessing`](data-preprocessing/SKILL.md) | ⚙️ W | intermediate | Turns a raw tabular dataset into a clean, numeric, leakage-free matrix ready for modeling |
| [`feature-engineering`](feature-engineering/SKILL.md) | ⚙️ W | intermediate | Turns a clean, numeric-ready dataset into a set of *predictive* features and then trims that set down to the… |
| [`hyperparameter-tuning`](hyperparameter-tuning/SKILL.md) | ⚙️ W | advanced | Covers searching a model's hyperparameter space efficiently and getting an *unbiased* estimate of how the tuned… |
| [`model-evaluation`](model-evaluation/SKILL.md) | ⚙️ W | intermediate | Covers how to *score* a model honestly: picking a metric that matches the business problem, choosing a… |
| [`supervised-learning`](supervised-learning/SKILL.md) | ⚙️ W | intermediate | Goes from a clean, feature-engineered matrix to a fitted model, via a disciplined spot-check: always start with… |
| [`unsupervised-learning`](unsupervised-learning/SKILL.md) | ⚙️ W | intermediate | Covers the two core unsupervised tasks on tabular data: clustering (finding groups with no labels) and… |

### 3. Deep Learning

| Skill | Type | Level | What it covers |
|---|---|---|---|
| [`attention-mechanisms`](attention-mechanisms/SKILL.md) | 📖 R | intermediate | Attention lets a model decide, for each token, which other tokens matter most — replacing the fixed, sequential… |
| [`cnn-vision`](cnn-vision/SKILL.md) | 📖 R | intermediate | Convolutional neural networks exploit the structure of images — nearby pixels are related, and a pattern (an… |
| [`neural-net-fundamentals`](neural-net-fundamentals/SKILL.md) | 📖 R | beginner | Every neural network, regardless of architecture, learns the same way: a forward pass computes a prediction and… |
| [`pytorch-patterns`](pytorch-patterns/SKILL.md) | ⚙️ W | intermediate | Most PyTorch bugs come from deviating from a small set of idioms: how a `Dataset` feeds a `DataLoader`, when to… |
| [`rnn-sequence`](rnn-sequence/SKILL.md) | 📖 R | intermediate | Recurrent networks process a sequence one element at a time, carrying a hidden state forward as memory of… |
| [`training-deep-models`](training-deep-models/SKILL.md) | ⚙️ W | intermediate | Getting a model architecture right is only half of training a deep network well — the other half is the… |

### 4. LLMs & Generative AI

| Skill | Type | Level | What it covers |
|---|---|---|---|
| [`agents-and-tools`](agents-and-tools/SKILL.md) | 📖 R | intermediate | An LLM agent extends a single prompt/response call into a loop: the model decides which tool to call, observes… |
| [`fine-tuning-llms`](fine-tuning-llms/SKILL.md) | ⚙️ W | advanced | Fine-tuning updates an LLM's weights (or a small set of added weights) so behavior that a prompt alone can't… |
| [`llm-evaluation`](llm-evaluation/SKILL.md) | ⚙️ W | intermediate | Evaluating an LLM system means scoring open-ended, non-deterministic text output against a rubric or reference… |
| [`prompt-engineering`](prompt-engineering/SKILL.md) | 📖 R | beginner | Prompt engineering is the practice of shaping the *input* to an LLM — instructions, examples, formatting, and… |
| [`rag-pipeline`](rag-pipeline/SKILL.md) | ⚙️ W | intermediate | A RAG pipeline grounds an LLM's answers in an external corpus by retrieving relevant chunks at query time and… |
| [`agent-evaluation`](agent-evaluation/SKILL.md) | ⚙️ W | intermediate | Evaluates an agent's *behavior* over a task — tool selection, argument validity, loop/recovery, safety — as opposed to scoring one generated text |
| [`rag-evaluation`](rag-evaluation/SKILL.md) | ⚙️ W | intermediate | Measures a RAG system specifically — retrieval metrics (Recall@k/MRR/nDCG), faithfulness, citation correctness, and a named RAG failure-mode taxonomy |

### 5. Specialized Domains

| Skill | Type | Level | What it covers |
|---|---|---|---|
| [`computer-vision`](computer-vision/SKILL.md) | ⚙️ W | intermediate | Covers the applied computer-vision pipeline: augmenting an image dataset correctly, fine-tuning a pretrained… |
| [`nlp-tasks`](nlp-tasks/SKILL.md) | ⚙️ W | intermediate | Covers the four workhorse NLP tasks — tokenization, named entity recognition (NER), text classification, and… |
| [`recommender-systems`](recommender-systems/SKILL.md) | ⚙️ W | intermediate | Covers building recommenders from both ends: content-based filtering (item similarity from features/text) and… |
| [`reinforcement-learning`](reinforcement-learning/SKILL.md) | 📖 R | intermediate | Reinforcement learning (RL) is the framework for learning to act in an environment through trial and error,… |
| [`time-series`](time-series/SKILL.md) | ⚙️ W | intermediate | Covers the forecasting-specific pipeline: getting a series onto a clean fixed-frequency index, decomposing… |

### 6. MLOps & Production

| Skill | Type | Level | What it covers |
|---|---|---|---|
| [`ci-cd-for-ml`](ci-cd-for-ml/SKILL.md) | ⚙️ W | intermediate | Applies standard software CI/CD discipline to an ML codebase, plus the ML-specific pieces vanilla CI/CD doesn't… |
| [`data-pipelines`](data-pipelines/SKILL.md) | ⚙️ W | intermediate | Covers the recurring, scheduled infrastructure that keeps ML features fresh and consistent — not the one-time… |
| [`experiment-tracking`](experiment-tracking/SKILL.md) | ⚙️ W | beginner | Turns ad-hoc training scripts into a searchable history of runs: every hyperparameter, metric, and artifact… |
| [`ml-monitoring`](ml-monitoring/SKILL.md) | ⚙️ W | intermediate | Watches a model *after* it's serving live traffic: are incoming features drifting away from what the model was… |
| [`model-deployment`](model-deployment/SKILL.md) | ⚙️ W | intermediate | Takes a trained model artifact from a notebook or training job to a running network endpoint that answers real… |
| [`model-optimization`](model-optimization/SKILL.md) | ⚙️ W | advanced | Takes a trained model and reduces its size/latency footprint before it goes behind an endpoint — quantization,… |

### 7. Responsible AI

| Skill | Type | Level | What it covers |
|---|---|---|---|
| [`ai-ethics-fairness`](ai-ethics-fairness/SKILL.md) | 📖 R | intermediate | Covers how bias enters ML systems and how to quantify fairness once a model exists — which group-fairness… |
| [`data-privacy`](data-privacy/SKILL.md) | 📖 R | intermediate | Covers how personal data ends up identifiable even after "cleaning," and the main technical tools for reducing… |
| [`explainability`](explainability/SKILL.md) | ⚙️ W | intermediate | Turns an opaque trained model into explanations a human can act on: global feature-importance rankings and… |

### 8. AI/ML Security

| Skill | Type | Level | What it covers |
|---|---|---|---|
| [`ai-ml-security`](ai-ml-security/SKILL.md) | 📖 R | intermediate | Prompt injection (direct/indirect/RAG-document), data poisoning vs. model supply-chain risk, insecure deserialization, excessive agent permissions, tenant isolation — explicitly distinguished from responsible-AI *policy* concerns like fairness/privacy |

## How to use this library
1. Copy `_TEMPLATE/SKILL.md` into a new folder named after the skill (lowercase-kebab).
2. Fill the front-matter per `docs/SKILL-SPEC.md` — `description` is what makes the skill discoverable, so write it as *"use when the user wants to…"* with 3+ concrete trigger phrases and a "NOT for…" clause; also set `lifecycle`/`risk_level`/`evidence_level`/`last_verified`/`capabilities`/`requires`/`conflicts`/`inputs`/`outputs`.
3. Keep each skill focused. Link to sibling skills via `related` instead of duplicating content; use `requires` only for a genuine hard prerequisite.
4. Embed runnable code in workflow skills — copy-paste, not just prose. Phrase any heuristic conditionally, not as a universal law — see `docs/EVIDENCE.md`.
5. Run `python scripts/validate_skills.py` until it reports zero errors for your skill, then `python scripts/generate_index.py --fix` to sync `INDEX.md`.
6. Add at least one case to `evals/<your-skill>/` covering a non-happy-path category (see `evals/SCHEMA.md`), then `python scripts/validate_evals.py`.

## Validating, routing, and testing

```bash
python scripts/validate_skills.py --warnings   # frontmatter/section/relationship checks
python scripts/generate_index.py --check       # INDEX.md drift check
python scripts/build_dependency_graph.py --check   # regenerate docs/SKILL-GRAPH.md if this fails
python scripts/check_freshness.py              # known-dead-API + staleness scan (non-blocking except STALE)
python scripts/validate_evals.py               # eval case-file sanity check
python -m unittest discover -s tests -p "test_*.py"   # unit tests for all of the above (stdlib only, no pip install)
python scripts/router.py "your task description"      # see which skills would be routed to
```

All of the above run in `.github/workflows/ci.yml` on every push/PR (the live-link-check job is manual-dispatch only — see `docs/FRESHNESS.md`).

## Conventions
- **Naming:** lowercase-kebab, domain-prefixed where helpful (`llm-rag-pipeline`). Folder name **must** equal front-matter `name`.
- **Required sections:** every skill needs `## Overview`. Workflow skills also need `## Workflow`; reference skills need `## Key Concepts`. Full contract: `docs/SKILL-SPEC.md`.
- **Structure:** `## Overview` → `## Workflow` (or `## Key Concepts`) → `## Gotchas` → `## References`.
- **Depth:** progressive — don't re-explain pandas in a modeling skill; link to `python-for-ml` instead.
- **Code:** Python 3.10+, scikit-learn / PyTorch / Hugging Face idioms, runnable as-is.
- **Evidence:** every claim is implicitly categorized by the skill's `evidence_level`; a claim with real exceptions must be phrased conditionally in the body, never as an unqualified universal rule. See `docs/EVIDENCE.md`.

## Gold-standard examples
- `data-preprocessing/` — a complete **workflow** skill (clone this for procedures).
- `attention-mechanisms/` — a complete **reference** skill (clone this for explainers).

## What this is NOT
- **Not a benchmark suite.** `evals/` holds structured, sanity-checked *case
  definitions* (`scripts/validate_evals.py` confirms they're well-formed and
  point at a real skill) — no runner in this repo executes an agent against
  them and produces a score. Grading a transcript against a case's
  `expected_behavior` is a human/agent review step. See `evals/SCHEMA.md`.
- **Not a verified-current API reference.** `version_constraints` and
  `last_verified` are honesty-labeled, model-knowledge-at-authoring-time
  estimates, not live package/API checks — this repo has no scheduled job
  that actually installs the referenced packages. See `docs/FRESHNESS.md`.
- **Not a semantic/embedding-based retrieval system.** `scripts/router.py` is
  a deterministic keyword/tag matcher with a documented, demonstrated
  vocabulary-collision failure mode. See `docs/ROUTING.md`.
- **Not "production-ready" in the sense of having been deployed anywhere.**
  Skills describe disciplined, evidence-classified procedures; nothing here
  has been run against a real production workload as part of authoring it.

## Limitations & further reading
- `docs/SKILL-SPEC.md` — the full frontmatter/section contract and its "Known limitations" section (evidence granularity, router accuracy, no live verification, no automated grading).
- `docs/SKILL-AUDIT.md` — the Phase 15 per-skill audit: risk/evidence classification rationale and every claim that was rewritten, with before/after text.
- `docs/EVIDENCE.md`, `docs/FRESHNESS.md`, `docs/REPRODUCIBILITY.md`, `docs/ROUTING.md`, `docs/QUALITY-GATES.md` — the policy documents behind each part of the pipeline above.

See `INDEX.md` for the master checklist and `CLAUDE.md` for the full authoring contract and Definition of Done.
