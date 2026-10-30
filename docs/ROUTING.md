# Routing algorithm

`scripts/router.py` maps a free-text task description to an ordered list of
skills, without loading every `SKILL.md` into an agent's context. It is a
deterministic, offline, standard-library-only **lexical** router: no
embeddings, no LLM call, no network. Every number below comes from the
labelled benchmark in [`evals/routing/`](../evals/routing/README.md) (359
cases, 217 dev / 142 test, in 199 paraphrase families) scored by
`scripts/eval_router.py`, unless a section says it was measured on an earlier
version of the benchmark.

> **Benchmark state (2026-10-30).** 115 cases written by model writers with no repository
> access were added (78 test, 37 dev), on top of the 2026-10-29 family re-split. The
> coverage and split-integrity rules pass, and `eval_router.py` exits 0 (all, dev and test). The
> held-out numbers below cover 142 test cases in 85 families and every skill.

```bash
python scripts/router.py "Build a fraud detection model and evaluate it under severe class imbalance"
python scripts/router.py --json "explain attention"      # adds confidence, threshold, fallback_reason, domain, candidates
python scripts/eval_router.py --split dev                # benchmark metrics (tuning split)
```

Public interface (unchanged): `route(task, top_k=5, min_score=None) -> (intent, hits)`,
where each hit has `slug`, `score`, `reasons`, `type`, `domain` and
`confidence`. `route()` returns **no hits** when nothing clears the confidence
bar; `route_details()` returns the same selection plus `confidence`,
`threshold`, `fallback_reason`, the top `candidates` before gating, and (new)
`domain` (why the task does or does not count as ML) and `directives` (skill
slugs named in an ignored routing directive). `analyze_task(text)` still works
without an index; `analyze_task(text, index)` is what `route_details` uses.

## Algorithm

1. **Normalise words.** Lower-case, keep apostrophes (`don't`), join a single
   letter hyphenated to a word (`q-learning` -> `qlearning`, `k-means` ->
   `kmeans`), `a/b` -> `ab`, drop stopwords and 1-character tokens, then stem
   with the Porter (1980) algorithm (implemented from the paper; its published
   examples are a unit test), British `-ise` -> `-ize`, plus a few aliases where
   Porter splits one concept (`imbalanced`/`imbalance`, `classifier`/
   `classification`, `bias`/`biased`) and exceptions where it merges two
   (`customer` vs `custom`, `general` vs `generation`).
