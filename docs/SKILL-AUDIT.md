# Skill Audit (Phase 15)

Compiled from the 7 domain-batch migration/audit passes run on 2026-09-21, plus
3 net-new skills authored directly to the new spec in the same pass. Every skill
listed here: (a) has the full frontmatter contract in docs/SKILL-SPEC.md, (b)
was read end-to-end for the evidence-discipline test in docs/EVIDENCE.md, (c)
passes `scripts/validate_skills.py` with zero errors. "Changes made" lists only
what was actually rewritten — most skills needed frontmatter only, and that is
recorded as such rather than padded with invented findings.

Legend: **risk** = `risk_level`, **evid** = `evidence_level`.

## Foundations

| Skill | risk | evid | Findings / changes made |
|---|---|---|---|
| `ml-math-essentials` | low | established-practice | No evidence issues found; content already conditionally phrased. |
| `statistics-for-ml` | low | established-practice | No evidence issues found; CLT sample-size and multiple-comparisons claims already correctly hedged/derived. |
| `python-for-ml` | low | official-documentation | No evidence issues found. Added one honestly-labeled `version_constraints` entry (`np.random.default_rng`, estimated since numpy 1.17). |
| `ml-problem-framing` | low | established-practice | **Rewritten:** a fabricated precise statistic ("a one-line heuristic would have gotten 90% of the value") was replaced with language naming the actual dependency ("the actual gap depends on the problem, so measure it") instead of asserting an invented number as fact. |

## Classical ML

| Skill | risk | evid | Findings / changes made |
|---|---|---|---|
| `data-preprocessing` *(gold standard)* | low | established-practice | Frontmatter only, prose preserved. The "~50 unique values" hard threshold had already been softened to an explicit heuristic in an earlier pass this session; left as-is. Added `version_constraints` for ColumnTransformer/OneHotEncoder/SimpleImputer (0.20+, honestly labeled unverified). |
| `feature-engineering` | medium | established-practice | **Rewritten:** "one-hot blows up past ~50 categories" hard threshold → explicit heuristic naming the real dependent variables (row count, feature budget, downstream model). `risk_level: medium` because this skill's own stated purpose is "where leakage most often sneaks in unnoticed" — a mistake here silently inflates reported performance. |
| `supervised-learning` | low | established-practice | No evidence issues found; baseline-first/CV discipline are genuine methodology invariants, not disguised heuristics. |
| `unsupervised-learning` | low | established-practice | No evidence issues found; DBSCAN `eps` and dimensionality remarks were already phrased as "rough heuristic." |
| `model-evaluation` | medium | established-practice | No evidence issues found (already-correct invariants: accuracy-on-imbalance, temporal leakage, calibration ≠ AUC). `risk_level: medium` — this skill's output is the trusted number that gates shipping decisions. |
| `hyperparameter-tuning` | medium | established-practice | No evidence issues found; nested-CV-vs-biased-`best_score_` framing already correct. `risk_level: medium` for the same "silently inflated reported performance" reason as model-evaluation. |

## Deep Learning

