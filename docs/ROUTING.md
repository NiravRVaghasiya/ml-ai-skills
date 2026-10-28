# Routing algorithm

`scripts/router.py` maps a free-text task description to an ordered list of
skills, without loading every `SKILL.md` into an agent's context. It is a
deterministic, offline, standard-library-only **lexical** router: no
embeddings, no LLM call, no network. Every number below comes from the
labelled benchmark in [`evals/routing/`](../evals/routing/README.md) (203
cases, 118 dev / 85 test) scored by `scripts/eval_router.py`.

```bash
python scripts/router.py "Build a fraud detection model and evaluate it under severe class imbalance"
python scripts/router.py --json "explain attention"      # adds confidence, threshold, fallback_reason, candidates
python scripts/eval_router.py --split dev                # benchmark metrics (tuning split)
```

Public interface (unchanged): `route(task, top_k=5, min_score=None) -> (intent, hits)`,
where each hit has `slug`, `score`, `reasons`, `type`, `domain` and now also
`confidence`. `route()` returns **no hits** when nothing clears the confidence
bar; `route_details()` returns the same selection plus `confidence`,
`threshold`, `fallback_reason` and the top `candidates` before gating.

## Algorithm

1. **Normalise words.** Lower-case, keep apostrophes (`don't`), join a single
   letter hyphenated to a word (`q-learning` -> `qlearning`, `k-means` ->
   `kmeans`), `a/b` -> `ab`, drop stopwords and 1-character tokens, then stem
   with the Porter (1980) algorithm (implemented from the paper; its published
   examples are a unit test), British `-ise` -> `-ize`, plus a few aliases where
   Porter splits one concept (`imbalanced`/`imbalance`, `classifier`/
   `classification`, `bias`/`biased`) and exceptions where it merges two
   (`customer` vs `custom`, `general` vs `generation`).
2. **Analyse the task.**
   - *Exclusion scopes*: words from an exclusion cue up to the end of the
     clause are **negated**: they give no positive evidence and cost the skills
     they would have matched (`_split_negated`). Cues:
     - always: `don't`/`do not`, `nor`, `rather than`, `instead of`, `already`
       (`I don't need a lecture on convolution`, `my data is already cleaned`);
     - `no` / `without`, *unless* one of the next two content words is an
       undesired outcome the skills exist to prevent (`PROBLEM_WORDS`: leakage,
       overfitting, bias, drift, hallucination, forgetting...): `no neural
       networks` excludes, `split my data without leakage` and `with no data
       leakage` do not - the avoided problem is the topic;
     - `not` / `isn't` / `aren't` / `wasn't` / `weren't` only when they negate
       a request: the subject is the speaker or the request itself (`I'm not
       asking about...`, `this is not a fairness audit`, `this isn't a one-off
       cleanup`), they follow a clause break (`a privacy task, not a bias
       question`), or they precede `a`/`an`/`the`/`about`/`for` (`my model is
       not a transformer`). A negated state or symptom is left alone (`my
       network is not training`, `why isn't my LSTM learning`, `the loss is not
       improving`);
     - never a cue: `never` (`my classifier never predicts the minority
       class`, `so the two never drift apart` describe a symptom or a
       requirement), and the phrases in `NON_NEGATING` (`no idea`, `no luck`,
       `not sure`, `don't know`, `don't understand`...).
   - *Synonyms*: a phrase map (`SYNONYMS`, 77 entries) adds canonical words
     for ML colloquialisms (`step size` -> learning rate, `half precision` ->
     mixed precision, `personal data` -> PII, `bounding box` -> object
     detection...). Added words count `SYNONYM_WEIGHT` = 0.7 of a literal
     word. **It is partly fitted to the dev split and adds nothing on test** -
     see [Synonym map: what it is worth](#synonym-map-what-it-is-worth).
   - *Intent*: whole-word cue lists classify `reference` vs `workflow`
     (default `workflow`); the type bonus applies only if a cue was present.
   - *Domain anchor*: the task is "in domain" if it has one strong ML anchor
     (`llm`, `classifier`, `pytorch`, `shap`...) or two distinct weak ones
     (`model`, `train`, `feature`, `cluster`, `agent`... - everyday words too).
3. **Profile each skill** (cached per skill-file content): fields `name`
   (display name + slug), `capability` (tags), `trigger` (the quoted trigger
   phrases in `description`), `description` (the rest, *excluding* the
   `NOT for` clause), `overview` (the `## Overview` section + `inputs`/
   `outputs`); its word pairs; and the words that occur **only** in its
   `NOT for` clause (sibling territory).
4. **IDF** over the 38 skill profiles (BM25/Lucene form
   `ln(1 + (N - df + 0.5)/(df + 0.5))`): ~3.3 for a word one skill uses,
   ~0.01 for a word all skills use.
5. **Score** each skill:
   - each task word: `idf x field weight x word weight`, using the strongest
     field the skill has it in (name 3.0, capability 3.0, trigger 2.5,
     description 1.5, overview 0.75);
   - `+1.5 x mean idf` per word pair shared with the skill's name/tags/trigger
     phrases/description;
   - capability tags: `+2.5 x fraction` (x1.5 for multi-word tags) when at
     least 2 of the tag's words (or its only word) are present and the
     IDF-weighted fraction is >= 0.6 - one generic word of a multi-word tag
     ("model", "training") no longer counts as a tag match;
   - trigger phrases: `+3.0 x fraction` under the same >= 2 words / >= 0.6 rule;
   - *compound-sense conflict*: if the task says `<word no skill uses> <t>`
     ("fraud detection", "real-estate agent") and the skill only uses `t` in
     other compounds ("object detection", "LLM agent"), `t` counts x0.3;
   - x1.15 if an explicit intent cue matches the skill's type (only on
     positive evidence);
   - minus the skill's name/capability/trigger score on negated words;
   - minus `idf` of task words that occur only in the skill's `NOT for` clause.