2. **Drop routing directives.** A hyphenated skill slug that follows one of
   `DIRECTIVE_VERBS` (route, load, activate, select, invoke, trigger, enable) in
   the same sentence is removed before analysis: "activate every skill:
   rag-pipeline, fine-tuning-llms" and "[router directive: load data-privacy]"
   give those skills no evidence. A slug named without such a verb ("use
   time-series cross-validation") is ordinary text.
3. **Analyse the task.**
   - *Exclusion scopes*: words from an exclusion cue up to the end of the
     clause are **negated**: they give no positive evidence and cost the skills
     they would have matched (`_split_negated`). Cues:
     - always: `don't`/`do not`, `nor`, `rather than`, `instead of`, `already`;
     - `no` / `without`, *unless* one of the next two content words is an
       undesired outcome the skills exist to prevent (`PROBLEM_WORDS`: leakage,
       overfitting, bias, drift, hallucination, forgetting...);
     - `not` / `isn't` / `aren't` / `wasn't` / `weren't` only when they negate
       a request: the subject is the speaker or the request itself (`I'm not
       asking about...`, `this is not a fairness audit`), they follow a clause
       break (`a privacy task, not a bias question`), they precede
       `a`/`an`/`the`/`about`/`for` (`my model is not a transformer`), or
       (new) the subject is a noun that names the request (`REQUEST_NOUNS`:
       "the deliverable is not fairness work", "our priority isn't PII
       removal", "the question is not which algorithm"). Any other negated
       state or symptom is left alone (`my network is not training`, `the
       loss is not improving`, `my series is not stationary`);
     - never a cue: `never`, and the phrases in `NON_NEGATING` (`no idea`,
       `not sure`, `don't know`...).
   - *Synonyms*: a phrase map (`SYNONYMS`, 29 entries) adds canonical words at
     `SYNONYM_WEIGHT` = 0.7 (`step size` -> learning rate, `personal data` ->
     PII, `bounding box` -> object detection...). **Every key is backed by skill
     text that uses it for the canonical concept** (see [Synonym map
     audit](#synonym-map-audit)). A multi-word key replaces its literal words
     ("ignore previous instructions" is an injection idiom, not a request about
     instructions), and synonym-substituted text feeds the word-pair evidence
     ("fp16" is read as "mixed precision").
   - *Intent*: whole-word cue lists classify `reference` vs `workflow`
     (default `workflow`); the type bonus applies only if a cue was present.
     "how do I / we / you" is procedural, not a reference cue, and a word used
     in its everyday sense ("train" in "train station") is not a cue.
   - *Domain anchor* (`domain_evidence`): the task is about ML if it has a
     STRONG anchor word (`llm`, `classifier`, `pytorch`, `epoch`...), a curated
     ML phrase (an adjacent word pair from some skill's name, capability tags
     or trigger phrases, also after synonym substitution, not made only of
     everyday ML words), or two distinct WEAK anchors (`model`, `train`,
     `data`, `agent`, `leakage`...) used in their ML sense. A weak anchor is in
     its everyday sense when it directly modifies a word no skill's metadata
     uses and the phrase is not headed by an ML word ("model railway", "train
     station", "cluster headaches"; not "train churn models"), or when it is a
     verb whose object noun phrase is headed by such a word ("train my puppy";
     not "train my churn model").
4. **Profile each skill** (cached per skill-file content): fields `name`
   (display name + slug), `capability` (tags), `trigger` (the quoted trigger
   phrases in `description`), `description` (the rest, *excluding* the
   `NOT for` clause), `headings` (new: the bold lead-in of each Workflow step
   and Key Concepts / Gotchas bullet plus the sentence after it, e.g.
   "Memorization in trained models. A model can itself leak its training data:
   large language models have been shown to regurgitate verbatim training
   sequences..."), `overview` (the `## Overview` section + `inputs`/
   `outputs`); its word pairs (name/tags/triggers/description only - headings
   prose pairs are not claims about what a skill is for); and the words that
   occur **only** in its `NOT for` clause (sibling territory).
5. **IDF** over the 38 skill profiles (BM25/Lucene form
   `ln(1 + (N - df + 0.5)/(df + 0.5))`): ~3.3 for a word one skill uses,
   ~0.01 for a word all skills use. The headings field takes part, so it also
   lowers the IDF of words many skills discuss.
6. **Score** each skill:
   - each task word: `idf x field weight x word weight`, using the strongest
     field the skill has it in (name 3.0, capability 3.0, trigger 2.5,
     description 1.5, headings 0.75, overview 0.75);
   - `+1.5 x mean idf` per word pair shared with the skill's name/tags/trigger
     phrases/description;
   - capability tags: `+2.5 x fraction` (x1.5 for multi-word tags) when at
     least 2 of the tag's words (or its only word) are present and the
     IDF-weighted fraction is >= 0.6;
   - trigger phrases: `+3.0 x fraction` under the same >= 2 words / >= 0.6 rule;
   - *compound-sense conflict* (x0.3 on the word): the task says `<word no
     skill uses> <t>` ("fraud detection") while the skill only uses `t` in
     other compounds ("object detection"); or (new) an everyday-sense weak
     anchor ("model railway", "train my puppy") for a skill that uses it to
     modify other words. A pair is a compound only in noun position: not
     after a subject, modal or "to" ("to invent new columns"), not with a past
     participle ("send poisoned updates"), and at the start of a sentence or
     clause only when the second word is a nominalisation (`NOMINAL_SUFFIXES`:
     -tion, -sion, -ment, -ness, -ity, -ance, -ence), so "Fraud detection with
     xgboost..." is a compound and "Strip PII from..." / "emails: strip PII" is
     a verb and its object;
   - x1.15 if an explicit intent cue matches the skill's type (only on
     positive evidence);
   - minus the skill's name/capability/trigger score on negated words;
   - minus `idf` of task words that occur only in the skill's `NOT for` clause.
7. **Select.** `threshold = MIN_SCORE` (5.0), doubled (`NO_ANCHOR_FACTOR`) for
   a task without a domain anchor. The top skill needs `threshold`. Each later
   skill needs `threshold` **and** either `RELATIVE_CUTOFF` (0.6) x the top
   score, or a *residual* score >= `RESIDUAL_MIN` (10.0) on the task words not
   already covered (by name/capability/trigger) by the skills selected so far.
   At most `top_k`.
8. **No-skill fallback.** If nothing clears the bar, return no hits and a
   `fallback_reason` (best candidate, its score, the threshold and, for an
   unanchored task, why it is not ML).
9. **Prerequisites** (`requires`) are prepended only for **selected** skills.
10. **Confidence** per hit = `score / (score + threshold)`: a monotone margin
    indicator, **not** a calibrated probability; scores are not comparable
    across tasks.

Determinism: all iteration over sets is sorted, ties break on slug, and
`tests/test_router.py::TestCli::test_deterministic_across_hash_seeds` checks
byte-identical `--json` output under different `PYTHONHASHSEED` values.

### Thresholds and weights

All constants are module-level in `scripts/router.py`. The 2026-10-27 values
were tuned on the original dev split; the parameters added on 2026-10-29 were
chosen on the re-split dev split only.

| Constant | Value | Role |
|---|---|---|
| `MIN_SCORE` | 5.0 | absolute minimum for any selection |
| `NO_ANCHOR_FACTOR` | 2.0 | threshold multiplier when the task has no ML-domain anchor |
| `RELATIVE_CUTOFF` | 0.6 | later hits within this fraction of the top score are kept |
| `RESIDUAL_MIN` | 10.0 | ...or if they score this on words the earlier picks left uncovered |
| `FIELD_WEIGHTS` | 3 / 3 / 2.5 / 1.5 / 0.75 / 0.75 | name / capability / trigger / description / headings / overview |
| `BIGRAM_FIELDS`, `ANCHOR_BIGRAM_FIELDS`, `CONFLICT_BIGRAM_FIELDS` | see code | which fields' word pairs count as phrase evidence, as ML-phrase anchors, and as "the skill's own compounds" |
| `BIGRAM_WEIGHT`, `CAPABILITY_TAG_WEIGHT`, `TRIGGER_PHRASE_WEIGHT` | 1.5, 2.5, 3.0 | phrase evidence |
| `PHRASE_MIN_FRACTION` | 0.6 | minimum IDF-weighted share of a tag/trigger phrase present |
| `SYNONYM_WEIGHT` | 0.7 | weight of synonym-added words |
| `INTENT_BONUS` | 0.15 | multiplicative type bonus, evidence-gated |
| `NEGATION_PENALTY`, `NOT_FOR_PENALTY` | 1.0, 1.0 | negative evidence |
| `MODIFIER_CONFLICT_FACTOR`, `HEAD_CONFLICT` | 0.3, on | compound-sense conflict discount, and its everyday-sense case |
| `REQUEST_NOUNS`, `DIRECTIVE_VERBS`, `VERB_CONTEXT`, `OBJECT_DETERMINERS`, `PROCEDURAL_SUBJECTS` | word lists | request-noun negation, routing directives, compound/verb position, "how do I" |
| `NOMINAL_SUFFIXES` | -tion -sion -ment -ness -ity -ance -ence | a clause-initial word pair is a compound only if its second word ends in one (English deverbal noun suffixes, not benchmark vocabulary) |

The variants tried on dev (then 169 cases) and rejected on 2026-10-29: indexing the whole
Workflow/Key Concepts/Gotchas prose (weight 0.25: precision 0.824 but task
accuracy 0.828; 0.5: task 0.822), lead-ins without their sentence (task 0.858,
but none of the #31 paraphrases routed), headings weight 1.0/1.5 (task
0.852/0.840, more unnecessary activations), IDF computed without the headings
field (precision 0.654), headings word pairs as phrase evidence (put
`computer-vision` back on "fraud detection ... class imbalance"), requiring
multi-word evidence for every unanchored selection (negative false activation
0.297 -> 0.216 but task accuracy 0.852 -> 0.817), and restricting the
compound-sense rule to name/capability pairs (no net change).

Single-component ablations of the final revision (dev split, 180 cases, run
2026-10-30 after the review fixes; columns are task accuracy, precision,
recall, top-1, false activation over all negatives, forbidden rate,
unnecessary per query):

| variant (dev, 180 cases) | task | prec | recall | top1 | negFA | forbid | unneeded |
|---|---|---|---|---|---|---|---|
| this revision, everything on | 0.783 | 0.754 | 0.812 | 0.761 | 0.237 | 0.068 | 0.278 |
| no headings field | 0.761 | 0.701 | 0.800 | 0.761 | 0.263 | 0.119 | 0.361 |
| domain gate: word lists only (previous rule) | 0.767 | 0.733 | 0.800 | 0.747 | 0.263 | 0.068 | 0.306 |
| no everyday-sense detection | 0.778 | 0.720 | 0.819 | 0.761 | 0.289 | 0.085 | 0.333 |
| no request-noun negation | 0.772 | 0.732 | 0.812 | 0.761 | 0.237 | 0.136 | 0.311 |
| routing directives obeyed | 0.772 | 0.736 | 0.812 | 0.761 | 0.263 | 0.102 | 0.306 |
| multi-word synonyms keep their literal words | 0.778 | 0.743 | 0.812 | 0.761 | 0.263 | 0.068 | 0.294 |
| pre-audit synonym map (77 keys) | 0.822 | 0.756 | 0.863 | 0.810 | 0.263 | 0.068 | 0.289 |
| round-1 audited map (46 keys, "occurs anywhere in a skill") | 0.806 | 0.756 | 0.844 | 0.782 | 0.263 | 0.068 | 0.283 |
| no synonym map | 0.761 | 0.754 | 0.787 | 0.732 | 0.237 | 0.068 | 0.267 |
| clause start always a verb position (round-1 compound rule) | 0.778 | 0.746 | 0.812 | 0.761 | 0.237 | 0.085 | 0.289 |
| no head-position compound conflict | 0.772 | 0.742 | 0.806 | 0.753 | 0.263 | 0.085 | 0.294 |

The synonym rows are the cost of the stricter audit on dev: the dropped keys
were written after reading dev queries, so dev is where they help. On the
held-out split the round-1 (46-key) and the current (29-key) maps score the
same task accuracy, recall and top-1 (0.797 / 0.807 / 0.674 on the re-split
test split, aggregate runs on 2026-10-29 and 2026-10-30).

### Synonym map audit

The map was written in the first rewrite after reading dev queries; on the
held-out split it added nothing (2026-10-27: test task accuracy 0.800 with and
without it). It has been audited twice on 2026-10-29/30.

- **Round 1** kept a key if the phrase occurred anywhere in a skill's text,
  code included. Review showed that this does not establish equivalence: it
  kept "shift", "bot", "assistant", "strip" and "nan" because the words occur
  somewhere, and "I need an assistant to schedule meetings" was read as an LLM
  request.
- **Round 2 (current)** applies one rule to all 77 original keys. A skill's
  metadata (description before `NOT for`, capabilities, display name, slug) is
  one passage, and each paragraph, list item and Workflow step of its body is
  another; fenced code, HTML comments, link targets and the References
  section are left out. A key is kept if
  - **same-passage**: some passage contains the key phrase and at least one
    canonical word that is not part of the key ("fp16" in the
    `training-deep-models` gotcha about AMP dtypes, which also says "mixed
    precision"), and, **for a one-word key**, at least half (`SENSE_MIN`) of
    all passages that contain it do so (the library does not mostly use it in
    another sense); or
  - **variant**: a spelling of a kept key or of a metadata word, with the same
    canonical words (`ignore all previous instructions`, `warm up`).

**29 keys are kept, 48 removed** (31 in round 1, 17 in round 2). Round 2
removed `language model`, `chatbot`, `chat bot`, `bot`, `gpt`, `assistant`
and `machine learning` (no passage uses them with "LLM" / "ML": the only
co-occurrences are in reference titles), `jailbreak`, `grader`, `strip`,
`scrub`, `held out`, `by chance` (no passage with the canonical words), and
`nan` (0 of 6 passages: the skills use NaN for a diverged loss or an undefined
metric, not a missing value), `holdout` (1 of 3), `shrink` (1 of 10) and
`attribution` (4 of 11; the stem also matches "attributes"). Since the
2026-10-30 `rag-pipeline` description edit ("a chatbot or support bot that
answers from your own documents"), `bot` meets the same-passage rule, but it
stays out of `router.SYNONYMS`; `tests/test_router_synonym_audit.py` asserts
both. Two keys the
review questioned are kept on this rule, with their numbers in the JSON:
`shift` -> drift (7 of 13 passages, all in `ml-monitoring`, use it with
"drift") and `http` -> api endpoint (1 of 1, `model-deployment`). "distribution
shift" stays removed because no skill writes it.

Cost on dev (180 cases): task accuracy 0.806 with the round-1 map, 0.783 with
this one; the dev cases lost are `par-018`, `par-020`, `par-047`, `amb-003`
and `mul-010`. On the held-out split the two maps score the same (see the
ablation note above). The fix for the lost cases is skill text, not router
vocabulary: see [Skill wording from dev failures](#skill-wording-from-dev-failures-applied-2026-10-30).
`evals/routing/synonym-audit.json` has, per key, the supporting skills and the
passage counts, and per removed key the reason and the dev cases that used it.
`tests/test_router_synonym_audit.py` recomputes the rule against the current
skills (so a skill edit that removes the backing text fails it) and fails if
`SYNONYMS` and the audit disagree.

## Measured before / after

### Revision 2026-10-29/30 (current)

#### Held-out test split, 142 cases (2026-10-30)

Measured after the blind-written cases were added and the six description
edits in [Skill wording from dev
failures](#skill-wording-from-dev-failures-applied-2026-10-30) were applied.
It is the first measurement on the 78 blind-written test cases, and
nothing was tuned against them. All three rows score the same cases with the
same current skill metadata; only the router code differs. "HEAD" is
`scripts/router.py` and `scripts/skills_lib.py` at git `51743b0`, "keyword" the
pre-improvement router at `8328c60`, each copied out with `git show` and
passed with `--router`:

```bash
python scripts/eval_router.py --split test
python scripts/eval_router.py --split test --router <copy of 51743b0>/router.py
python scripts/eval_router.py --split test --router <copy of 8328c60>/router.py
```

| router | prec | recall | top-1 | task | falseAct (all negatives) | unneeded / q | forbid | abstain |
|---|---|---|---|---|---|---|---|---|
| keyword (`8328c60`) | 0.139 | 0.660 | 0.354 | 0.303 (43/142) | 1.000 (60/60) | 4.599 | 0.506 | 0.000 |
| HEAD (`51743b0`) | 0.492 | **0.720** | **0.573** | 0.655 (93/142) | 0.317 (19/60) | 0.718 | 0.153 | 0.352 |
| this | **0.547** | 0.700 | 0.549 | **0.711 (101/142)** | **0.200 (12/60)** | **0.570** | **0.106** | 0.408 |

95% Wilson intervals (k/n [low, high]):

| metric | keyword | HEAD | this |
|---|---|---|---|
| task accuracy | 43/142 [0.233, 0.383] | 93/142 [0.574, 0.728] | 101/142 [0.632, 0.779] |
| recall | 66/100 [0.563, 0.745] | 72/100 [0.625, 0.799] | 70/100 [0.604, 0.781] |
| top-1 | 29/82 [0.259, 0.462] | 47/82 [0.465, 0.675] | 45/82 [0.441, 0.652] |
| false activation over negatives | 60/60 [0.940, 1.000] | 19/60 [0.213, 0.442] | 12/60 [0.118, 0.318] |
| forbidden selection | 43/85 [0.402, 0.610] | 13/85 [0.092, 0.244] | 9/85 [0.057, 0.189] |

Cases in one family are correlated (142 cases in 85 families), so the
intervals are optimistic. Also for this router: no-skill precision 0.828
(48/58) and recall 0.800 (48/60), against HEAD's 0.820 (41/50) and 0.683
(41/60); both select nothing on all 17 `no-skill` cases.

By category, task accuracy (keyword / HEAD / this): typical 0.750 / 0.750 /
0.812 (16), paraphrase 0.381 / 0.429 / 0.429 (21), ambiguous 0.625 / 0.750 /
0.812 (16), multi-skill 0.562 / 0.750 / 0.750 (16), no-skill 0.000 / 1.000 /
1.000 (17), conflicting 0.273 / 0.545 / 0.455 (11), adversarial 0.022 / 0.556
/ 0.711 (45).

Paired with HEAD, case by case on task-level success: 12 cases are right only
with this router and 4 only with HEAD's. The exact two-sided McNemar test
gives p = 0.077. It assumes independent cases, which cases in one family are
not, so the difference is **not significant**. By source: on the 78
blind-written cases this router gets 50 (0.641) and HEAD's 43 (0.551); on the
64 original test cases, 51 and 50.

Label provenance: each blind-written test case was labelled by its writer and
independently by a second blind model reviewer; the 64 original test cases
were labelled again by two blind model reviewers. Of the 142 test cases, 135
are `agreed` and 7 `adjudicated`: `amb-017` and `mul-019` (compatible
reviewer sets, labels kept), `hw0-011` (a training-data extraction question,
settled by the coordinator under `ai-ml-security`'s `NOT for` clause), and
`hw0-030`, `hw0-033`, `hw0-040`, `hw0-043` (the blind reviewer's labels
differed; each case's `notes` say how it was settled). The writers and
reviewers are models; no person has reviewed the held-out labels
(`evals/routing/README.md`, "Methodology").

Dev split, same day (217 cases in 114 families, tuning data): this router task
accuracy 0.825 (179/217, [0.769, 0.870]), precision 0.780, recall 0.854, top-1
0.801, false activation over negatives 0.220 (9/41); HEAD's router 0.737
(160/217, [0.675, 0.791]), 0.656, 0.818, 0.756, 0.342 (14/41). The dev split
holds the 41 cases written for known failure modes, and the description edits
were fitted to dev failures, so dev favours this router.

**Read these honestly:**

- **It is a trade-off, not a uniform gain.** On the held-out split this router
  beats HEAD's on task accuracy, precision, unnecessary activations, forbidden
  selections and false activation over negatives. It is behind on recall (70
  vs 72 of 100 expected skills) and top-1 (45 vs 47 of 82), and it abstains
  more (58 vs 50 of 142 cases, 10 vs 9 of the 82 that expect a skill).
- **Not significant.** McNemar p = 0.077 under an independence assumption
  the families break, and each interval in the table overlaps HEAD's.
- **The HEAD row uses the edited metadata.** It compares router code only; it
  is not the router as shipped at `51743b0` with that commit's skill text.
- **The CI no-regression check needs an explicit acceptance.** On base
  `51743b0`'s benchmark and baseline (the original 85-case test split, 21 of
  whose cases are now dev and were tuned on), this router has recall 0.8571 <
  0.8701 and top-1 0.7727 < 0.7879: `amb-007` no longer selects
  `data-preprocessing`, `con-004` no longer selects `llm-evaluation` and
  `mul-002` drops `supervised-learning`, while `par-040`, `mul-014`,
  `adv-009`, `adv-012` and `adv-026` are fixed. The three regressed cases are
  still test cases and were not tuned against; two of the fixed ones
  (`par-040`, `adv-026`) are now dev cases the router was tuned on, so the net
  loss may understate the regression on unseen requests. The two values are recorded in `baseline.json`
  `accepted_regressions` (`evals/routing/README.md`, "Accepted regressions"),
  which turns exactly those values into warnings. Run locally on the
  2026-10-30 tree, `eval_router.py --gate --base-sha HEAD` exits 0 with two
  warnings; no CI run has been observed.
- **The keyword-router floor passes.**
  `TestRegressionFloor.test_current_floor_not_below_pre_improvement` now
  compares with the keyword router re-measured on this benchmark: all-splits
  recall 0.802 vs 0.725, task accuracy 0.780 vs 0.410. Its earlier failure
  (0.811 vs 0.831) compared numbers from two different benchmarks.
- **The held-out labels come from models.** The writers had not seen the
  `NOT for` clauses, which is why the coordinator adjudicated the privacy
  attack cases. A person may label some cases differently.
- **"False activation (no-skill)" is the old metric** and counts only the
  `no-skill` category; the all-negatives column counts every case with an
  empty `expected` (60 on test: 17 `no-skill`, 43 `adversarial`) and is gated.

#### Earlier measurements on the 244-case benchmark (history)

Measured on 2026-10-29/30 on the benchmark before the blind-written cases
(244 cases, 180 dev / 64 test) and **before the description edits**, with the
working tree's skill metadata of that day. The CLI exited 2 then (the
coverage rules failed), so these numbers come from `eval_router.evaluate()`
on the same cases.

"HEAD" is `scripts/router.py` at git `51743b0`, run on the same benchmark and
skill metadata.

| split | router | prec | recall | top-1 | task | falseAct (no-skill) | falseAct (all negatives) | unneeded / q | forbid | abstain |
|---|---|---|---|---|---|---|---|---|---|---|
| dev, re-split (180) | HEAD | 0.614 | **0.819** | 0.754 | 0.744 | 0.167 | 0.342 (13/38) | 0.539 | 0.220 | 0.222 |
| dev, re-split (180) | this | **0.754** | 0.812 | **0.761** | **0.783** | **0.111** | **0.237 (9/38)** | **0.278** | **0.068** | 0.278 |
| dev, original (118) | HEAD | 0.755 | **0.929** | **0.894** | **0.873** | 0.077 | 0.292 | 0.331 | 0.148 | 0.186 |
| dev, original (118) | this | **0.790** | 0.866 | 0.830 | 0.822 | 0.077 | **0.250** | **0.254** | **0.111** | 0.220 |

95% Wilson intervals on the re-split dev split: task accuracy 141/180,
[0.718, 0.837] (HEAD 134/180, [0.676, 0.803]); false activation over
negatives 9/38, [0.130, 0.392] (HEAD 13/38, [0.212, 0.501]). The intervals
overlap; cases in a family are correlated, so the real uncertainty is larger.

By category, re-split dev split (task accuracy HEAD -> this): typical 0.971
-> 1.000 (35), paraphrase 0.542 -> 0.583 (48), ambiguous 0.786 -> 0.643 (14),
multi-skill 0.938 -> 0.813 (16), no-skill 0.833 -> 0.889 (18), conflicting
0.800 -> 0.920 (25), adversarial 0.542 -> 0.708 (24).

On the original 118-case dev split, 9 cases HEAD got right now fail
(`par-003`, `par-018`, `par-020`, `par-035`, `par-036`, `amb-003`, `mul-010`
from the synonym audit; `amb-016`, `mul-006` from the headings field) and 3 are
fixed (`adv-005`, `con-008`, `par-006`). Seven of the nine route correctly
with the [skill wording](#skill-wording-from-dev-failures-applied-2026-10-30).

**Held-out evidence then: the re-split test split (64 cases), scored in aggregate
only**, on 2026-10-29 (round 1, router frozen) and once more on 2026-10-30
after the review fixes were frozen; no router change was made after either
run and no per-case test output was inspected:

| split | router | prec | recall | top-1 | task | falseAct (all negatives) | unneeded / q | forbid |
|---|---|---|---|---|---|---|---|---|
| test, re-split (64) | HEAD | 0.598 | **0.842** | **0.739** | 0.781 | 0.278 (5/18) | 0.578 | 0.214 |
| test, re-split (64) | round 1 (2026-10-29) | 0.628 | 0.807 | 0.674 | 0.797 | 0.167 (3/18) | 0.500 | 0.143 |
| test, re-split (64) | this (2026-10-30) | **0.651** | 0.807 | 0.674 | **0.797** | **0.167 (3/18)** | **0.453** | **0.143** |

95% Wilson intervals (this): task accuracy 51/64 [0.683, 0.877]; recall 46/57
[0.687, 0.889]; top-1 31/46 [0.530, 0.791]; false activation over negatives
3/18 [0.058, 0.392].

**Not held-out: the original 85-case test split.** 21 of its 85 cases moved to
dev in the re-split and the router was tuned on them, so these rows are partly
tuned and are shown only because the CI no-regression check runs on this
split (the parent commit's benchmark and baseline):

| split | router | prec | recall | top-1 | task | falseAct (all negatives) | unneeded / q | forbid |
|---|---|---|---|---|---|---|---|---|
| test, original (85; 21 tuned on) | HEAD | 0.625 | 0.870 | 0.788 | 0.800 | 0.316 (6/19) | 0.529 | 0.227 |
| test, original (85; 21 tuned on) | this | 0.682 | 0.857 | 0.773 | 0.835 | 0.211 (4/19) | 0.412 | 0.136 |
| test, original (85; 21 tuned on) | this, description edits applied (gate run, 2026-10-30) | 0.670 | 0.857 | 0.773 | 0.835 | 0.211 (4/19) | 0.435 | 0.136 |

On those 64 cases the revision traded recall (-0.035, two expected skills)
and top-1 (-0.065, three cases) for task accuracy (+0.016, one case),
precision, forbidden selections and false activation over negatives (5/18 ->
3/18). The 142-case results above supersede them.

### Skill wording from dev failures (applied 2026-10-30)

Most dev cases this revision lost, and several failure-mode cases, need words
that no skill's text had. Router vocabulary for them would be the dev-fitted
synonym map again; the fix was skill metadata. `PROPOSED_METADATA` in
`tests/test_router_regressions.py` held the exact description edits, and
`TestPendingMetadata` checked, with the edits applied in memory, that every
listed case routed correctly, that each still failed without its edit, and
that no other dev case broke. On 2026-10-30 the edits were applied to the
`SKILL.md` descriptions and all twelve cases were observed routing correctly
with the real metadata; `PROPOSED_METADATA` is now empty and the cases are
pinned in `FIXED_DEV_CASES` and `TestFailureModeRegressions`:

| skill | wording added to `description` | dev cases it fixes |
|---|---|---|
| `ai-ethics-fairness` | "..., such as whether outcomes differ across gender, race, ethnicity or age groups." | `par-035`, `con-027`, `mul-006` |
| `ai-ml-security` | "..., protecting them against attackers, ..."; trigger phrase "is there a backdoor in this model" | `amb-016`, `par-055` |
| `rag-pipeline` | "(such as a chatbot or support bot that answers from your own documents)" | `par-018`, `mul-010` |
| `agents-and-tools` | "(a language model that decides which tools or APIs to call)" | `par-020` |
| `llm-evaluation` | "(a chatbot or RAG app)" | `amb-003` |
| `training-deep-models` | trigger phrase "my validation loss stopped improving" | `par-050`, `par-051`, `par-052` |

Measured in memory before they were applied, on dev only (180 cases): task
accuracy 0.783 -> 0.850, recall 0.812 -> 0.888, top-1 0.761 -> 0.838, false
activation over negatives unchanged (0.237), no dev case broken. The wording
was written after seeing these dev failures, so it is fitted to dev; the
held-out numbers above are the ones to read. `par-003` (labelled data -> which
algorithm) is not fixed by "labelled data" in `supervised-learning`'s
description alone and is listed in `REGRESSED_DEV_CASES`.

### First rewrite, 2026-10-27 (history)

Recorded on the original 203-case benchmark (118 dev / 85 test, benchmark
sha256 `297a157e...`), kept in `baseline.json` `history`: the
pre-improvement keyword router (git `8328c60`) had task accuracy 0.534 dev /
0.553 test and false activation 1.000; the first rewrite reached 0.873 dev /
0.800 test with precision 0.755 / 0.625. Those numbers were measured against
skill metadata as it stood on 2026-10-27 and are not re-runs. The tuning
method then: weights and thresholds swept on dev only, test scored in
aggregate after tuning was frozen, one incidental test id seen in
`--write-baseline` output; a later negation-scope review fix was designed on
the review's own examples and dev.

The regression floor (`tests/test_eval_router.py::TestRegressionFloor`)
enforces the top-level entry of `evals/routing/baseline.json` on every metric
and split with no tolerance; earlier entries are kept under `history`
(pre-improvement keyword router first). The router reads skill names,
capabilities, descriptions, Overview sections and bullet lead-ins, so
**SKILL.md edits move these numbers too**; re-baseline deliberately.

## Distinguishing REFERENCE from WORKFLOW asks

`classify_intent()` counts whole-word cue matches from `REFERENCE_CUES`
("what is", "explain", "how does"...) and `WORKFLOW_CUES` ("build", "train",
"deploy"...); more reference cues -> `reference`, otherwise `workflow`. "how
do I / we / you" is not a reference cue (it asks for a procedure), and a weak
anchor in its everyday sense is masked first. The type bonus is small
(x1.15), multiplicative, and only applied when a cue was present and the
skill already has lexical evidence.

## Known failure modes (measured, not hypothetical)

From the dev split (per-case output was inspected only for dev). The 38 dev
cases still misrouted on 2026-10-30 (217-case dev split, description edits
applied): `par-003`, `par-008`, `par-010`, `par-025`, `par-036`, `par-046`,
`par-047`, `par-048`, `par-049`, `par-053`, `par-054`, `par-056`, `par-057`,
`amb-008`, `amb-018`, `amb-023`, `mul-001`, `non-018`, `non-023`, `con-025`,
`adv-006`, `adv-008`, `adv-010`, `adv-020`, `adv-021`, `adv-024`, `adv-032`,
and 11 of the 37 blind-written dev cases: `hw0-006`, `hw0-010`, `hw0-016`,
`hw0-017`, `hw0-025`, `hw0-026`, `hw0-028`, `hw0-029`, `hw0-032`, `hw0-038`,
`hw2-003`. Most of the blind-written ones describe a goal or a symptom
instead of using a skill's terms: "who's likely to cancel" (`hw0-016`,
`hw0-017`, expecting `ml-problem-framing`), "under 50ms" or "running on a
phone" (`hw0-028`, `hw0-029`: `model-optimization` missed, `cnn-vision` or
`pytorch-patterns` selected), "keep an eye on it in prod" (`hw0-032`:
`ml-monitoring` missed), a help-desk bot that "pulls answers from
Confluence" (`hw0-025`, `hw0-026`: `rag-evaluation` missed, nothing or
`llm-evaluation` selected), and an LLM that might "spit out customer emails
... word for word" (`hw0-010`: `data-privacy` missed, `nlp-tasks` and
`fine-tuning-llms` selected).

- **Vocabulary that no skill text has still routes nowhere.** "detect fraud in
  credit card transactions" (`par-046`), "which subscribers are likely to
  cancel next month" (`par-048`), "tomorrow's electricity demand from meter
  readings" (`par-049`) and "real or just noise" (`par-008`) have no ML word
  and no metadata vocabulary, so they stay under the raised bar. The headings
  field closed some gaps from skill text ("the validation loss is not
  improving after epoch 3" -> `training-deep-models`, but only at score 5.2
  against the 5.0 minimum, so a small metadata edit can undo it; some
  membership-inference and training-data-extraction paraphrases ->
  `data-privacy`; a backdoored downloaded checkpoint described at length ->
  `ai-ml-security`), but only where a skill already writes the words.
- **Some close paraphrases of those still fail** (the review's probes, now
  dev cases). The `training-deep-models` trigger phrase "my validation loss
  stopped improving" fixed `par-050`-`par-052`, but "How many epochs should I
  train for?" (`par-053`) and "My loss is not decreasing." (`amb-023`) still
  select nothing. Poisoning, backdoor and membership-inference
  requests that avoid the terms ("slipped mislabeled samples into our training
  data to sabotage the model", "planted a trigger pattern in our image
  classifier", "could someone figure out if my data was in the training set")
  still select nothing or the vision skills (`par-054`, `par-056`, `par-057`);
  "a backdoor in a checkpoint we downloaded" (`par-055`) now routes to
  `ai-ml-security`.
- **Vocabulary the synonym audit removed** routes nowhere until skill text has
  it: "my data has NaN values" (`par-047`). "evaluate my chatbot" (`amb-003`),
  "our support bot ... look things up in our manuals" (`par-018`) and "the
  language model decides when to call our APIs" (`par-020`) route correctly
  since the description edits.
- **Homonyms of a skill's own name, trigger phrase or a STRONG anchor still
  activate it**: "prompt-engineering my wedding speech" (`adv-021`), "the most
  important feature ... dishwasher" (`adv-010`, trigger "feature
  importance"), "Debug this Python IndexError" (`non-018`), "sleep regression"
  (`adv-032`: "regression" is a strong anchor), "golden retriever ...
  reinforce" (`adv-024`), "train my puppy ... positive reinforcement"
  (`adv-008`: the train part is caught, "positive reinforcement" is not).
- **The everyday-sense rule is a heuristic.** It needs the neighbouring word
  to be absent from every skill's metadata, so an off-topic word that some
  skill happens to use ("layout", "price") hides the everyday sense, and an
  ML request whose object is an unusual word ("train my chatbot-for-plumbers"
  style phrasing) can lose an anchor. Strong anchors are never discounted.
- **Off-topic CI / infra requests with library vocabulary**: "Set up a GitHub
  Actions workflow that deploys my static website" (`non-023`) selects
  `ci-cd-for-ml` and `model-optimization`; the `github actions` -> CI synonym
  is backed by `ci-cd-for-ml`'s text, and nothing in the request says it is
  not ML.
- **Keyword soup** ("data drift SHAP LoRA RAG ... what should I cook?",
  `adv-006`) still selects the named techniques: a lexical router cannot tell
  which sentence is the real request. Routing *directives* naming slugs are now
  ignored, but a list of technique names is not a directive.
- **Negation scoping is rule-based.** Request nouns, request subjects,
  determiners and clause breaks cover the benchmark's and the reviews'
  phrasings; others will be mis-scoped. In `con-025` the exclusion works (the
  excluded skill is not selected) but the requested skill still scores under
  the bar.
- **A secondary skill can be dropped** when the first skill's words cover the
  request (`mul-001`).
- **The compound-sense rule is lexical.** It reads "age groups" and "credit
  model" as compounds whose modifier no skill uses and discounts the head for
  skills that only use it in other compounds; the verb-position,
  past-participle and clause-initial nominalisation rules remove the worst
  misreadings ("strip PII", "send poisoned updates") while keeping
  "Fraud detection with xgboost...", not all of them (a clause-initial
  compound whose head is a plain noun, "Fraud model with xgboost...", is read as
  verb + object).
- Hand-curated lists (stopwords, anchors, request nouns, directive verbs,
  synonyms, stem aliases) are English-only and incomplete.

## What a deterministic lexical router cannot guarantee

- No semantic retrieval: a request that shares no word (after stemming and
  the audited synonyms) with any skill's text cannot be routed, and a request
  that shares words with the wrong skill can be.
- No guarantee on unseen phrasing: every rule above was written against the
  benchmark's dev split and the reviews' examples. The held-out estimate is
  142 cases in 85 families, labelled by models, with a task-accuracy interval
  of [0.632, 0.779]; on the 78 blind-written cases task accuracy is 0.641, so
  requests worded unlike the skills route worse than the benchmark average.
- No recall for a request that names no ML term: "detect fraud in credit card
  transactions" and "which subscribers are likely to cancel" select nothing,
  and the router abstains on 10 of the 82 held-out cases that expect a
  skill.
- No calibrated confidence: `confidence` is a margin over the threshold.
- No protection against adversarial wording beyond the rules listed: an
  attacker who avoids directive verbs or uses unlisted homonyms can still
  steer the selection. The router chooses which skills to *read*; it is not a
  security boundary.
- Skill text is part of the model: any `SKILL.md` edit can change routing.

## CI gate and base commit

The routing gate is `python scripts/eval_router.py --gate` (`run_gate`). It
runs three checks on the test split over every metric in `THRESHOLD_FLAGS`
that the relevant baseline records (eight in this commit's baseline, including
`negative_false_activation_rate`; seven in 51743b0's): integrity (`benchmark_sha256`), this
commit's router vs this commit's benchmark + baseline (which must also pass
the coverage and family rules), and this commit's router vs the **base
commit's** benchmark + baseline. `resolve_base()` picks the base commit:

| event | explicit `--base-sha` | base used |
|---|---|---|
| pull_request with `--head-sha` equal to HEAD (head-only checkout, even of the author's own merge commit) | any | the push rules below (explicit ancestor as is, else the merge base) |
| pull_request with `--head-sha`, HEAD a merge whose second parent is not the PR head | any | the gate fails (`GateError`: unknown checkout) |
| pull_request, HEAD a merge commit (refs/pull/N/merge) | none | first parent of HEAD |
| pull_request, HEAD a merge commit | `pull_request.base.sha`, contained in the first parent | first parent (the base tip actually merged into) |
| pull_request, HEAD a merge commit | base not contained in the first parent | the explicit base (the first-parent assumption is wrong here) |
| push / other, or a head-only PR checkout | an ancestor of HEAD (`github.event.before`) | the explicit base, so every commit of a multi-commit push is covered |
| push (force push) / head-only PR checkout after the base moved | not an ancestor | merge base of the two |
| any | empty or all zeros (new branch) | first parent of HEAD; none for a root commit (check reported NOT RUN) |
| any | not in the clone, or no shared history | the gate fails (`GateError`); a requested comparison is never skipped silently |

Without `--head-sha`, every merge commit in a pull_request event is taken for
GitHub's test merge. That is wrong for a head-only checkout whose PR head is
the author's own merge commit and whose first parent already contains
`base.sha`: the gate would compare against that first parent instead of the
base. Pass `--head-sha "${{ github.event.pull_request.head.sha }}"` to rule it
out (`test_head_only_checkout_of_an_author_merge_commit`).

A base that cannot be resolved fails the gate. A resolved base that has no
`baseline.json` (it predates the routing benchmark), or no base at all (a root
commit with no explicit base), skips the no-regression check with a
`::warning::` and "NOT RUN" in the summary, and does **not** fail the gate.

`tests/test_router_gate.py` exercises each row in throwaway git repositories,
including a two-commit push whose first commit regresses the router and lowers
`baseline.json`: compared with `HEAD^1` it passes, compared with
`github.event.before` it fails. The workflow must fetch enough history for the
base; `ci.yml`'s evals-offline job checks out with `fetch-depth: 0` and runs
`eval_router.py --gate --base-sha "$ROUTING_BASE_SHA"`. Run locally on
uncommitted changes, pass `--base-sha HEAD`.

A no-regression failure (check 3) that a reviewer accepts is recorded in
`baseline.json` `accepted_regressions`, pinned to the base benchmark's hash,
the metric and the exact measured value; the gate reports a match as a
`::warning::` and fails anything else (`evals/routing/README.md`, "Accepted
regressions"; tests in `tests/test_router_gate.py`).

## Extension point: semantic retrieval (not enabled)

A semantic layer (sentence embeddings of each skill's description/Overview,
cosine similarity as an extra score term or a re-ranker over
`route_details().candidates`) is the obvious next step for the paraphrase and
homonym failures above. It is **not** enabled, because:

- it cannot be measured here: `huggingface.co` does not resolve on the
  development machine, so no embedding model could be downloaded, and an
  unmeasured change would violate the benchmark-first method above;
- the default router must stay offline, deterministic and standard-library
  only; a remote embedding or LLM API must never become a dependency of the
  default path.

How to add one without touching the default: write a module exposing
`route(task, top_k=5)` that calls `router.route_details(task, top_k=...)`,
re-scores `candidates` with a local model, re-applies the same gating, and
compare it with `python scripts/eval_router.py --router path/to/semantic_router.py
--split dev` (then once on `--split test`). A candidate module may define
dataclasses under `from __future__ import annotations`: `load_route_fn`
registers it in `sys.modules` before executing it.

## Router vs. a real retrieval system

The router answers "which 1-3 skills should I load for this task, or none",
not "rank every skill by calibrated relevance". Scores are only meaningful
relative to each other within one call, and `confidence` is a margin over the
threshold, not a probability.
