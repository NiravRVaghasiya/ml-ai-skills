# Skill audit

This file records what each audit actually did to each skill, so a reader can tell
"checked and fixed" from "looked at" and from "not checked". **Every audit here was done by
agents (implementer, independent reviewer, then a fix pass), not by a human reviewer**; no
skill has a recorded human review. Defects were reproduced before they were fixed, and the
fixed code is pinned by regression tests in `tests/examples/` where the skill has runnable
code.

- **Phase 1 (2026-10-27):**
  - *Code-bearing skills:* each skill's Python blocks were executed in a pinned environment
    (`requirements-examples.txt`) through `tests/examples/harness.py`. Defects were fixed in
    the skill and pinned by tests.
  - *Reference skills:* the content was checked against the cited primary sources (papers,
    vendor docs; fetched where reachable) and corrected.
  - Each workstream was then reviewed independently and fixed again.
- **Phase 2 (2026-10-27/28):**
  - Eval cases were written or strengthened for every skill and stress-tested with answers
    from reviewers who had not seen the patterns.
  - The router was rewritten and benchmarked.
  - Content errors that the eval work exposed were reported to the skill owners.

The last column gives the eval cases on 2026-10-28 (`python scripts/validate_evals.py
--coverage`), with the number of `kind: executable` cases in brackets. "Open" items are also
listed in `docs/KNOWN-ISSUES.md`. Skill-content fixes made after 2026-10-28 are not reflected
here.

## Foundations

| Skill | Phase 1 method | Main findings fixed | Open | Evals |
|---|---|---|---|---|
| `ml-math-essentials` (R) | sources + numeric checks (√d_k variance, Adam vs AdamW, `eigh` order) | missing caveats on PCA centering, L1 solvers, Adam vs AdamW; a shape-mismatch overclaim; an unanswered trigger phrase; missing `related` | none | 2 (0) |
| `ml-problem-framing` (W) | code executed | leakage check was an `assert` (removed by `python -O`) on a column valid by construction; no label was built; NaT as-of timestamps passed; degenerate label crashed later | example as-of timestamps are hand-written, so the shipped audit cannot fail | 2 (1) |
| `python-for-ml` (R) | code executed (numpy 2.4.4, pandas 2.2.3) | wrong claims on mixed dtypes, the `.base` view test, the Windows int default, `int32.sum` overflow, Copy-on-Write; float-equality constant-row guard | pandas 3 behaviour comes from release notes only (not executed) | 2 (0) |
| `statistics-for-ml` (R) | sources + numeric checks (Mann-Whitney vs Welch) | CLT stated without the finite-variance condition; Mann-Whitney presented as a drop-in t-test; missing p-value definition, Bonferroni/BH distinction and CV-fold t-test pitfall | none | 2 (0) |

## Classical ML

| Skill | Phase 1 method | Main findings fixed | Open | Evals |
|---|---|---|---|---|
| `data-preprocessing` (W, gold) | code executed | `X, y` never built (NameError); `None` categoricals encoded as a category; bool/`string` columns silently dropped by `remainder="drop"`; first fix downcast object columns; no warning that `joblib.load` runs code | `stratify=y` raises for regression targets (left to keep the gold diff minimal) | 2 (1) |
| `feature-engineering` (W) | code executed | undefined `X_train_numeric`; domain features added to train only; wrong `TargetEncoder` target type and deprecated `random_state`; CV validated a different procedure; unscaled RFECV ranking; NaN ratio never imputed | needs scikit-learn ≥ 1.9 for the seeded `TargetEncoder`; older path not executed | 1 (1) |
| `supervised-learning` (W) | code executed | shipped a hard-coded model regardless of the comparison; NaN CV scores ranked arbitrarily; deprecated `SVC(probability=True)`; false claim that an int `cv` reshuffles | none | 2 (1) |
| `unsupervised-learning` (W) | code executed | k loop crashed when `n_samples ≤ 9`; duplicate rows collapsed clusters; fixed perplexity crashed for `n ≤ 30`; `eps=0` and missing import | none | 2 (1) |
| `model-evaluation` (W) | code executed | calibration chosen on the test set; `learning_curve` NaN on sorted rows; isotonic hard-coded regardless of calibration size; APIs newer than the stated bound | lower bounds from GitHub tags, not executed | 3 (1) |
| `hyperparameter-tuning` (W) | code executed | MedianPruner could never prune; `KFold` on imbalanced ROC-AUC gave NaN folds; missing import; wrong multiclass `roc_auc` comment | test file slow (~60 s) | 2 (2) |