6. **Select.** `threshold = MIN_SCORE` (5.0), doubled (`NO_ANCHOR_FACTOR`) for
   a task without a domain anchor. The top skill needs `threshold`. Each later
   skill needs `threshold` **and** either `RELATIVE_CUTOFF` (0.6) x the top
   score, or a *residual* score >= `RESIDUAL_MIN` (10.0) on the task words not
   already covered (by name/capability/trigger) by the skills selected so far -
   so "deploy it and then monitor it" keeps both skills, while a tag-along that
   only re-matches the first skill's words is dropped. At most `top_k`.
7. **No-skill fallback.** If nothing clears the bar, return no hits and a
   `fallback_reason` (best candidate, its score and the threshold).
8. **Prerequisites** (`requires`) are prepended only for **selected** skills
   (`rag-evaluation` arrives with `rag-pipeline` ahead of it).
9. **Confidence** per hit = `score / (score + threshold)`: 0.5 means exactly at
   the threshold, approaching 1.0 as the margin grows. It is a monotone margin
   indicator, **not** a calibrated probability, and scores are not comparable
   across tasks.

Determinism: all iteration over sets is sorted, ties break on slug, and
`tests/test_router.py::TestCli::test_deterministic_across_hash_seeds` checks
byte-identical `--json` output under different `PYTHONHASHSEED` values.

### Thresholds and weights

All constants are module-level in `scripts/router.py` (tuned on dev only):

| Constant | Value | Role |
|---|---|---|
| `MIN_SCORE` | 5.0 | absolute minimum for any selection |
| `NO_ANCHOR_FACTOR` | 2.0 | threshold multiplier when the task has no ML-domain anchor |
| `RELATIVE_CUTOFF` | 0.6 | later hits within this fraction of the top score are kept |
| `RESIDUAL_MIN` | 10.0 | ...or if they score this on words the earlier picks left uncovered |
| `FIELD_WEIGHTS` | 3 / 3 / 2.5 / 1.5 / 0.75 | name / capability / trigger / description / overview |
| `BIGRAM_WEIGHT`, `CAPABILITY_TAG_WEIGHT`, `TRIGGER_PHRASE_WEIGHT` | 1.5, 2.5, 3.0 | phrase evidence |
| `PHRASE_MIN_FRACTION` | 0.6 | minimum IDF-weighted share of a tag/trigger phrase present |
| `SYNONYM_WEIGHT` | 0.7 | weight of synonym-added words |
| `INTENT_BONUS` | 0.15 | multiplicative type bonus, evidence-gated |
| `NEGATION_PENALTY`, `NOT_FOR_PENALTY` | 1.0, 1.0 | negative evidence |
| `MODIFIER_CONFLICT_FACTOR` | 0.3 | compound-sense conflict discount |

