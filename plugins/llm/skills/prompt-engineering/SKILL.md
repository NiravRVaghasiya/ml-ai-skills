---
name: prompt-engineering
display_name: Prompt Engineering
description: >
  Use when the user wants to design, structure, or debug prompts for an LLM —
  choosing zero-shot vs few-shot framing, getting reliable structured/JSON
  output, or improving reasoning quality without touching model weights or
  retrieval. Trigger phrases: "write a better prompt", "few-shot examples",
  "get JSON output from the model", "why is the model ignoring my
  instructions", "chain-of-thought prompt", "system prompt design". NOT for
  grounding answers in external documents (see rag-pipeline), changing model
  weights (see fine-tuning-llms), or building multi-step tool-calling loops
  (see agents-and-tools).
type: reference
domain: llm
level: beginner
lifecycle: stable
risk_level: low
evidence_level: established-practice
last_verified: 2026-10-28
capabilities:
  - zero-shot-prompting
  - few-shot-prompting
  - chain-of-thought-prompting
  - structured-output-prompting
  - prompt-template-design
requires:
conflicts:
related:
  - rag-pipeline
  - agents-and-tools
  - fine-tuning-llms
  - llm-evaluation
  - ai-ml-security
  - attention-mechanisms
inputs: A task description and/or a draft prompt (with or without examples) intended for a single LLM call.
outputs: A restructured prompt (zero/few-shot framing, CoT scaffolding, or output-format constraints) plus a diagnosis of why an existing prompt is unreliable.
---

## Overview
Prompt engineering is the practice of shaping the *input* to an LLM — instructions,
examples, formatting, and constraints — to reliably steer its output, without any
retrieval augmentation or weight changes. This card covers the core patterns
(zero/few-shot, chain-of-thought, structured output) and the failure modes that
make prompts brittle. Walk away knowing which pattern to reach for and why a
prompt that works today may silently break on a slightly different input.

## Key Concepts
- **Zero-shot vs. few-shot.** Zero-shot relies purely on the instruction; few-shot
  adds a handful of input/output examples inside the prompt (a few is a common
  starting point; how many help depends on the task, the model, and the context
  budget) to demonstrate the exact format and reasoning style you want. Few-shot
  examples act as an implicit spec — models often pattern-match to them more
  strongly than to prose instructions, so when the two disagree, expect the
  examples to win.
- **Role separation (system vs. user vs. assistant).** The system/developer prompt
  sets durable behavior (persona, constraints, output contract); the user turn
  carries the task; prior assistant turns carry conversational memory. Mixing task
  data into the system prompt makes behavior harder to override per-request.
- **Chain-of-thought (CoT).** Prompting the model to reason step by step before
  giving a final answer improved multi-step reasoning and arithmetic benchmarks
  in the original studies — but Wei et al. found the gain emerges only at large
  model scale (smaller models can get *worse*), and models with built-in
  reasoning/"extended thinking" modes already reason before answering, so
  explicit CoT instructions there may add little or conflict with the
  provider's guidance — measure on your task rather than assuming a gain.
  Variants: zero-shot CoT (just ask for reasoning, e.g. "Let's think step by
  step", Kojima et al. 2022), few-shot CoT (show worked examples), and
  self-consistency (sample multiple CoT paths, take a majority vote on the final
  answer; Wang et al. 2022 sampled at non-zero temperature — where an API exposes
  no sampling setting, repeated calls already vary run to run).
- **Structured output.** Getting reliable JSON/XML/function-call output requires
  more than "respond in JSON" — the two robust approaches are (1) provide a
  concrete schema/example in the prompt and validate + retry on parse failure, or
  (2) use the provider's native structured-output mode (e.g. Anthropic
  structured outputs / `strict: true` tool use, OpenAI Structured Outputs with
  `strict: true`), which constrains decoding to the schema rather than hoping
  the model complies. Only modes documented as schema-constrained give that
  guarantee — plain tool calling or "JSON mode" without a strict schema does
  not — and even constrained modes can return non-conforming output on a
  refusal or when generation is cut off by the token limit, and they constrain
  *shape*, not correctness of the values. Keep a validation step either way.