## Deep Learning

| Skill | Phase 1 method | Main findings fixed | Open | Evals |
|---|---|---|---|---|
| `attention-mechanisms` (R, gold) | sources (Vaswani, FlashAttention, NoPE, rank-collapse papers) + numeric check | called self-attention permutation-*invariant* (it is equivariant); "always paired with positional embeddings" overclaim; implied FlashAttention changes complexity; a reviewer-caught miscitation in the first rewrite was reverted | multi-head bullet omits `W^O` (incomplete, not wrong; gold standard left as is) | 3 (0) |
| `cnn-vision` (R) | sources (ResNet, VGG, ViT, ConvNeXt) + torch checks | plain-net degradation misattributed to vanishing gradients; `'same'` padding claimed to preserve size at any stride | TF/Keras `'same'` padding claim from docs only | 3 (0) |
| `neural-net-fundamentals` (R) | sources + torch checks | RMSprop missing the square root; Adam called "robust to LR"; several universal laws (zero init, Xavier for sigmoid, batch size, ReLU) | chain-rule shorthand and the "Xavier" naming left unchanged | 2 (0) |
| `pytorch-patterns` (W) | code executed (torch 2.13 CPU) | `device` undefined; non-atomic save, which then left a `.tmp` file on failure; hook leaked on exception; resume repeated the finished epoch | multi-worker loading not exercised (`num_workers` set to 0 in the test) | 2 (1) |
| `rnn-sequence` (R) | sources (Greff, Gers, Khandelwal) + torch checks | `retain_graph` failure described wrongly; lengths-1 gather presented as equivalent to `h_n` for bidirectional RNNs; GRU "gate blocks"; Greff miscited as a GRU benchmark. Phase 2 eval work found the bidirectional gather is valid only on packed output, reported to the owner | LSTM 1997 PDF host has an expired TLS certificate | 2 (0) |
| `training-deep-models` (W) | code executed (CPU) | deprecated `torch.cuda.amp.GradScaler` with CUDA hard-coded; NaN validation loss broke checkpointing; cosine LR climbed past `total_steps`. Phase 2 eval work found the scheduler-granularity Gotcha described the wrong direction, reported to the owner | CUDA AMP path not run | 2 (1) |

## LLMs & Generative AI

| Skill | Phase 1 method | Main findings fixed | Open | Evals |
|---|---|---|---|---|
| `agents-and-tools` (R) | sources (Anthropic tool-use docs, MCP spec) | strict tool mode presented as making argument validation unnecessary; unsupported superlative; hard 30-tool rule; MCP missing | "Building Effective Agents" URL not live-checked (DNS) | 3 (0) |
| `agent-evaluation` (W) | code executed | `detect_loops` crashed on dict/list args; an errored prerequisite counted as satisfied; misleading ordering message | `detect_loops` counts any identical call, not only blind retries (documented) | 2 (2) |
| `fine-tuning-llms` (W) | code executed (tiny random Llama; TRL 1.16, PEFT 0.21) | `SFTConfig(max_seq_length=)` rejected; merge wrapped an already-wrapped PeftModel into the 4-bit base; `input` field dropped and no eval split; second base copy loaded while training objects were alive | GPU memory behaviour not measured | 1 (1) |
| `llm-evaluation` (W) | code executed (anthropic 2.3.0 over a mock transport) | `temperature=0` rejected by the SDK; `content[0].text` broke on thinking blocks; unvalidated judge replies; hard-coded NLI label; `evaluate.load` needed the Hub; NaN in JSON; refusals scored | no live API call; Hub model labels unverified | 2 (1) |
| `prompt-engineering` (R) | sources (Anthropic docs, CoT paper) | overclaimed that tool-calling/`response_format` constrains decoding; universal chain-of-thought claim | OpenAI `strict: true` naming from model knowledge | 3 (0) |
| `rag-evaluation` (W) | code executed | nDCG > 1 with duplicate ids; negative `k` accepted; `KeyError` on uncited claims; unlabeled queries deflated MRR | faithfulness NLI call lives in `llm-evaluation` and is not run here | 3 (1) |
| `rag-pipeline` (W) | code executed (real faiss-cpu 1.15.1, sentence-transformers 6.2.0, anthropic 2.3.0 over a mock) | FAISS `-1` padding indexed the store; no tenant/ACL check, then an ACL that failed open on null; persisted index never checked; unsalted query hashes; timeout/`max_tokens` mismatch | `save_store` not atomic; character-based context budget; no live API call | 5 (5) |