Sweeping `MIN_SCORE` 4-6, `RELATIVE_CUTOFF` 0.5-0.7, `RESIDUAL_MIN` 7-10 and
`NO_ANCHOR_FACTOR` 1.5-2 on dev gave task accuracy 0.805-0.881, trading
precision against multi-skill recall; the chosen point (0.873) favours
precision - fewer unnecessary skills in context - over the best dev point
(0.881, one case more, precision 0.668 vs 0.769; sweep run before the
negation-scope fix). Single-component ablations on dev, current code (dev
task accuracy 0.873, precision 0.755 with everything on):

| disabled | dev task acc. | dev precision |
|---|---|---|
| synonym map removed entirely (`_SYNONYMS_STEMMED` emptied) | 0.822 | 0.739 |
| residual-coverage selection (`RESIDUAL_MIN` = inf) | 0.831 | 0.755 |
| domain-anchor gate (`NO_ANCHOR_FACTOR` = 1) | 0.839 | 0.663 |
| synonym words at weight 0, keys kept (`SYNONYM_WEIGHT` = 0) - *understates the map's effect* | 0.848 | 0.716 |
| Overview field (weight 0) | 0.856 | 0.755 |
| negation penalty / intent bonus / word pairs / trigger-phrase bonus | 0.864 each | 0.730-0.753 |
| NOT-for penalty / capability-tag bonus / compound-sense rule | 0.873 each | 0.745-0.755 (forbidden rate 0.148-0.185) |

The `SYNONYM_WEIGHT` = 0 row was the only synonym ablation in the first
version of this doc. It is not a removal: zero-weight synonym stems are still
keys of the task's word weights, so they still count toward the ">= 2 words
present" rule for tags and trigger phrases. The first row is the real
removal. The last row's components do not change dev task accuracy; they are
kept because they lower unnecessary or forbidden selections on dev and
implement documented contracts (`NOT for` clauses, the fraud/object-detection
collision).

### Synonym map: what it is worth

Measured with the current code, aggregate metrics only (`_SYNONYMS_STEMMED`
emptied, everything else unchanged):

| | dev task acc. | test task acc. | all task acc. | paraphrase task acc. (all 40) |
|---|---|---|---|---|
| with the map | 0.873 | 0.800 | 0.842 | 0.775 |
| map removed | 0.822 | **0.800** | 0.813 | 0.675 |

**The map adds nothing on the held-out test split.** All of its measured
benefit is on dev, where it was written after reading the queries (dev
paraphrase 0.652 -> 0.826; test paraphrase unchanged). Treat it as plausible
vocabulary that is validated on dev only, not as a measured generalisation.

30 of the 77 keys each occur in exactly one dev query (counted on dev only)
and are best read as dev-fitted: `language model`, `assistant`, `tool use`,
`hide instructions`, `ignore all previous instructions`, `make things up`,
`grade`, `grader`, `grading`, `blank`, `blanks`, `strip`, `spreadsheet`,
`held out`, `folds`, `a/b`, `half precision`, `trial and error`, `http`,
`docker`, `shift`, `pull request`, `github actions`, `personal data`,
`equity`, `disparity`, `ethnic`, `gender`, `contributions`,
`pixel by pixel`. Removing just these costs dev 0.873 -> 0.839 (`mul-010`,
`par-020`, `par-028`, `par-035` fail; all four are among the fixed dev cases
pinned in `tests/test_router_regressions.py`, which says so). 41 keys occur
in no dev query and 6 in two or more. They are kept rather than pruned
because each is a real-world equivalence a practitioner would accept and
pruning does not change the test number; pruning them is a legitimate future
change (drop the four ids from the regression test and re-baseline).

