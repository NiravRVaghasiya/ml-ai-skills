# Routing algorithm

`scripts/router.py` maps a free-text task description to an ordered list of
skills, without loading every `SKILL.md` into context. It is deliberately the
simplest thing that could plausibly work — a scored keyword/tag match, no
embeddings, no external service — per the "don't overengineer" principle. This
doc explains the algorithm, states its actual measured failure mode, and says
what you'd need to build instead if that failure mode becomes a real problem.

## Algorithm

1. **Classify intent** — `reference` or `workflow` — by counting cue-phrase hits
   from two fixed lists (`REFERENCE_CUES` like "what is", "explain", "how does";
   `WORKFLOW_CUES` like "build", "train", "evaluate", "deploy"). Ties, and the
   no-cue-at-all case, default to `workflow` (a bare topic name is more often
   about to be *acted on* than asked about in the abstract).
2. **Score every skill** against the task's tokenized words:
   - `+3` per `capabilities` tag that overlaps the task text — the strongest
     signal, because capability tags are hand-curated to be specific.
   - `+2` per word overlap with the skill's `display_name`/slug.
   - `+1` per word overlap with the skill's `description`.
   - `+2` if the skill's `type` matches the classified intent, `-1` if it doesn't
     (a soft bonus/penalty, not a hard filter — a `workflow` task can still
     legitimately need a `reference` skill loaded alongside it).
3. **Take the top-k** by score (ties broken alphabetically for determinism).
4. **Expand with hard prerequisites** — for every selected skill, recursively
   prepend anything in its `requires` list that wasn't already selected, so a
   route like `rag-evaluation` always arrives with `rag-pipeline` ahead of it.

## Distinguishing REFERENCE from WORKFLOW asks

This is exactly the cue-phrase step above. "What is calibration?" scores 1
reference cue, 0 workflow cues → `reference`. "Calibrate this classifier and
compare calibration curves" scores 0 reference cues, 1 workflow cue (an implicit
imperative) → `workflow`. See `tests/test_router.py::TestClassifyIntent` for the
exact assertions this is checked against.

## Known failure mode (found during this repo's own testing, not hypothetical)

Running the mission's own worked example:

```
$ python scripts/router.py "Build a fraud detection model and evaluate it under severe class imbalance"
```

returned `computer-vision` as the #1 hit, ahead of `model-evaluation`, purely
because "detection" appears in both "fraud detection" and computer-vision's
"object detection" capability tag — a vocabulary collision between two unrelated
domains that share a common English word. `model-evaluation` still placed in
the top 5 (via description overlap on "build"/"model"), but a naive top-1
consumer would get the wrong skill first.

**Mitigation applied:** none automated — this is a real, demonstrated limitation
of a pure keyword matcher, not a bug to "fix" by special-casing "detection".
Recorded here per the "make uncertainty explicit" principle instead of hiding
it. The `capabilities` tags added during the Phase-15 audit pass are more
specific than bare description-word overlap (e.g. `fraud-detection` and
`imbalanced-classification` as distinct tags rather than relying on the word
"detection" alone), which reduces but does not eliminate this class of
collision — verify current top-5 output for this exact query before trusting it
blindly (`python scripts/router.py "Build a fraud detection model and evaluate
it under severe class imbalance"`).

**What would actually fix this:** an embedding-based (semantic) similarity layer
as a second ranking pass over the keyword-matched candidate set, so "fraud
detection" and "object detection" don't collide just because they share a
token. Not implemented here — it would add a model-inference dependency to what
is currently a zero-dependency, offline script, and the mission explicitly
warns against introducing infrastructure before a demonstrated need beyond one
observed case. If routing accuracy on real usage turns out to need it, that's
the concrete next step (see docs/SKILL-SPEC.md "Known limitations").

## Router vs. a real retrieval system

The router answers "which 3–5 skills should I load for this task", not "rank
every skill by relevance with calibrated scores" — scores are only meaningful
relative to each other within one call, not comparable across calls, and are
not probabilities.
