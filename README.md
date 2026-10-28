# ML / AI Skills Library

![Python 3.10+](https://img.shields.io/badge/python-3.10%2B-blue)
[![CI: GitHub Actions](https://img.shields.io/badge/CI-GitHub%20Actions-blue)](.github/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow)](LICENSE)

**38 self-contained ML/AI Agent Skills, checked by their own tooling and packaged as a native
Claude Code plugin marketplace.**

## Overview

Agents are only as good as the procedural knowledge they can load. This repo is a curated
library of ML/AI **skills**, one `SKILL.md` per topic, covering everything from data
preprocessing to LLM evaluation to AI/ML security. Each skill is either:

- a **⚙️ workflow**: a runnable, step-by-step procedure with copy-paste code; or
- a **📖 reference**: an explainer an agent loads for context.

It's built for two audiences at once:
- **Claude Code users** who want reviewed ML/AI guidance available while they work.
- **Anyone auditing or extending the library.** Every skill follows one formal spec,
  enforced by a dependency-free Python toolchain (validator, router, eval and benchmark
  runners, freshness and claims checkers, packaging generators).

It's a content + tooling repo: plain Markdown, YAML frontmatter, generated plugin manifests,
a bash installer, and stdlib-only Python. It is not a running service.

**Status: not production-certified.** The skills are guidance to review before use, not
verified code. The [Evidence](#evidence-what-has-and-has-not-been-checked) section lists
exactly what has been checked, and [`docs/KNOWN-ISSUES.md`](docs/KNOWN-ISSUES.md) lists what
has not.

## Features

- **38 skills across 8 domains**: foundations, classical ML, deep learning, LLMs/GenAI,
  specialized domains, MLOps, responsible AI, and AI/ML security. Full catalog below.
- **Two enforced skill types.** Workflow skills require `## Workflow` with runnable code;
  reference skills require `## Key Concepts`. Both require `## Gotchas` (≥3) and
  `## References` (≥2).
- **Executed code.** The runnable Python blocks of the 26 code-bearing skills are executed
  by `tests/examples/` against pinned library versions, using small synthetic inputs and
  documented stubs.
- **Offline behavioural evals.** Every skill has a `plan.json`, a non-happy-path case, and
  regex hard assertions checked against pass/fail fixtures. No model output has been graded.
- **Native Claude Code plugin/marketplace distribution.** One all-in-one plugin plus one
  plugin per domain, generated from the same canonical `SKILL.md` files (zero duplicated
  content).
- **Portable, plugin-free installer.** `install.sh`/`uninstall.sh` for setups that read
  `~/.claude/skills/` directly; unless you pass `--force`, it keeps local edits and never
  touches folders it did not create.
- **Deterministic tooling.** Validator, lexical router plus labelled routing benchmark,
  dependency-graph builder, freshness and claims checkers, contract lock and packaging
  generator. The validators, the routing floor (`eval_router.py`), the contract lock and
  the generators' `--check` modes are CI gates; `router.py` and `quality_report.py` are not.
- **Honest about limits.** Every skill declares `risk_level` and `evidence_level`;
  `scripts/quality_report.py` reports what was checked and what was skipped, and never
  labels a skill "verified".

## Installation

### Claude Code (recommended)

Native plugin/marketplace distribution:

```
/plugin marketplace add NiravRVaghasiya/ml-ai-skills
/plugin install ml-ai-skills@ml-ai-skills
```

The packaging is tested with the `claude` CLI itself: `claude plugin validate --strict` and
a local `marketplace add` / `install` / `list` / `update` / reinstall cycle, run in an
isolated config directory. Whether Claude Code then loads and selects the skills at runtime
is **not** automatically tested. Details: [Claude Code compatibility](#claude-code-compatibility).

### Install a domain

Prefer a lighter footprint? Install just one domain instead of all 38 skills (same
marketplace, already added above):

```
/plugin install <domain>@ml-ai-skills
```

| Domain plugin | Skills | Covers |
|---|---|---|
| `foundations` | 4 | linear algebra/stats cheat-refs, Python/numpy/pandas idioms, problem framing |
| `classical-ml` | 6 | preprocessing, feature engineering, supervised/unsupervised learning, evaluation, tuning |
| `deep-learning` | 6 | backprop, CNNs, RNNs, attention/transformers, training loops, PyTorch patterns |
| `llm` | 7 | prompting, RAG, fine-tuning (LoRA/QLoRA), agents/tools, LLM/RAG/agent evaluation |
| `specialized` | 5 | computer vision, NLP tasks, time series, recommenders, reinforcement learning |
| `mlops` | 6 | experiment tracking, deployment, monitoring, data pipelines, CI/CD, optimization |
| `responsible-ai` | 3 | fairness/bias metrics, explainability (SHAP/LIME), privacy |
| `security` | 1 | ML/LLM/agent-specific security threats |

Each domain plugin ships only its own skills. See
[`.claude-plugin/marketplace.json`](.claude-plugin/marketplace.json) for the exact set.

### Portable skills (no plugin system)

For setups without native plugin support, or a plain-file checkout:

```bash
git clone https://github.com/NiravRVaghasiya/ml-ai-skills.git
cd ml-ai-skills
./install.sh            # → ~/.claude/skills/<slug>/SKILL.md  (personal scope)
```

| Command | What it does |
|---|---|
| `./install.sh` | Install all skills into `~/.claude/skills/` |
| `./install.sh --project [DIR]` | Install into `./.claude/skills/` (or `DIR/.claude/skills/`) |
| `./install.sh --target DIR` | Install into an arbitrary directory |
| `./install.sh --dry-run` | Preview only; writes nothing (not even the lock or manifest) |
| `git pull && ./install.sh` | **Update.** Refreshes unmodified skills in place; skills you edited are kept, with a warning |
| `./install.sh --force [--skill SLUG]` | Overwrite local edits, or a same-named folder the installer did not create (narrow it with `--skill`) |
| `./install.sh --prune` | Remove skills deleted upstream (unmodified ones only; `--prune --force` removes them anyway) |
| `./uninstall.sh [--skill NAME] [--project] [--dry-run] [--force]` | Remove what the installer added; edited skills are kept unless `--force` |

How it behaves:

- It copies whole `<slug>/SKILL.md` folders and records each one, with the SHA-256 of its
  `SKILL.md`, in a plain-text manifest (`.ml-ai-skills-manifest`).
- A changed hash means you edited the skill, so it is kept. Only `SKILL.md` is hashed.
- Each skill is swapped in with two renames (the old copy is moved aside, then the new copy
  is moved in), so the folder is briefly absent between them. A failure or interruption
  moves the old copy back (or, if something else took its place, keeps it in the lock
  directory for you to inspect), and a lock directory serialises runs.
- It refuses symlinks and junctions, never writes outside the target directory, and needs
  only `bash` and coreutils: no Python, no network.
- Exit codes: `0` success (protective skips are `WARN:` lines), `1` error, `2` usage error
  (nothing changed), `3` another run holds the lock.

`./bin/ml-ai-skills {install|update|uninstall|validate|list|help}` is a thin wrapper around
the above. The full contract, threat model and the platforms it was tested on are in
[`docs/INSTALLER.md`](docs/INSTALLER.md).

**Which path should I use?** The plugin/marketplace route is how Claude Code's own plugin
ecosystem expects skills to be distributed and updated (`/plugin update`). The installer is
a direct-filesystem alternative for scripted setups or tools that only read
`~/.claude/skills/`. It delivers the identical `SKILL.md` content through a different
mechanism; it is not a second implementation.

## Usage

**Installed (plugin or `install.sh`):** describe your task. Claude Code's own skill
discovery decides which `SKILL.md` to load. This repo does not test that step (see
[Limitations](#limitations)).

**Without installing**, use the repo as a plain reference library. `scripts/router.py` is a
deterministic, offline, lexical router (no embeddings, no model call). It ranks skills by
IDF-weighted word and phrase matches against each skill's name, capability tags, trigger
phrases and description, plus (at lower weight) its `## Overview` text and `inputs`/`outputs`
fields. It applies a domain anchor and negation scoping, and returns
nothing when no skill clears its confidence threshold:

```bash
python scripts/router.py "handle missing values and encode categorical features before training"
```

```
intent: workflow
confidence: 0.93 (threshold 5.0)
  [workflow ] data-preprocessing           score=69.7   capability match: missing-value-imputation, categorical-encoding; trigger phrase match: 'handle missing values'; ...
```

On its labelled benchmark (`evals/routing/`, 85 held-out test cases), its task-level
selection accuracy is 0.80. Its false-activation rate is 0.0 on the test split's 9 plainly
off-topic `no-skill` cases (cooking, CSS, git...); that metric covers only that category.
Off-topic requests built to mislead it are a separate category: 6 of the 10 adversarial
test cases that need no skill (homonyms such as "monitor my blood pressure" or "the Wheel
of Time series") still select one. Over all 19 test cases that need no skill, it selects something
for 6. Those figures describe this router on that benchmark only; the algorithm,
measurements and known failure modes are in [`docs/ROUTING.md`](docs/ROUTING.md).

Or reference a skill by path directly, e.g. "follow [`data-preprocessing/SKILL.md`](data-preprocessing/SKILL.md)".

[`CLAUDE.md`](CLAUDE.md) is the authoring manual an agent reads *before creating or editing*
a skill.

## Available skills

<!-- PROJECT-STATS:START -->
<!-- Generated by `python scripts/generate_readme_stats.py --write`. Do not hand-edit
     this block — your changes will be overwritten. Edit skill frontmatter, scripts/,
     tests/, docs/, or evals/ instead, then regenerate. -->

| Metric | Value |
|---|---|
| Skills | **38** (workflow (25) · reference (13)) |
| Domains | 8 — foundations (4) · classical-ml (6) · deep-learning (6) · llm (7) · specialized (5) · mlops (6) · responsible-ai (3) · security (1) |
| Level | beginner (5) · intermediate (30) · advanced (3) |
| Risk level | low (23) · medium (13) · high (2) |
| Evidence level | primary (3) · official-documentation (10) · established-practice (25) |
| Eval cases | 83 case file(s) covering 38/38 skills — graded offline against pass/fail fixtures by regex hard assertions; no model output graded (see `evals/SCHEMA.md`) |
| Unit tests | 666 in the default suite (`tests/test_*.py`, stdlib only) + 351 optional example tests (`tests/examples/`, need `requirements-examples.txt`) |
| Scripts / docs | 22 scripts in `scripts/`, 13 policy docs in `docs/` |
<!-- PROJECT-STATS:END -->

Every skill's frontmatter declares `domain`, `level`, `lifecycle`, `risk_level`,
`evidence_level`, `last_verified` (a review date, not a verification claim), `capabilities`,
`requires`/`conflicts`/`related`, and `inputs`/`outputs`. Full contract:
[`docs/SKILL-SPEC.md`](docs/SKILL-SPEC.md). Per-skill descriptions live in each `SKILL.md`
`## Overview` (and one-liners in [`INDEX.md`](INDEX.md)). They are not duplicated here, to
avoid drift.

<details>
<summary><b>Full catalog</b>: 38 skills by domain (⚙️ W = workflow · 📖 R = reference)</summary>

#### 1. Foundations
| Skill | Type | Level |
|---|---|---|
| [`ml-math-essentials`](ml-math-essentials/SKILL.md) | 📖 R | beginner |
| [`ml-problem-framing`](ml-problem-framing/SKILL.md) | ⚙️ W | intermediate |
| [`python-for-ml`](python-for-ml/SKILL.md) | 📖 R | beginner |
| [`statistics-for-ml`](statistics-for-ml/SKILL.md) | 📖 R | intermediate |

#### 2. Classical ML
| Skill | Type | Level |
|---|---|---|
| [`data-preprocessing`](data-preprocessing/SKILL.md) *(gold standard)* | ⚙️ W | intermediate |
| [`feature-engineering`](feature-engineering/SKILL.md) | ⚙️ W | intermediate |
| [`hyperparameter-tuning`](hyperparameter-tuning/SKILL.md) | ⚙️ W | advanced |
| [`model-evaluation`](model-evaluation/SKILL.md) | ⚙️ W | intermediate |
| [`supervised-learning`](supervised-learning/SKILL.md) | ⚙️ W | intermediate |
| [`unsupervised-learning`](unsupervised-learning/SKILL.md) | ⚙️ W | intermediate |

#### 3. Deep Learning
| Skill | Type | Level |
|---|---|---|
| [`attention-mechanisms`](attention-mechanisms/SKILL.md) *(gold standard)* | 📖 R | intermediate |
| [`cnn-vision`](cnn-vision/SKILL.md) | 📖 R | intermediate |
| [`neural-net-fundamentals`](neural-net-fundamentals/SKILL.md) | 📖 R | beginner |
| [`pytorch-patterns`](pytorch-patterns/SKILL.md) | ⚙️ W | intermediate |
| [`rnn-sequence`](rnn-sequence/SKILL.md) | 📖 R | intermediate |
| [`training-deep-models`](training-deep-models/SKILL.md) | ⚙️ W | intermediate |

#### 4. LLMs & Generative AI
| Skill | Type | Level |
|---|---|---|
| [`agents-and-tools`](agents-and-tools/SKILL.md) | 📖 R | intermediate |
| [`agent-evaluation`](agent-evaluation/SKILL.md) | ⚙️ W | intermediate |
| [`fine-tuning-llms`](fine-tuning-llms/SKILL.md) | ⚙️ W | advanced |
| [`llm-evaluation`](llm-evaluation/SKILL.md) | ⚙️ W | intermediate |
| [`prompt-engineering`](prompt-engineering/SKILL.md) | 📖 R | beginner |
| [`rag-evaluation`](rag-evaluation/SKILL.md) | ⚙️ W | intermediate |
| [`rag-pipeline`](rag-pipeline/SKILL.md) | ⚙️ W | intermediate |

#### 5. Specialized Domains
| Skill | Type | Level |
|---|---|---|
| [`computer-vision`](computer-vision/SKILL.md) | ⚙️ W | intermediate |
| [`nlp-tasks`](nlp-tasks/SKILL.md) | ⚙️ W | intermediate |
| [`recommender-systems`](recommender-systems/SKILL.md) | ⚙️ W | intermediate |
| [`reinforcement-learning`](reinforcement-learning/SKILL.md) | 📖 R | intermediate |
| [`time-series`](time-series/SKILL.md) | ⚙️ W | intermediate |

#### 6. MLOps & Production
| Skill | Type | Level |
|---|---|---|
| [`ci-cd-for-ml`](ci-cd-for-ml/SKILL.md) | ⚙️ W | intermediate |
| [`data-pipelines`](data-pipelines/SKILL.md) | ⚙️ W | intermediate |
| [`experiment-tracking`](experiment-tracking/SKILL.md) | ⚙️ W | beginner |
| [`ml-monitoring`](ml-monitoring/SKILL.md) | ⚙️ W | intermediate |
| [`model-deployment`](model-deployment/SKILL.md) | ⚙️ W | intermediate |
| [`model-optimization`](model-optimization/SKILL.md) | ⚙️ W | advanced |

#### 7. Responsible AI
| Skill | Type | Level |
|---|---|---|
| [`ai-ethics-fairness`](ai-ethics-fairness/SKILL.md) | 📖 R | intermediate |
| [`data-privacy`](data-privacy/SKILL.md) | 📖 R | intermediate |
| [`explainability`](explainability/SKILL.md) | ⚙️ W | intermediate |

#### 8. AI/ML Security
| Skill | Type | Level |
|---|---|---|
| [`ai-ml-security`](ai-ml-security/SKILL.md) | 📖 R | intermediate |

</details>

## Evidence: what has and has not been checked

Each kind of evidence below is separate. None of them alone, nor all of them together,
shows that a skill's advice is correct for your problem.

| Evidence | Scope | How it is checked |
|---|---|---|
| **Structurally valid** | all 38 skills | `scripts/validate_skills.py`: frontmatter contract, sections, links, relationships |
| **Code executed** | the 26 skills with runnable Python | `tests/examples/` runs every runnable block on synthetic inputs with stubbed services. Last observed run: 2026-10-28, 327 tests OK, Windows, Python 3.13 |
| **Offline eval coverage** | all 38 skills | `scripts/validate_evals.py`: each case's pass/fail fixtures grade as intended under regex hard assertions. **No model output has been graded; with-skill uplift is unmeasured** |
| **Checkable claims** | 24 records in `docs/claims.json` | each record says how it was checked; 22 are `runtime-test` records naming the test that backs them and the version it ran against, 2 are `unverified` |
| **Human review** | none recorded | Phase 1/2 audits were agent reviews ([`docs/SKILL-AUDIT.md`](docs/SKILL-AUDIT.md)) |

`evidence_level` in each skill's frontmatter is the dominant *source* tier of its claims
(primary paper, official docs, established practice), not a verification status. Run
`python scripts/quality_report.py` for the current per-skill picture.

## Tooling

The scripts are stdlib-only (no `pip install`). [CI](.github/workflows/ci.yml) runs the
validation, eval, routing, generated-artifact (including the README stats block) and test
gates on pushes to `main` and on pull requests. The live-link check is manual
(`workflow_dispatch`), and `quality_report.py` is not run in CI. CI itself has not yet run
on GitHub (see [`docs/KNOWN-ISSUES.md`](docs/KNOWN-ISSUES.md)).

| Command | What it does |
|---|---|
| `python scripts/validate_skills.py --warnings` | Main gate: frontmatter, sections, relationships, cycles, INDEX sync (`--json`, `--strict`) |
| `python scripts/validate_evals.py --strict --coverage` | Eval cases, hard-assertion lint, fixture self-tests, per-skill coverage (`--require-full-coverage` in CI) |
| `python scripts/run_evals.py --out eval-report.json` | Offline eval report; `--run-executable --python PY` runs executable cases; `--compare A B` finds regressions; model mode is opt-in and has never been run |
| `python scripts/eval_router.py --split test --json` | Routing benchmark metrics; CI compares the test split with `evals/routing/baseline.json` |
| `python scripts/router.py "your task"` | Preview routing (`--json` adds confidence and candidates) |
| `python scripts/check_contract.py --check` | Public contract (slugs, plugins, enums, keys, VERSION) matches its lock; `--write` re-locks and refuses breaking changes without a major bump |
| `python scripts/generate_schema.py --check` | Generated JSON Schema matches `schema/allowed_values.py` |
| `python scripts/check_claims.py` | Claims registry well-formed; runtime-test evidence names real tests |
| `python scripts/check_freshness.py` | Offline known-removed-API patterns and review clock (no network) |
| `python scripts/check_generated.py` | Every generated file matches its generator (INDEX, README stats, plugins, graph, schema) |
| `python scripts/generate_plugins.py --check` | `plugins/` and `marketplace.json` drift check |
| `python scripts/quality_report.py --out quality-report.json` | Machine-readable quality report; `--run-tests` adds the unit suite, `--run-examples PY` the example suite |
| `python -m unittest discover -s tests -p "test_*.py"` | Everything under `tests/`: unit, installer (~9 min) and plugin/marketplace tests, plus `tests/examples/`, whose tests skip when their dependencies are missing. The claude-CLI tests skip without `claude` on PATH. CI runs these groups as separate jobs |
| `python -m unittest discover -s tests/examples -t tests -p "test_*.py"` | Example suite (needs `requirements-examples.txt`; see [`docs/EXAMPLE-TESTS.md`](docs/EXAMPLE-TESTS.md)) |
| `claude plugin validate . --strict` | Native schema validation (needs the `claude` CLI) |
| `./install.sh --dry-run` | Installer smoke test |

| Doc | Covers |
|---|---|
| [`docs/ROUTING.md`](docs/ROUTING.md) | Router algorithm, benchmark results before/after, ablations, measured failure modes |
| [`evals/SCHEMA.md`](evals/SCHEMA.md) | Eval case format, hard assertions, fixtures, `plan.json`, report format, what is and is not measured |
| [`docs/EXAMPLE-TESTS.md`](docs/EXAMPLE-TESTS.md) | How SKILL.md code is executed, stubs, dependencies, coverage |
| [`docs/EVIDENCE.md`](docs/EVIDENCE.md), [`docs/FRESHNESS.md`](docs/FRESHNESS.md) | Evidence tiers, claims registry, freshness policy and verification kinds |
| [`docs/QUALITY-GATES.md`](docs/QUALITY-GATES.md) | Which gates CI enforces and which need review |
| [`docs/COMPATIBILITY.md`](docs/COMPATIBILITY.md) | Supported environments and the test behind each claim |
| [`docs/RELEASE.md`](docs/RELEASE.md) | Versioning, release steps, verifying a release, deprecation policy |
| [`docs/INSTALLER.md`](docs/INSTALLER.md) | Installer contract, threat model, exit codes, platforms |
| [`docs/SKILL-GRAPH.md`](docs/SKILL-GRAPH.md) | Generated `requires`/`related`/`conflicts` graph |
| [`docs/KNOWN-ISSUES.md`](docs/KNOWN-ISSUES.md) | Every open issue, unverified claim and untested platform, by severity |

## Contributing

See [`CONTRIBUTING.md`](CONTRIBUTING.md) for the end-to-end steps (template, validator,
example tests, eval cases, routing cases, claims, regenerated artifacts, contract lock,
changelog) and the review checklist. [`CLAUDE.md`](CLAUDE.md) is the authoring manual.
Preserve the two gold-standard examples (`data-preprocessing/`, `attention-mechanisms/`).
Security issues: see [`SECURITY.md`](SECURITY.md).

## Project structure

```
ml-ai-skills/
├── README.md            ← you are here
├── INDEX.md             ← one-line-per-skill checklist (generated by scripts/generate_index.py)
├── CLAUDE.md            ← authoring/operating manual for skills
├── CONTRIBUTING.md, SECURITY.md, CHANGELOG.md
├── VERSION              ← single version stamp for every plugin.json/marketplace.json
├── .claude-plugin/marketplace.json  ← generated: native Claude Code marketplace manifest
├── plugins/             ← generated: one all-in-one + one per-domain plugin
│   └── <plugin-name>/.claude-plugin/plugin.json + skills/<slug>/SKILL.md (byte-copy of canonical)
├── install.sh / uninstall.sh        ← portable, non-plugin installation path
├── bin/ml-ai-skills     ← thin CLI wrapper
├── _TEMPLATE/SKILL.md   ← clone for every new skill
├── docs/                ← spec, evidence, freshness, routing, quality gates, compatibility, release, known issues, ...
├── schema/              ← allowed_values.py (every enum), generated JSON Schema, contract lock, freshness policy
├── scripts/             ← validator, router, eval/benchmark runners, freshness/claims, generators, quality report
├── tests/               ← stdlib unit/installer/plugin tests; tests/examples/ executes SKILL.md code
├── evals/               ← per-skill cases, fixtures and plan.json; evals/routing/ benchmark; SCHEMA.md
└── <skill-slug>/SKILL.md            ← canonical content: edit here, never under plugins/
```

## Claude Code compatibility

What is tested, using the real `claude` CLI against an isolated `CLAUDE_CONFIG_DIR` (never
the tester's own `~/.claude`), in
[`tests/test_generate_plugins.py`](tests/test_generate_plugins.py):

- `claude plugin validate . --strict` and `claude plugin validate plugins/<name> --strict`
  exit 0 for the marketplace and every generated plugin.
- `plugin marketplace add`, `install ml-ai-skills@ml-ai-skills`, a domain-plugin install,
  `list` (both plugins listed), `update` and a reinstall all exit 0.

These passed on 2026-10-28 with a local Claude Code build (`claude 2.1.310.1372`, Windows 11).
CI pins the public `@anthropic-ai/claude-code` CLI (`CLAUDE_CODE_VERSION` in `ci.yml`), but
that job has not run yet. The tests skip when `claude` is not on PATH.

`install.sh` is tested to place `<slug>/SKILL.md` folders under `~/.claude/skills/` or
`<project>/.claude/skills/`, the documented locations for loose skills. The tests check that
each installed file parses and its `name` matches its folder.

**Not tested:** that Claude Code loads these skills at runtime, which skill it selects for a
task, or whether its output improves. CLI builds have disagreed on some flags (for example
`plugin validate --json`, `plugin install -y`), so the tests assert return codes only. No
other tool (Cursor, Copilot, Codex, Kiro, …) is targeted or tested. claude.ai uploads and
the Skills API are expected to reject the extra frontmatter keys. Full matrix:
[`docs/COMPATIBILITY.md`](docs/COMPATIBILITY.md).

## Limitations

- **No model-graded evals.** Eval cases are graded offline against fixtures by regex hard
  assertions. Real model output has never been graded, and independent probes showed the
  patterns miss many unanticipated phrasings.
- **Executed examples use synthetic data and stubs.** They show the code runs against the
  pinned versions, not that the advice is optimal. GPU paths, pretrained weights and real
  external services are not exercised.
- **Router is lexical, not semantic.** It has no embeddings. It misses paraphrases with no
  shared vocabulary and vocabulary that appears in no skill, and its benchmark is
  single-labeller with an 85-case test split (`docs/ROUTING.md`).
- **Runtime behaviour in Claude Code is untested**, beyond plugin validation and the
  install/update cycle.
- **CI has not run on GitHub yet.** Linux and macOS results, the public-CLI plugin job, and
  the release workflow are configured but not observed.
- **No live package/API verification by default.** `last_verified` is a review date; most
  `version_constraints` lower bounds are estimates. Checked claims are listed in
  `docs/claims.json`.
- **Evidence classification is skill-level, not sentence-level**, and no skill has a
  recorded human review.

Full list, by severity: [`docs/KNOWN-ISSUES.md`](docs/KNOWN-ISSUES.md). See also
[`docs/SKILL-SPEC.md`](docs/SKILL-SPEC.md) §"Known limitations".

## License

[MIT](LICENSE) — © 2026 Nirav Vaghasiya. Free to use, modify, and distribute (including commercially),
provided the copyright notice and license text are retained.