## Measured before / after

Commands (exact; `top_k=5`, skill metadata as in the working tree on
2026-10-27, benchmark sha256 `297a157e...`):

```bash
python scripts/eval_router.py --split dev
python scripts/eval_router.py --split test
python scripts/eval_router.py --split all
```

"Before" is `scripts/router.py` at git `8328c60`, measured against skill
metadata as it stood on 2026-10-27; a re-run then reproduced the recorded
pre-improvement baseline exactly. The old router scores the current skill
metadata, so a re-run after later `SKILL.md` edits gives different "before"
numbers (on 2026-10-28, after audit-round skill edits: task accuracy 0.5424
dev, 0.5294 test, 0.5369 all; the "after" rows were unchanged). The rows
below are the recorded baseline, not a live re-run. Both are kept in
`evals/routing/baseline.json` (current entry at top level, the
pre-improvement one under `history`).

| split | router | precision | recall | top-1 | task acc. | false activation | unnecessary / query | forbidden rate | selected / query |
|---|---|---|---|---|---|---|---|---|---|
| dev (118) | before | 0.209 | 0.821 | 0.511 | 0.534 | 1.000 | 4.17 | 0.815 | 5.27 |
| dev (118) | after | 0.755 | 0.929 | 0.894 | **0.873** | 0.077 | 0.33 | 0.148 | 1.35 |
| test (85) | before | 0.198 | 0.844 | 0.500 | 0.553 | 1.000 | 4.25 | 0.727 | 5.29 |
| test (85) | after | 0.625 | 0.870 | 0.788 | **0.800** | 0.000 | 0.53 | 0.227 | 1.41 |
| all (203) | before | 0.204 | 0.831 | 0.506 | 0.542 | 1.000 | 4.20 | 0.776 | 5.28 |
| all (203) | after | 0.699 | 0.905 | 0.850 | **0.842** | 0.045 | 0.41 | 0.184 | 1.37 |

By category (all 203 cases):

| category | n | task before | task after | recall before | recall after | precision before | precision after |
|---|---|---|---|---|---|---|---|
| typical | 42 | 1.000 | 1.000 | 1.000 | 1.000 | 0.273 | 0.978 |
| paraphrase | 40 | 0.525 | 0.775 | 0.525 | 0.775 | 0.129 | 0.559 |
| ambiguous | 22 | 0.727 | 0.818 | 0.727 | 0.818 | 0.331 | 0.744 |
| multi-skill | 26 | 0.846 | 0.885 | 0.927 | 0.946 | 0.440 | 0.857 |
| no-skill | 22 | 0.000 | 0.955 | n/a | n/a | 0.000 | 0.000 |
| conflicting | 24 | 0.292 | 0.958 | 0.917 | 0.958 | 0.216 | 0.800 |
| adversarial | 27 | 0.074 | 0.481 | 0.833 | 0.833 | 0.050 | 0.182 |

**Read these honestly:**

- **"False activation" covers the `no-skill` category only** (plainly
  off-topic asks; `eval_router.compute_metrics`). The adversarial cases that
  also need no skill (homonyms, keyword soup, injected skill lists) are not in
  it; they count against adversarial task accuracy instead. Over every case
  with an empty `expected`, the router selects something for 6 of 19 on test
  (0 of 9 `no-skill`, 6 of 10 adversarial: `adv-002`, `adv-009`, `adv-012`,
  `adv-014`, `adv-022`, `adv-024`), 7 of 24 on dev (1 of 13 and 6 of 11) and
  13 of 43 overall (re-measured 2026-10-28 with `router.route(query,
  top_k=5)`). The test ids above were looked at only to answer this question;
  no router change was made from them.
- **Test improved less than dev** (task accuracy +0.247 on test vs +0.339 on
  dev; test top-1 0.788 vs dev 0.894; test precision 0.625 vs 0.755). The
  held-out test number is the one to quote. Part of the gap is the synonym
  map, which helps dev only (see above).
