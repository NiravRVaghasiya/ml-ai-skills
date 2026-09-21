# Evaluation case format

`evals/<skill-slug>/<case-name>.yaml` — one behavioral test case per file.

This is **not** a benchmark harness that runs a model and produces a score.
No such runner exists in this repo, and none of the numbers you might expect
from one (accuracy, pass-rate, etc.) are fabricated here — see
`docs/SKILL-SPEC.md` "What evals/ is and is not". What exists today is:

1. A structured, machine-checkable case format (this schema).
2. `scripts/validate_evals.py`, which checks every case file is well-formed
   and points at a real skill — a **sanity** check, not a behavioral one.
3. Human- or agent-graded review: a case is meant to be handed to an agent
   (with the target skill loaded) and its transcript compared against
   `expected_behavior` / `must_not` by a reviewer. That grading step is
   intentionally NOT automated here — automating it convincingly requires an
   LLM-as-judge setup with its own evaluation (see `llm-evaluation` and
   `rag-evaluation` skills for the failure modes of that approach), and
   building that judge is future work (see docs/SKILL-SPEC.md "Known
   limitations").

## Fields

```yaml
skill: data-preprocessing        # required — must be a real slug (folder name)
category: misconception          # required — one of the 6 below
input: |                         # required — literal block scalar, the user message
  I have train and test data. Can I normalize the whole dataset before splitting?
expected_behavior:               # required — non-empty list of concepts/actions a
  - identifies leakage risk      # correct response must contain. Grade on CONCEPT
  - explains why fitting on combined data leaks information from test into train
  - recommends fitting transformations on the training split only
  - shows or references a Pipeline/ColumnTransformer-based safe approach
  # presence, not exact wording — do not grade on string match.
must_not:                        # optional — list of things a correct response
  - claim it is always safe to normalize before splitting  # must NOT do
notes: >                         # optional — why this case exists / what it guards
  Guards against the single most common leakage mistake; see
  data-preprocessing/SKILL.md Gotchas.
```

## Categories

| category          | tests whether the agent...                                             |
|-------------------|--------------------------------------------------------------------------|
| `happy_path`      | correctly executes the intended workflow with no complications           |
| `edge_case`       | handles a realistic but non-default situation (small data, high cardinality, missing values, class imbalance, ...) |
| `adversarial`     | resists a request that is actively trying to produce an unsafe/incorrect/leaking/injected result |
| `misconception`   | corrects a plausible-sounding but wrong claim in the user's message instead of going along with it |
| `failure_recovery`| notices its own action failed/was wrong and recovers instead of looping or confidently continuing |
| `ambiguity`       | asks a clarifying question or states an assumption instead of silently guessing when the request is underspecified |

## Grading contract

- Grade on whether every `expected_behavior` item is *substantively* present
  (a paraphrase counts), and whether any `must_not` item is present (any
  form of it is a fail, even hedged).
- A response can say more than what's listed — extra correct detail is not
  a failure.
- A response that hedges appropriately ("this depends on X") is not
  automatically a failure of `happy_path` cases; only mark it down if the
  hedge avoids answering the actual question.
