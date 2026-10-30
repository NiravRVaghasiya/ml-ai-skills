# Labelled grading sets (`evals/_grading/`)

Labelled answers used to measure the **graders** (the regex hard assertions, an optional model
judge, and the combined verdict), not the skills. `scripts/run_evals.py --grade-labelled` scores a
grader against one of these sets and reports TPR / TNR / FPR / FNR with Wilson 95% intervals, per
skill, per `label_source` and in aggregate. See `docs/EVALUATION.md` for the grading layers.

This directory is not a skill: `validate_evals.py` ignores it, and it must never contain a
`*.yaml` case or a `plan.json`.

## Partitions

| directory | what it is for | allowed `label_source` |
|---|---|---|
| `dev/` | extra development answers. The `evals/<slug>/` fixtures are dev data too (`--grade-labelled dev-fixtures`); they stay where they are | `human`, `agent-adjudicated`, `fixture-author` |
| `calibration/` | answers for tuning or choosing a judge (prompt, model, thresholds). Once a judge has been tuned on them, they are no longer held out | `human`, `agent-adjudicated` |
| `heldout/` | answers written without seeing the regexes, used only to report grader accuracy. Never tune anything against them | `human`, `agent-adjudicated` |

Rules (`run_evals.py` enforces the mechanical ones, and `tests/test_eval_grading.py` checks the
shipped files):

- The `evals/<slug>/` fixtures were used to write and tune the patterns. They are **dev data** and
  must never be copied or moved into `calibration/` or `heldout/`. An answer whose normalized text
  equals a fixture is excluded from calibration and held-out scoring and counted as `dev_overlap`.
- `fixture-author` labels are dev labels by definition, so they are rejected in `calibration/` and
  `heldout/`.
- Every item in `<partition>/manifest.jsonl` must have `"partition": "<partition>"`.
- A held-out item that someone has looked at while editing a pattern or judge is no longer held
  out. Move it to `dev/` (or `calibration/`) and say so in `notes`; do not keep scoring it as
  held out.

## Manifest format (`manifest.jsonl`)

One JSON object per line (UTF-8, LF). Unknown fields are errors.

| field | required | meaning |
|---|---|---|
| `item_id` | yes | unique within the manifest |
| `case_id` | yes | `<skill>/<case id>` of an existing case in `evals/` |
| `skill` | yes | the skill slug; must prefix `case_id` |
| `partition` | yes | `dev`, `calibration` or `heldout`; must match the directory |
| `answer_text` / `answer_file` | exactly one | the answer inline, or a relative POSIX path to a UTF-8 file inside this manifest's directory (no `..`, no absolute paths) |
| `intended_label` | yes | `pass` or `fail`: what the author meant to write (a correct or a wrong answer) |
| `adjudicated_labels` | for `agent-adjudicated` | list of `{"labeler": str, "label": "pass" \| "fail" \| "unresolved", ...}`; extra keys such as `rationale` are kept |
| `final_label` | yes | `pass`, `fail` or `unresolved`. Scoring uses this label; `unresolved` items are excluded and counted |
| `label_source` | yes | `human` (a person reviewed it), `agent-adjudicated` (model labelers, not people), or `fixture-author` (the pattern author; dev only) |
| `notes` | no | free text; say how a disagreement was resolved |

Consistency checks (errors unless stated):

- A `final_label` that contradicts unanimous `adjudicated_labels` is an error in every partition.
- `agent-adjudicated` items in `calibration/` or `heldout/` need at least two `adjudicated_labels`
  from distinct labelers (names compared case-insensitively). Their `final_label` must be the
  strict majority of those labels. When there is no strict majority (for example `pass` vs
  `fail`, or `pass` vs `unresolved`), it must be `unresolved`, so the item is excluded and counted.
- A `human` final label may overrule split model labels; say how in `notes`. In `dev/`, and for
  `human` items, a resolved `final_label` over split labels without `notes` is a warning.

Example line:

```json
{"item_id": "dp-leak-001", "case_id": "data-preprocessing/leakage", "skill": "data-preprocessing", "partition": "heldout", "answer_text": "...", "intended_label": "fail", "adjudicated_labels": [{"labeler": "labeler-a", "label": "fail", "rationale": "..."}, {"labeler": "labeler-b", "label": "fail"}], "final_label": "fail", "label_source": "agent-adjudicated", "notes": ""}
```

## Commands

```bash
python scripts/run_evals.py --grade-heldout --out heldout-grading.json     # evals/_grading/heldout/manifest.jsonl
python scripts/run_evals.py --grade-labelled calibration
python scripts/run_evals.py --grade-labelled dev-fixtures                   # the evals/<slug>/ fixtures (100% by construction)
python scripts/run_evals.py --grade-labelled path/to/manifest.jsonl --skill data-preprocessing
```

Exit 0 when the set was scored or is empty/missing (status `unmeasured`), 1 when the manifest has
errors. Positive class = `final_label: pass`. FPR is the share of wrong answers a grader passes.

## Status

| manifest | items | status |
|---|---|---|
| `heldout/manifest.jsonl` | 249 (83 cases x 3 answers) | measured, regex layer only |
| `calibration/manifest.jsonl` | 0 | empty: no judge has been calibrated |
| `dev/manifest.jsonl` | 0 | empty (the `evals/<slug>/` fixtures are the dev data) |

The held-out answers were written on 2026-10-29 by 6 blind model writers that saw only each
case's input and rubric (`expected_behavior`, `must_not`), not the regexes, the skills or the
repository; their tool-use transcripts show no repository file was read. For each case there
is one correct or borderline-correct answer (`intended_label: pass`), one confidently
incorrect and one partially correct answer (`fail`); each item's `notes` say which. Every
item has two model labelers (`agent-adjudicated`), and both agreed with the intended label on
all 249, so none is `unresolved`. The patterns were tuned before the set was written, by
someone who never saw it.

`python scripts/run_evals.py --grade-heldout`, re-run 2026-10-30 (Wilson 95%):

| regex layer | k/n | rate | 95% interval |
|---|---|---|---|
| TPR: correct answers passed | 71/83 | 0.855 | [0.764, 0.915] |
| FNR: correct answers failed | 12/83 | 0.145 | [0.085, 0.236] |
| TNR: wrong answers failed | 152/166 | 0.916 | [0.863, 0.949] |
| FPR: wrong answers passed | 14/166 | 0.084 | [0.051, 0.137] |

All 14 false passes are partially correct answers; every confidently incorrect answer failed.
Because the labelers never disagreed, the wrong answers are probably clearer than real model
mistakes, so this set likely overstates how well the regexes catch wrong answers. The
intervals treat the 249 answers as independent, which they are not. Agreement here is
agreement with model labelers, not people (`status.against_humans`: not measured). Any judge
is **uncalibrated against humans** until a `human`-labelled sample exists, and against
anything at all until `calibration/` has items. The one model run so far (2026-10-30) is reported in
`docs/EVALUATION.md` and `docs/KNOWN-ISSUES.md` #1; its opus judge was not measured against
this set or against people.