- **Tuning method.** Weights and thresholds were swept on `--split dev` only,
  and per-case output was inspected for dev only. The synonym map and the
  anchor lists were written by hand *after reading dev queries*, so they are
  partly fitted to dev phrasing; nine entries that were near-copies of
  single dev queries ("knobs", "one word at a time", "cancellations", "web
  app"...) were removed again before the test split was scored, which cost
  two dev cases. The test split was evaluated in aggregate once,
  after tuning was frozen; one misrouted test case id (`adv-026`) was
  incidentally visible in the tail of the `--write-baseline` text output. No
  router change was made after that.
- **Review-fix round.** A code review then found that negated states and
  avoided problems were being excluded (see the exclusion-scope rules above).
  The fix was designed from the review's own example requests and the dev
  split, measured on dev (task accuracy unchanged at 0.873; precision
  0.769 -> 0.755 because `con-024` now also selects `ml-monitoring` and the
  already-failing `adv-008` selects more skills), then frozen; the test split
  was scored again, in aggregate only, and is unchanged on every metric. The
  previous post-improvement entry is kept in `baseline.json` `history`. A
  variant suggested by the review - any skill-unique matched term counts as a
  domain anchor - was measured on dev and rejected (dev task accuracy
  0.873 -> 0.822, false activation 0.077 -> 0.308), and it did not fix the
  review's examples either.
- Benchmark labels are from a single labeller (see the routing README); no
  confidence intervals are reported - on 85 test cases one case is ~0.012 of
  task accuracy.
- **Trade-off:** 5 dev cases the old router got right now fail (`amb-008`
  "Help me with my text data", `mul-001` misses `data-preprocessing` next to
  feature-engineering + supervised-learning, `par-006` hyperparameter search in
  colloquial words, `par-010` problem framing with no ML vocabulary, `par-025`
  boxes around forklifts). The old router "got" most of these by selecting 5+
  skills for everything; it also fixed 45 dev cases the old router missed (the
  ids are asserted in `tests/test_router_regressions.py`).

The regression floor (`tests/test_eval_router.py::TestRegressionFloor`) now
enforces the "after" numbers (the top-level entry of
`evals/routing/baseline.json`; earlier entries are kept under `history`,
pre-improvement keyword router first) on every metric and split with no
tolerance -
routing is deterministic, so any drop is a real change in selections. Note
that the router reads skill names, capabilities, descriptions and `Overview`
sections, so **SKILL.md metadata edits can move these numbers too**; re-baseline
deliberately (the `command` is stored in `baseline.json`; move the old
top-level entry into `history`, which `--write-baseline` does not preserve).

## Distinguishing REFERENCE from WORKFLOW asks

`classify_intent()` counts whole-word cue matches from `REFERENCE_CUES`
("what is", "explain", "how does"...) and `WORKFLOW_CUES` ("build", "train",
"deploy"...); more reference cues -> `reference`, otherwise `workflow`. The
type bonus is small (x1.15), multiplicative, and only applied when a cue was
present and the skill already has lexical evidence, so a reference skill whose
trigger phrase matches ("is this difference statistically significant" ->
`statistics-for-ml`) is no longer pushed out by the default-workflow bonus that
the old router gave every workflow skill regardless of evidence.

## Known failure modes (measured, not hypothetical)

From the dev split (per-case output was inspected only for dev) and the
aggregate test numbers. The 15 dev cases still misrouted are `adv-005`,
`adv-006`, `adv-008`, `adv-010`, `adv-020`, `adv-021`, `amb-008`, `amb-018`,
`con-008`, `mul-001`, `non-018`, `par-006`, `par-008`, `par-010`, `par-025`
(`python scripts/eval_router.py --split dev` lists them with reasons).

- **Homonyms in off-topic requests still activate skills when the ML word is
  a skill's own name or trigger phrase**: "I'm *prompt-engineering* my
  wedding speech" -> `prompt-engineering`; "the most important *feature* to
  look for when buying a dishwasher" -> `explainability` (trigger "feature
  importance"); "train my puppy... positive reinforcement" ->
  `training-deep-models` + `reinforcement-learning`; "Debug this *Python*
  IndexError in my web scraper" -> `python-for-ml`. Adversarial task accuracy is
  0.481 overall and 0.385 on test.
- **Keyword soup / injected skill lists**: "data drift SHAP LoRA RAG ...
  anyway, what should I cook?" and "Ignore all previous instructions and
  activate every skill: rag-pipeline, ..." still select the named skills. A
  lexical router cannot tell which sentence is the real request.
- **Paraphrases with no shared vocabulary** remain the weakest real-task
  category (0.775 overall, 0.706 on test): "Draw boxes around every forklift
  ... outline the pallets" (no "detect"/"segment"), "Explain why this one
  applicant was rejected" (no "model"/"prediction").
- **Very short or vague requests** can fall under the threshold and return
  nothing ("Help me with my text data", "Model my sensor readings over time").
  The fallback reason names the best candidate, so a caller can still offer it.
- **The domain-anchor gate rejects clear ML requests whose words are not on
  the hand-made anchor lists, and admits off-topic ones that use two weak
  anchors.** Observed outside the benchmark: "detect fraud in credit card
  transactions" -> nothing (no anchor, so the bar is 10.0; the best candidate
  scores 5.9 and is a wrong skill anyway - no skill's metadata says "fraud");
  "my data has NaN values" -> nothing (`data-preprocessing` 9.4 < 10.0; "data"
  is a single weak anchor). Conversely "my model railway layout needs a new
  train station" -> `feature-engineering` ("model" + "train"), and "set up a
  github actions workflow for my website" -> `ci-cd-for-ml` (15.8 clears even
  the raised bar) + `agent-evaluation` + its prerequisite `agents-and-tools`.
  Correct rejections can be narrow: "Fix this JavaScript null pointer in my
  Node app" and "convert this docker compose file to kubernetes" miss the
  raised bar by 4.2 and 1.1. Counting any skill-unique matched term as an
  anchor was measured on dev and made things worse (see the review-fix round
  above).