## Specialized Domains

| Skill | Phase 1 method | Main findings fixed | Open | Evals |
|---|---|---|---|---|
| `computer-vision` (W) | code executed (torchvision 0.28 CPU, random weights) | double ImageNet normalisation before Faster R-CNN; COCO xywh fed as xyxy; undefined names; uint8 masks rejected by the loss; no segmentation loader; TTA merge misdescribed | pretrained weights and mAP quality not measured; slow test file | 2 (1) |
| `nlp-tasks` (W) | code executed (Transformers 5.21, tiny models) | `evaluation_strategy` rejected; no tokenizer or collator; `summarization` pipeline removed; character chunking; unshuffled eval subset; wrong spaCy label set | Hub checkpoints and IMDB sort order unverified (Hub unreachable) | 2 (1) |
| `recommender-systems` (W) | code executed (implicit 0.7.3 CPU) | undefined names; unbounded ALS scores blended with cosine; no path for new users; NaN ids/text crashed; duplicate item ids; float ids | random per-interaction split kept for brevity (Gotcha points to a time split) | 2 (1) |
| `reinforcement-learning` (R) | sources (DQN 2013, Watkins & Dayan, Pan et al.) | DQN credited with experience replay; Q-learning conditions stated as necessary; a stale repo-state remark in the description | the target network appearing first in the 2015 paper is from knowledge | 2 (0) |
| `time-series` (W) | code executed (statsmodels 0.14.6, prophet 1.5, pmdarima 2.1) | partial interpolation then all-NaN STL; MAPE exploded on zero sales; last fold dropped; yearly seasonality forced on short history | none | 2 (1) |

## MLOps & Production

| Skill | Phase 1 method | Main findings fixed | Open | Evals |
|---|---|---|---|---|
| `ci-cd-for-ml` (W) | code executed | leakage test was a tautology; release job had no Python setup or install step, no permissions block, and used the deprecated MLflow `--stage` | the skill's GitHub Actions workflows were parsed, not run | 2 (1) |
| `data-pipelines` (W) | code executed (SQLite, pandas, pandera; Airflow stubbed) | raw `%s` SQL failed on every `to_sql` connectable and was not atomic; Airflow 3 `schedule_interval`; 30-day window was one day, then off by the trigger day; Feast source never written | real Airflow and Feast never run | 2 (1) |
| `experiment-tracking` (W) | code executed (mlflow 3.16.1) | artifact logged but never created; best-run search picked FAILED runs; deprecated stage API; reproduce step missed MLflow 3 logged models; W&B run not finished on error | CI pins mlflow 3.17.2 (advisories), not re-run locally; tests are opt-in | 2 (1) |
| `ml-monitoring` (W) | code executed (evidently 0.7.23) | evidently imports broke on 0.7; join key never logged; F1 on float predictions; DAG helper import commented out | real Airflow not run | 1 (1) |
| `model-deployment` (W) | code executed (FastAPI TestClient, torch 2.13) | no authentication and `joblib.load` on an unverified file; TOCTOU re-open; NaN echoed into a 500; `/health` conflated liveness and readiness; `/docs` public; wrong FastAPI lower bound | Docker/Kubernetes/TorchServe commands not executed | 3 (2) |
| `model-optimization` (W) | code executed (torch 2.13, onnxruntime 1.31) | quantized size reported as 0 MB; `eval_accuracy` undefined; missing `onnxscript`; gate checked one variant only | latency effect on real hardware not measured | 2 (2) |