| Skill | risk | evid | Findings / changes made |
|---|---|---|---|
| `neural-net-fundamentals` | low | established-practice | No evidence issues found. |
| `cnn-vision` | low | **primary** | Classified `primary` (not defaulted to established-practice) because its central claims (skip connections solving the degradation problem, VGG's depth/diminishing-returns finding) are the specific arguments of the cited ResNet/VGG papers, not generic textbook material. |
| `rnn-sequence` | low | established-practice | No evidence issues found; vanishing-gradient and "reduces, doesn't eliminate" framing already correct. Deliberately classified differently from `cnn-vision`/`attention-mechanisms` rather than defaulting all three to the same tier. |
| `attention-mechanisms` *(gold standard)* | low | primary | Frontmatter only, prose untouched — already tightly sourced to Vaswani et al. 2017. |
| `training-deep-models` | low | official-documentation | No evidence issues found ("always split param groups" / "usually needs no scaler" already correctly scoped). Added one `version_constraints` entry (`torch.cuda.amp` since PyTorch 1.6+). |
| `pytorch-patterns` | low | official-documentation | No evidence issues found; hook-cleanup and state_dict-vs-pickle claims are genuine resource/serialization invariants, not heuristics. No `version_constraints` added — no milestone the reviewer was confident enough to state honestly. |

## LLMs & Generative AI

| Skill | risk | evid | Findings / changes made |
|---|---|---|---|
| `prompt-engineering` | low | established-practice | Temperature/determinism language already softened in an earlier pass (see docs/EVIDENCE.md example 2); left as-is. No further issues found. |
| `rag-pipeline` | medium | established-practice | Added `related: [rag-evaluation, ai-ml-security]` and a new Gotcha pointing to `ai-ml-security` for the "retrieved documents are untrusted content" threat model instead of re-deriving it inline. `risk_level: medium` — a carelessly built pipeline creates real injection/staleness surface. |
| `fine-tuning-llms` | medium | established-practice | No evidence issues found — LR/overfitting claims already hedged. Classified `established-practice` rather than `primary` because the skill's actual claims are operational advice layered on the LoRA/QLoRA papers, not restatements of the papers themselves. |
| `agents-and-tools` | medium | established-practice | Added `related: [agent-evaluation, ai-ml-security]`. **Rewritten:** an absolute superlative ("the description is *the* single biggest lever over tool selection") softened to "one of the biggest levers," naming that tool naming/scope overlap also matter. Cross-referenced `ai-ml-security` from the "unvalidated tool output"/"no sandboxing" Gotchas and `agent-evaluation` from the sandboxing Gotcha, rather than duplicating that content inline. |
| `llm-evaluation` | low | established-practice | Temperature=0/determinism Gotcha and code comment already fixed in an earlier pass (see docs/EVIDENCE.md example 3); left as-is. Added `related: [agent-evaluation, rag-evaluation]` and one Overview sentence distinguishing this skill's scope (single-output text quality) from those two siblings'. |

## Specialized Domains

| Skill | risk | evid | Findings / changes made |
|---|---|---|---|
| `nlp-tasks` | low | official-documentation | No evidence issues found; existing hedges already name the actual dependency (e.g. per-model token limits). |
| `computer-vision` | low | official-documentation | No evidence issues found; NMS-threshold/pixel-accuracy gotchas already conditional. Added 2 `version_constraints` entries (torchvision `weights=` enum API, `_v2` detection variants — dated to 0.13, confident milestone). |
| `time-series` | low | established-practice | **Rewritten (2 claims):** (1) the ADF `p<0.05 → d=1` rule was presented as settled fact — added that the cutoff is "a convention, not a proof," that KPSS can disagree, and that `auto_arima` should have final say. (2) "under ~2 years of data is mostly noise" (a fixed calendar-length threshold) → reworded to state the real dependency is *cycle count* in the training data, not calendar length, keeping "~2 years" only as a rough guide. |
| `recommender-systems` | low | established-practice | **Rewritten:** an ALS confidence-scaling constant (`alpha=15`) presented with no caveat → labeled a dataset-dependent hyperparameter to tune against the held-out ranking metric, not a universal constant. |
| `reinforcement-learning` | low | established-practice | No evidence issues found; discount-factor and overestimation-bias Gotchas already explain the actual mechanism rather than asserting a bare threshold. |

## MLOps & Production

| Skill | risk | evid | Findings / changes made |
|---|---|---|---|
| `experiment-tracking` | low | official-documentation | Folded a version-sensitivity warning (MLflow Model Registry stage transitions being superseded by aliases/tags ~2.9+) into the existing "registry stage labels are just metadata" Gotcha. |
| `model-deployment` | **high** | official-documentation | **Rewritten:** the claim that a wrong feature *count* produces "a silent wrong prediction instead of a 400" was factually imprecise — split into the two real failure modes: wrong *order* (same count) is genuinely silent; wrong *count* usually raises an unhandled 500, not a validated 400. Neither is the clean 400 you'd want, so the fix (explicit shape/dtype/identity validation) is unchanged, but the diagnosis is now accurate. `risk_level: high` — a deployment mistake can cause a production outage. |
| `ml-monitoring` | medium | established-practice | **Most significant finding of the audit.** The skill's description promised "prediction drift" as a trigger phrase, but the Workflow only ever computed *data drift* and *ground-truth performance decay* — prediction drift and concept drift were never distinguished, and the wording implicitly treated data drift and performance decay as one continuum. Fixed by: rewriting the Overview to define all four (data/prediction/concept drift, plain performance degradation) with distinct causes; adding a new Workflow step that computes prediction drift on the output column separately from data drift on the input columns; rewriting the ground-truth-performance step to explain it's the only signal that surfaces concept drift, and that decay with *no* drift signal at all should point at a pipeline bug, not a retrain; adding a leading Gotcha naming all four explicitly. This directly satisfies the mission's Phase 12 requirement to distinguish these four concepts. |
| `data-pipelines` | medium | official-documentation | No evidence issues found; all 5 existing Gotchas already phrased conditionally/causally. Added one `version_constraints` entry (Feast `FeatureView(schema=...)` vs. older `features=[Feature(...)]`). |
| `ci-cd-for-ml` | medium | established-practice | No evidence issues found; all 5 Gotchas already hedged. |
| `model-optimization` | medium | official-documentation | No evidence issues found — already the best-hedged skill in this batch. Added one `version_constraints` entry (`torch.quantization` reorganized under `torch.ao.quantization` around PyTorch 1.13/2.0). |

## Responsible AI

| Skill | risk | evid | Findings / changes made |
|---|---|---|---|
| `ai-ethics-fairness` | medium | primary | Added `related: [ai-ml-security]`. No evidence issues found — the "80% rule" disparate-impact threshold was already correctly scoped as "(US EEOC-derived)." |
| `explainability` | low | official-documentation | No evidence issues found — the causal-claim warning on feature importance was already conditional. Added 2 `version_constraints` entries (`permutation_importance` since sklearn 0.22; SHAP `Explanation`-object API from the 0.3x series). |
| `data-privacy` | medium | established-practice | Added `related: [ai-ml-security]`. **Rewritten:** "anonymization aims to be irreversible... no reasonable means exist to re-link records" was stated as a universal technical/legal definition — it is specifically the EU/GDPR "reasonable means" legal test (Recital 26). Rewrote to name that jurisdictional scope explicitly and note other jurisdictions (e.g. US HIPAA Safe Harbor/Expert Determination) define the line differently. |

## AI/ML Security & Agent/RAG Evaluation *(net-new, authored directly to spec)*

| Skill | risk | evid | Notes |
|---|---|---|---|
| `ai-ml-security` | high | established-practice | New — fills the Phase 13 gap. Explicitly distinguishes "security control" from "responsible-AI policy" (the line the Responsible AI skills sit on the other side of). |
| `agent-evaluation` | low | established-practice | New — fills the Phase 8 gap (agent-trajectory/tool-use evaluation, distinct from generic LLM eval). `requires: [agents-and-tools]`. |
| `rag-evaluation` | low | established-practice | New — fills the Phase 9 gap (retrieval metrics, faithfulness, RAG-specific failure taxonomy, distinct from building the pipeline). `requires: [rag-pipeline]`. |

## Cross-cutting observations

- **No fabricated evidence was found or introduced.** Every reviewer either cited a real source already in the skill's References, or explicitly declined to add a `version_constraints` entry when not confident, per the honesty rule in docs/SKILL-SPEC.md. Several reports explicitly note *which* entries were omitted and why (e.g. TRL/bitsandbytes version milestones, exact Optuna/sklearn release numbers) — omission, not invention, was the consistent choice under uncertainty.
- **`evidence_level` was not defaulted uniformly.** Sibling skills in the same domain received different tiers when their actual content warranted it (e.g. `cnn-vision` → `primary` vs. `rnn-sequence` → `established-practice` within Deep Learning; `ai-ethics-fairness` → `primary` vs. `explainability` → `official-documentation` within Responsible AI).
- **The one deliberately-designed cross-cutting check (drift terminology in `ml-monitoring`) found a real, substantive gap**, not a false positive — see above. This is the strongest evidence that the audit step did real work rather than rubber-stamping frontmatter.
- **A validator bug, not a content bug, was the largest source of noise.** `scripts/validate_skills.py --warnings` initially reported 138 "python code block fails ast.parse" warnings across nearly every workflow skill; investigation traced this to `skills_lib.py`'s code-block extraction not dedenting Markdown-list-indented fences before parsing (a tooling defect, fixed in the same session — see git history), not to any actual broken code in the skills. Post-fix: **0 errors, 0 warnings** across all 38 skills.

## Remaining limitations (stated explicitly)

1. Evidence classification is skill-level, not sentence-level — see docs/SKILL-SPEC.md "Known limitations" item 3.
2. No live package/API verification occurred — every `version_constraints` entry and every "no evidence issues found" line above reflects reviewer knowledge at authoring time, not a live check. See docs/FRESHNESS.md.
3. The router's vocabulary-collision failure mode (docs/ROUTING.md) is reduced but not eliminated by the new `capabilities` tags — `model-evaluation` still fails to surface for a query that explicitly asks to "evaluate" a model, in the one worked example re-tested after this audit.
4. This audit did not attempt Phase 14's full agent-engineering skill decomposition (tool design, state management, memory, planning, retries, observability as separate skills) — `agents-and-tools` (existing) and `agent-evaluation` (new) cover agent loops and their evaluation, but planning/memory/observability as dedicated skills were judged out of scope for this pass to avoid adding skills without a demonstrated need beyond the mission brief's checklist. Recorded as future work, not silently dropped.