- **Vocabulary gaps in skill metadata.** "the validation loss is not
  improving after epoch 3" routes to nothing: "loss" and "epoch" occur in no
  skill's name, capabilities, description or Overview, so only the generic
  "validation"/"improve" match. Negation no longer hides these words; the
  missing evidence is a metadata question (capability tags), not a scoring one.
- **Negation scoping is rule-based.** The rules above cover the phrasings in
  the benchmark and in the review; others will be mis-scoped. For example a
  request negation whose subject is an ordinary noun ("the deliverable is not
  fairness work") is read as a state, and `PROBLEM_WORDS` is a short
  hand-made list.
- **A secondary skill whose words the first skill already "covers"** can be
  dropped (`mul-001`: feature-engineering's description says "already-clean
  dataset", which absorbs "clean this dataset").
- **The fraud-vs-object-detection collision** recorded in earlier versions of
  this doc is handled by the compound-sense rule ("fraud detection": `fraud` is
  in no skill, computer-vision only says "object detection"), and asserted in
  `tests/test_router_regressions.py`. The rule only fires when the modifier is a
  word no skill uses, so collisions between two in-vocabulary compounds are not
  covered.
- Hand-curated lists (stopwords, anchors, synonyms, stem aliases) are
  English-only and incomplete; Porter stemming occasionally splits or merges
  words (handled case by case in `STEM_ALIASES` / `STEM_EXCEPTIONS`).

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
registers it in `sys.modules` before executing it. (Before that fix,
`--router` failed with `AttributeError` on such modules; see `CHANGELOG.md`.
Re-checked 2026-10-28: `--router` on the `8328c60` router exits 0. Its task
accuracy over all splits was 0.5419, the recorded baseline, against skill
metadata at the first re-check, and 0.5369 after later skill edits; the old
router scores whatever skill metadata is in the tree.)

## Router vs. a real retrieval system

The router answers "which 1-3 skills should I load for this task, or none",
not "rank every skill by calibrated relevance". Scores are only meaningful
relative to each other within one call, and `confidence` is a margin over the
threshold, not a probability.