## Responsible AI & Security

| Skill | Phase 1 method | Main findings fixed | Open | Evals |
|---|---|---|---|---|
| `ai-ethics-fairness` (R) | sources (29 CFR 1607.4(D), fairmlbook, Fairlearn docs) | four-fifths rule compared minority to majority; impossibility results misattributed; a reviewer-caught overcorrection about predictive parity; expired TLS on the AIF360 link | none | 2 (0) |
| `data-privacy` (R) | sources (t-closeness paper, Sweeney) + DP numeric check | t-closeness defence misdescribed; Sweeney figure unsourced; AOL/Netflix mechanisms and SP 800-188 title wrong; a cross-reference to `ai-ml-security` that it does not cover | Golle 2006 figure not checked | 1 (0) |
| `explainability` (W) | code executed (shap 0.54.1, lime 0.2.0.1) | random-forest SHAP values are 3-D; Kernel block overwrote `shap_values`; KernelExplainer crashed on a Pipeline's `predict_proba`; LIME fed NumPy rows to name-based pipelines; wrong additivity and LIME-stability Gotchas | shap/IPython version ranges for the crashes not checked | 2 (1) |
| `ai-ml-security` (R) | sources (NVD, OWASP LLM Top 10 2025, NIST AI RMF and AI 100-2) + torch check | out-of-date claim that a default `torch.load` runs code; CVE-2025-32434 missing; wrong claim that poisoning inflates benchmarks like contamination; a bare URL the link checker skipped | none | 3 (0) |

## Cross-cutting observations

- **Executing the code found what reading had not.** The earlier frontmatter migration audit
  (below) found "no evidence issues" in most workflow skills. Running them found that more
  half of the code-bearing skills could not run top to bottom as written, because of
  undefined names or APIs removed upstream, and several more gave silently wrong results.
  All of those are now pinned by tests.
- **Independent probes beat self-checks.** In Phase 2, every eval workstream's own probe
  looked fine, while reviewer-written answers exposed large miss rates
  (`docs/KNOWN-ISSUES.md`, item 2).
- **Reference skills were checked against sources, not executed**, except for the numeric
  and torch checks named above. Unreachable sources are listed as open.

## Earlier audit: frontmatter migration (2026-09-21)

Kept for history. The findings below came from reading the skills; the code was not executed in that pass (see the Phase 1/2 tables above for what execution then found).

Compiled from the 7 domain-batch migration/audit passes run on 2026-09-21, plus
3 net-new skills authored directly to the new spec in the same pass. Every skill
listed here: (a) has the full frontmatter contract in docs/SKILL-SPEC.md, (b)
was read end-to-end for the evidence-discipline test in docs/EVIDENCE.md, (c)
passes `scripts/validate_skills.py` with zero errors. "Changes made" lists only
what was actually rewritten — most skills needed frontmatter only, and that is
recorded as such rather than padded with invented findings.

Legend: **risk** = `risk_level`, **evid** = `evidence_level`.

### Foundations

| Skill | risk | evid | Findings / changes made |
|---|---|---|---|
| `ml-math-essentials` | low | established-practice | No evidence issues found; content already conditionally phrased. |
| `statistics-for-ml` | low | established-practice | No evidence issues found; CLT sample-size and multiple-comparisons claims already correctly hedged/derived. |
| `python-for-ml` | low | official-documentation | No evidence issues found. Added one honestly-labeled `version_constraints` entry (`np.random.default_rng`, estimated since numpy 1.17). |
| `ml-problem-framing` | low | established-practice | **Rewritten:** a fabricated precise statistic ("a one-line heuristic would have gotten 90% of the value") was replaced with language naming the actual dependency ("the actual gap depends on the problem, so measure it") instead of asserting an invented number as fact. |

### Classical ML