- **Prompt templates.** Production prompts are parameterized templates (Jinja-style
  or f-strings) with clearly delimited slots for untrusted content (e.g.
  `<document>{{doc}}</document>`), not ad-hoc string concatenation — this aids
  maintainability and makes it clearer to the model what is data, though
  delimiters alone do not stop prompt injection (see [[ai-ml-security]]).
- **Instruction placement.** Liu et al. ("Lost in the Middle", 2023) found
  that models used relevant information placed at the beginning or end of a
  long context more reliably than information in the middle. The size of this
  effect varies by model and may be smaller in newer long-context models
  than in the 2023 models tested, but the low-cost default still holds: put
  critical instructions and the actual question near the start or end
  (Anthropic's long-context guidance, for example, puts long documents first
  and the query last), not buried in a wall of reference text.
- **Decoding parameters as part of the prompt contract.** Temperature, top-p, and
  max tokens are not "prompt engineering" per se, but they interact with it.
  Whether sampling parameters exist at all depends on the provider, model, and API
  version. Where they are accepted, the usual convention is low temperature (near
  0) for near-deterministic, structured tasks and higher for creative generation —
  "near", because temperature 0 makes sampling close to deterministic but does not
  guarantee bit-for-bit identical output across runs (see docs/REPRODUCIBILITY.md);
  a great prompt at temperature 1.0 can still look flaky. Where they are not, do
  not send them: [[llm-evaluation]] documents that the Claude Messages API version
  2026-10-21 rejects `temperature`/`top_p`/`top_k`, and the anthropic Python SDK
  2.3.0's `messages.create()` has no such parameters (passing one raises
  `TypeError`). There consistency comes from the prompt itself, a schema-constrained
  output mode, and measuring run-to-run variation with repeated calls (the
  noise-floor approach in [[llm-evaluation]]), not from a sampling knob. Check the
  current provider docs before relying on any decoding parameter, and keep
  `max_tokens` high enough for reasoning models, whose thinking tokens can count
  against it.

## Gotchas
- **Few-shot examples that don't match the real input distribution** actively
  mislead the model — it tends to copy the *style* of your examples (length, edge
  cases, format) even when it's wrong for the actual query.
- **"Respond only in JSON" without a schema or grammar constraint** can still
  produce malformed output often enough to need a parse-validate-retry loop in
  production; prefer a native schema-constrained output mode when available,
  and still handle the refusal / truncated-output cases it doesn't cover.
- **Prompt injection via concatenated untrusted content.** If user-supplied or
  retrieved text is spliced directly into the prompt, it can contain instructions
  that override your system prompt. Always delimit untrusted content clearly and
  frame it as data, never as instructions — but treat this as risk reduction,
  not a fix; anything the model can do after reading untrusted text needs the
  architectural controls in [[ai-ml-security]].
- **Overloading one prompt with too many instructions** (tone + format + length +
  five edge cases) tends to degrade adherence once the count passes what the
  model handles reliably — a threshold that varies by model, so check it in
  evals; if adherence drops, split into a shorter core instruction plus
  explicit examples, or into multiple calls.
- **Prompt edits are regressions waiting to happen.** A change that fixes one
  failing input can silently break others; re-run a fixed test set (see
  [[llm-evaluation]]) on every prompt change instead of spot-checking.
- **Chain-of-thought increases latency and cost** and can leak reasoning that
  should stay internal (e.g. exposing a policy check) — decide up front whether
  the reasoning trace is shown to the end user.

## References
- [Anthropic — Prompt engineering overview](https://platform.claude.com/docs/en/build-with-claude/prompt-engineering/overview) — official guide to Claude-specific prompting techniques and structured output patterns.
- [Wei et al., 2022 — Chain-of-Thought Prompting Elicits Reasoning in Large Language Models](https://arxiv.org/abs/2201.11903) — the paper establishing the CoT pattern referenced above.
- [Brown et al., 2020 — Language Models are Few-Shot Learners (GPT-3)](https://arxiv.org/abs/2005.14165) — the foundational few-shot prompting paper.
- [Liu et al., 2023 — Lost in the Middle: How Language Models Use Long Contexts](https://arxiv.org/abs/2307.03172) — the position-sensitivity result behind the instruction-placement advice.
