# ML / AI Skills Library

A curated, clone-able collection of **35** Machine Learning & AI skill files — one self-contained `SKILL.md` per folder, organized across 7 domains and built to a single production-grade standard.

Two kinds of skill live here:

- **⚙️ Workflow skills (W)** — actionable, step-by-step procedures the agent *executes* (train a model, build a RAG pipeline). Pattern: `<name>/SKILL.md` with `## Overview` + `## Workflow` (numbered steps with runnable, copy-paste code).
- **📖 Reference skills (R)** — explainer/knowledge cards the agent *loads for context* (attention explained, bias–variance). Pattern: `<name>/SKILL.md` with `## Overview` + `## Key Concepts`.

## Library at a glance

| | Count |
|---|---|
| **Total skills** | **35** |
| Workflow (⚙️ W) | 23 |
| Reference (📖 R) | 12 |
| Domains | 7 |
| Levels | beginner (5) · intermediate (27) · advanced (3) |

Repo layout:

```
ml-ai-skills/
├── README.md            ← you are here (conventions + full catalog)
├── INDEX.md             ← master checklist + build status
├── CLAUDE.md            ← operating manual for authoring skills
├── _TEMPLATE/SKILL.md   ← clone this for every new skill
└── <skill-slug>/SKILL.md   × 35
```

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

## How to use this library
1. Copy `_TEMPLATE/SKILL.md` into a new folder named after the skill (lowercase-kebab).
2. Fill the front-matter — the `description` field is what makes the skill discoverable, so write it as *"use when the user wants to…"* with concrete trigger phrases.
3. Keep each skill focused. Link to sibling skills via `related` instead of duplicating content.
4. Embed runnable code in workflow skills — copy-paste, not just prose.
5. Flip the item to ✅ in `INDEX.md` and bump the progress counter.

## Conventions
- **Naming:** lowercase-kebab, domain-prefixed where helpful (`llm-rag-pipeline`). Folder name **must** equal front-matter `name`.
- **Required sections:** every skill needs `## Overview`. Workflow skills also need `## Workflow`; reference skills need `## Key Concepts`.
- **Structure:** `## Overview` → `## Workflow` (or `## Key Concepts`) → `## Gotchas` → `## References`.
- **Depth:** progressive — don't re-explain pandas in a modeling skill; link to `python-for-ml` instead.
- **Code:** Python 3.10+, scikit-learn / PyTorch / Hugging Face idioms, runnable as-is.

## Gold-standard examples
- `data-preprocessing/` — a complete **workflow** skill (clone this for procedures).
- `attention-mechanisms/` — a complete **reference** skill (clone this for explainers).

See `INDEX.md` for the master checklist and `CLAUDE.md` for the full authoring contract and Definition of Done.