| Skill | risk | evid | Findings / changes made |
|---|---|---|---|
| `data-preprocessing` *(gold standard)* | low | established-practice | Frontmatter only, prose preserved. The "~50 unique values" hard threshold had already been softened to an explicit heuristic in an earlier pass this session; left as-is. Added `version_constraints` for ColumnTransformer/OneHotEncoder/SimpleImputer (0.20+, honestly labeled unverified). |
| `feature-engineering` | medium | established-practice | **Rewritten:** "one-hot blows up past ~50 categories" hard threshold → explicit heuristic naming the real dependent variables (row count, feature budget, downstream model). `risk_level: medium` because this skill's own stated purpose is "where leakage most often sneaks in unnoticed" — a mistake here silently inflates reported performance. |
| `supervised-learning` | low | established-practice | No evidence issues found; baseline-first/CV discipline are genuine methodology invariants, not disguised heuristics. |
| `unsupervised-learning` | low | established-practice | No evidence issues found; DBSCAN `eps` and dimensionality remarks were already phrased as "rough heuristic." |
| `model-evaluation` | medium | established-practice | No evidence issues found (already-correct invariants: accuracy-on-imbalance, temporal leakage, calibration ≠ AUC). `risk_level: medium` — this skill's output is the trusted number that gates shipping decisions. |
| `hyperparameter-tuning` | medium | established-practice | No evidence issues found; nested-CV-vs-biased-`best_score_` framing already correct. `risk_level: medium` for the same "silently inflated reported performance" reason as model-evaluation. |

### Deep Learning

| Skill | risk | evid | Findings / changes made |
|---|---|---|---|
| `neural-net-fundamentals` | low | established-practice | No evidence issues found. |
| `cnn-vision` | low | **primary** | Classified `primary` (not defaulted to established-practice) because its central claims (skip connections solving the degradation problem, VGG's depth/diminishing-returns finding) are the specific arguments of the cited ResNet/VGG papers, not generic textbook material. |
| `rnn-sequence` | low | established-practice | No evidence issues found; vanishing-gradient and "reduces, doesn't eliminate" framing already correct. Deliberately classified differently from `cnn-vision`/`attention-mechanisms` rather than defaulting all three to the same tier. |
| `attention-mechanisms` *(gold standard)* | low | primary | Frontmatter only, prose untouched — already tightly sourced to Vaswani et al. 2017. |
| `training-deep-models` | low | official-documentation | No evidence issues found ("always split param groups" / "usually needs no scaler" already correctly scoped). Added one `version_constraints` entry (`torch.cuda.amp` since PyTorch 1.6+). |
| `pytorch-patterns` | low | official-documentation | No evidence issues found; hook-cleanup and state_dict-vs-pickle claims are genuine resource/serialization invariants, not heuristics. No `version_constraints` added — no milestone the reviewer was confident enough to state honestly. |

### LLMs & Generative AI

| Skill | risk | evid | Findings / changes made |
|---|---|---|---|
| `prompt-engineering` | low | established-practice | Temperature/determinism language already softened in an earlier pass (see docs/EVIDENCE.md example 2); left as-is. No further issues found. |
| `rag-pipeline` | medium | established-practice | Added `related: [rag-evaluation, ai-ml-security]` and a new Gotcha pointing to `ai-ml-security` for the "retrieved documents are untrusted content" threat model instead of re-deriving it inline. `risk_level: medium` — a carelessly built pipeline creates real injection/staleness surface. |
| `fine-tuning-llms` | medium | established-practice | No evidence issues found — LR/overfitting claims already hedged. Classified `established-practice` rather than `primary` because the skill's actual claims are operational advice layered on the LoRA/QLoRA papers, not restatements of the papers themselves. |
| `agents-and-tools` | medium | established-practice | Added `related: [agent-evaluation, ai-ml-security]`. **Rewritten:** an absolute superlative ("the description is *the* single biggest lever over tool selection") softened to "one of the biggest levers," naming that tool naming/scope overlap also matter. Cross-referenced `ai-ml-security` from the "unvalidated tool output"/"no sandboxing" Gotchas and `agent-evaluation` from the sandboxing Gotcha, rather than duplicating that content inline. |
| `llm-evaluation` | medium (raised from low in the Phase 3 audit: the Workflow sends prompts, contexts and answers to a hosted, billed API) | established-practice | Temperature=0/determinism Gotcha and code comment already fixed in an earlier pass (see docs/EVIDENCE.md example 3); left as-is. Added `related: [agent-evaluation, rag-evaluation]` and one Overview sentence distinguishing this skill's scope (single-output text quality) from those two siblings'. |

### Specialized Domains

| Skill | risk | evid | Findings / changes made |
|---|---|---|---|
| `nlp-tasks` | low | official-documentation | No evidence issues found; existing hedges already name the actual dependency (e.g. per-model token limits). |
| `computer-vision` | low | official-documentation | No evidence issues found; NMS-threshold/pixel-accuracy gotchas already conditional. Added 2 `version_constraints` entries (torchvision `weights=` enum API, `_v2` detection variants — dated to 0.13, confident milestone). |
| `time-series` | low | established-practice | **Rewritten (2 claims):** (1) the ADF `p<0.05 → d=1` rule was presented as settled fact — added that the cutoff is "a convention, not a proof," that KPSS can disagree, and that `auto_arima` should have final say. (2) "under ~2 years of data is mostly noise" (a fixed calendar-length threshold) → reworded to state the real dependency is *cycle count* in the training data, not calendar length, keeping "~2 years" only as a rough guide. |
| `recommender-systems` | low | established-practice | **Rewritten:** an ALS confidence-scaling constant (`alpha=15`) presented with no caveat → labeled a dataset-dependent hyperparameter to tune against the held-out ranking metric, not a universal constant. |
| `reinforcement-learning` | low | established-practice | No evidence issues found; discount-factor and overestimation-bias Gotchas already explain the actual mechanism rather than asserting a bare threshold. |

### MLOps & Production

| Skill | risk | evid | Findings / changes made |
|---|---|---|---|
| `experiment-tracking` | low | official-documentation | Folded a version-sensitivity warning (MLflow Model Registry stage transitions being superseded by aliases/tags ~2.9+) into the existing "registry stage labels are just metadata" Gotcha. |
| `model-deployment` | **high** | official-documentation | **Rewritten:** the claim that a wrong feature *count* produces "a silent wrong prediction instead of a 400" was factually imprecise — split into the two real failure modes: wrong *order* (same count) is genuinely silent; wrong *count* usually raises an unhandled 500, not a validated 400. Neither is the clean 400 you'd want, so the fix (explicit shape/dtype/identity validation) is unchanged, but the diagnosis is now accurate. `risk_level: high` — a deployment mistake can cause a production outage. |
| `ml-monitoring` | medium | established-practice | **Most significant finding of the audit.** The skill's description promised "prediction drift" as a trigger phrase, but the Workflow only ever computed *data drift* and *ground-truth performance decay* — prediction drift and concept drift were never distinguished, and the wording implicitly treated data drift and performance decay as one continuum. Fixed by: rewriting the Overview to define all four (data/prediction/concept drift, plain performance degradation) with distinct causes; adding a new Workflow step that computes prediction drift on the output column separately from data drift on the input columns; rewriting the ground-truth-performance step to explain it's the only signal that surfaces concept drift, and that decay with *no* drift signal at all should point at a pipeline bug, not a retrain; adding a leading Gotcha naming all four explicitly. This directly satisfies the mission's Phase 12 requirement to distinguish these four concepts. |
| `data-pipelines` | medium | official-documentation | No evidence issues found; all 5 existing Gotchas already phrased conditionally/causally. Added one `version_constraints` entry (Feast `FeatureView(schema=...)` vs. older `features=[Feature(...)]`). |
| `ci-cd-for-ml` | medium | established-practice | No evidence issues found; all 5 Gotchas already hedged. |
| `model-optimization` | medium | official-documentation | No evidence issues found — already the best-hedged skill in this batch. Added one `version_constraints` entry (`torch.quantization` reorganized under `torch.ao.quantization` around PyTorch 1.13/2.0). |

### Responsible AI

| Skill | risk | evid | Findings / changes made |
|---|---|---|---|
| `ai-ethics-fairness` | medium | primary | Added `related: [ai-ml-security]`. No evidence issues found — the "80% rule" disparate-impact threshold was already correctly scoped as "(US EEOC-derived)." |
| `explainability` | low | official-documentation | No evidence issues found — the causal-claim warning on feature importance was already conditional. Added 2 `version_constraints` entries (`permutation_importance` since sklearn 0.22; SHAP `Explanation`-object API from the 0.3x series). |
| `data-privacy` | medium | established-practice | Added `related: [ai-ml-security]`. **Rewritten:** "anonymization aims to be irreversible... no reasonable means exist to re-link records" was stated as a universal technical/legal definition — it is specifically the EU/GDPR "reasonable means" legal test (Recital 26). Rewrote to name that jurisdictional scope explicitly and note other jurisdictions (e.g. US HIPAA Safe Harbor/Expert Determination) define the line differently. |

### AI/ML Security & Agent/RAG Evaluation *(net-new, authored directly to spec)*

| Skill | risk | evid | Notes |
|---|---|---|---|
| `ai-ml-security` | high | established-practice | New — fills the Phase 13 gap. Explicitly distinguishes "security control" from "responsible-AI policy" (the line the Responsible AI skills sit on the other side of). |
| `agent-evaluation` | low | established-practice | New — fills the Phase 8 gap (agent-trajectory/tool-use evaluation, distinct from generic LLM eval). `requires: [agents-and-tools]`. |
| `rag-evaluation` | low | established-practice | New — fills the Phase 9 gap (retrieval metrics, faithfulness, RAG-specific failure taxonomy, distinct from building the pipeline). `requires: [rag-pipeline]`. |

### Cross-cutting observations

- **No fabricated evidence was found or introduced.** Every reviewer either cited a real source already in the skill's References, or explicitly declined to add a `version_constraints` entry when not confident, per the honesty rule in docs/SKILL-SPEC.md. Several reports explicitly note *which* entries were omitted and why (e.g. TRL/bitsandbytes version milestones, exact Optuna/sklearn release numbers) — omission, not invention, was the consistent choice under uncertainty.
- **`evidence_level` was not defaulted uniformly.** Sibling skills in the same domain received different tiers when their actual content warranted it (e.g. `cnn-vision` → `primary` vs. `rnn-sequence` → `established-practice` within Deep Learning; `ai-ethics-fairness` → `primary` vs. `explainability` → `official-documentation` within Responsible AI).
- **The one deliberately-designed cross-cutting check (drift terminology in `ml-monitoring`) found a real, substantive gap**, not a false positive — see above. This is the strongest evidence that the audit step did real work rather than rubber-stamping frontmatter.
- **A validator bug, not a content bug, was the largest source of noise.** `scripts/validate_skills.py --warnings` initially reported 138 "python code block fails ast.parse" warnings across nearly every workflow skill; investigation traced this to `skills_lib.py`'s code-block extraction not dedenting Markdown-list-indented fences before parsing (a tooling defect, fixed in the same session — see git history), not to any actual broken code in the skills. Post-fix: **0 errors, 0 warnings** across all 38 skills.

### Remaining limitations (stated explicitly)

1. Evidence classification is skill-level, not sentence-level — see docs/SKILL-SPEC.md "Known limitations" item 3.
2. No live package/API verification occurred — every `version_constraints` entry and every "no evidence issues found" line above reflects reviewer knowledge at authoring time, not a live check. See docs/FRESHNESS.md.
3. *(Resolved by the Phase 2 router rewrite: on 2026-10-28 "evaluate my model" routes to `model-evaluation`, and "is this difference statistically significant" to `statistics-for-ml`. Current router failure modes are measured in docs/ROUTING.md.)* The router's vocabulary-collision failure mode was reduced but not eliminated by the `capabilities` tags — `model-evaluation` failed to surface for a query that explicitly asked to "evaluate" a model.
4. This audit did not attempt Phase 14's full agent-engineering skill decomposition (tool design, state management, memory, planning, retries, observability as separate skills) — `agents-and-tools` (existing) and `agent-evaluation` (new) cover agent loops and their evaluation, but planning/memory/observability as dedicated skills were judged out of scope for this pass to avoid adding skills without a demonstrated need beyond the mission brief's checklist. Recorded as future work, not silently dropped.
