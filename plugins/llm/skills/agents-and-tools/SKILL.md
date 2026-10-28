---
name: agents-and-tools
display_name: Agents and Tools
description: >
  Use when the user wants to understand or design an LLM agent — the
  observe-think-act loop, tool/function-calling schemas, planning vs. reactive
  control, or multi-agent orchestration patterns. Trigger phrases: "how does an
  LLM agent loop work", "design a tool-calling schema", "ReAct pattern",
  "multi-agent orchestration", "when should the agent stop looping", "give the
  model tools to call". NOT for retrieval-specific pipelines (see rag-pipeline),
  prompt wording/few-shot design for a single call (see prompt-engineering), or
  changing model weights (see fine-tuning-llms).
type: reference
domain: llm
level: intermediate
lifecycle: stable
risk_level: medium
evidence_level: established-practice
last_verified: 2026-10-27
capabilities:
  - agent-loop-design
  - tool-calling-schema-design
  - multi-agent-orchestration
  - agent-termination-guardrails
  - planning-vs-reactive-control
  - mcp-tool-integration
requires:
conflicts:
related:
  - prompt-engineering
  - rag-pipeline
  - llm-evaluation
  - agent-evaluation
  - ai-ml-security
inputs: A task an LLM-driven agent should accomplish, plus the set of tools/functions it may call.
outputs: A designed agent loop — tool schema, planning/reactive control choice, termination guardrails, and (if multi-agent) an orchestration pattern.
---

## Overview
An LLM agent extends a single prompt/response call into a loop: the model
decides which tool to call, observes the result, and decides what to do next
until it reaches a final answer or a stopping condition. This card covers the
loop mechanics, tool-calling schema design, and orchestration patterns for
composing multiple agents — the conceptual model to reach for before wiring up
any specific agent framework.

## Key Concepts
- **The agentic loop (ReAct: Reason + Act).** At each step the model produces a
  *thought* (reasoning about what to do next), an *action* (a tool call with
  arguments), then receives an *observation* (the tool's result) that gets
  appended to context for the next step. The loop repeats until the model emits
  a final answer instead of a tool call, or a stop condition fires. (The
  original ReAct paper elicited explicit Thought/Action/Observation text via
  prompting; with native tool-calling APIs the "action" is a structured tool-call
  block and a visible "thought" is optional — the loop structure is the same.)
- **Tool/function-calling schemas.** Each tool is exposed to the model as a
  name, a natural-language description, and a typed parameter schema (JSON
  Schema in most APIs, including Anthropic's tool use and OpenAI's function
  calling). The model selects a tool and emits arguments meant to match that
  schema;
  the *description* is one of the biggest levers over whether the model picks
  the right tool — treat it like an API doc aimed at the model, not a comment
  for humans. (Tool naming and how mutually-exclusive the tool set's scopes
  are also matter — see the Gotchas below — so a bad description is a strong
  suspect for tool-misselection but not the only possible cause.) Plain
  tool-calling does not by itself guarantee the emitted arguments match the
  schema; some providers offer a strict/constrained mode that does (e.g.
  Anthropic's `strict: true` tool definitions). Strict mode guarantees only
  the arguments' *shape and types*, not that the values are safe or in scope —
  so still validate argument values and authorization (paths, scopes, IDs,
  query breadth) before executing, especially for side-effecting tools.
- **Model Context Protocol (MCP).** An open protocol for exposing tools,
  resources, and prompts to LLM applications through a standard client/server
  interface, so one tool server can be reused across hosts. MCP standardizes
  *discovery and transport*, not *trust*: a third-party MCP server's tool
  descriptions and tool results are still untrusted input to the model (see
  [[ai-ml-security]]), and connecting one grants the agent whatever that
  server can do.
- **Planning vs. reactive control.** Reactive agents decide the next single
  step from current context only (classic ReAct). Planning agents first draft a
  multi-step plan (e.g. plan-and-execute), then execute steps against it,
  optionally replanning on failure — often more predictable for long tasks,
  but more expensive and rigid for short ones or for tasks where early
  observations invalidate the plan.
- **Memory.** Short-term memory is just the running transcript in context
  (thoughts, actions, observations); it's bounded by the context window and
  grows every step, so long-running agents need a context-management policy
  (truncating or summarizing old observations, or offloading them to files)
  before they hit the window limit. Long-term memory persists across
  sessions/tasks (a vector store, a scratch file, a database) and is
  retrieved rather than always included — this is where agent design and `rag-pipeline` overlap, but the
  retrieval mechanics themselves belong to that skill.
- **Multi-agent orchestration.** Common patterns: a **supervisor/orchestrator**
  agent that delegates subtasks to specialized worker agents and aggregates
  results (a plain **router** is the simpler case that classifies a request
  and hands it to exactly one handler); a **pipeline** of agents each
  transforming the previous one's output; **parallel fan-out** of independent
  subtasks; and **debate/critique** (evaluator-optimizer) setups where a second
  agent reviews the first's output before it's finalized. Orchestration adds
  coordination overhead, cost, and failure modes (miscommunication or lost
  context between agents) in exchange for specialization and parallelism — so
  start with a single well-tooled agent (or a fixed workflow with no agent
  loop at all) and add agents only when an evaluation shows the simpler design
  is insufficient.
- **Termination and guardrails.** Every agent loop needs an explicit stopping
  condition beyond "the model decided to stop": a max-iteration cap, a max-cost
  or max-tool-call budget, and ideally a way for a human or a validator to
  interrupt. Without this, a confused agent loops indefinitely, burning tokens
  and repeating failed actions.

## Gotchas
- **No max-iteration or budget guard.** A common and expensive production
  failure is an unbounded loop — a tool keeps returning an error, the model
  keeps retrying the same call. Always cap steps and cost, and fail
  loudly rather than silently retrying forever.
- **Ambiguous or overlapping tool descriptions.** If two tools' descriptions
  could plausibly apply to the same request, the model is likely to pick
  inconsistently between them. Keep tool sets small, names specific, and descriptions
  mutually exclusive in scope.
- **Unvalidated tool output fed straight back into the loop.** A tool that
  returns malformed data, an error string, or attacker-controlled content (e.g.
  scraped web text) becomes part of the model's context and can hijack
  subsequent reasoning (prompt injection via tool results) — validate/sanitize
  before feeding observations back in; see [[ai-ml-security]] "indirect prompt
  injection" for the full threat model, which this card only summarizes.
- **No sandboxing for code-execution or shell tools.** Giving an agent a
  code-execution or file-system tool without a sandboxed, permission-scoped
  environment is a security risk regardless of how well the prompt is written —
  the sandbox is the actual safety boundary, not the instructions; see
  [[ai-ml-security]] "excessive agent permissions" and use [[agent-evaluation]]
  to check whether a given trajectory actually respected that boundary.
- **Over-provisioning tools "just in case."** Every additional tool increases
  the chance of wrong selection and inflates the prompt with schema text on
  every turn; include only what the task genuinely needs. As a rule of thumb,
  once a single agent's tool set grows into the dozens and you observe
  misselection in evals, consider splitting it across specialized sub-agents
  or loading tool definitions on demand (some providers offer a tool-search
  mechanism for this) — the right cutoff depends on how distinct the tools are
  and on the model.

## References
- [Yao et al., 2022 — ReAct: Synergizing Reasoning and Acting in Language Models](https://arxiv.org/abs/2210.03629) — the reasoning/acting loop this card is built around.
- [Anthropic — Building Effective Agents](https://www.anthropic.com/research/building-effective-agents) — practical guidance on when to use an agent loop vs. a simpler workflow, and orchestration patterns.
- [Anthropic — Tool use with Claude](https://platform.claude.com/docs/en/agents-and-tools/tool-use/overview) — concrete tool-calling schema (`input_schema`, `tool_use`/`tool_result` round trip, `strict` mode) and loop mechanics.
- [Model Context Protocol — introduction and specification](https://modelcontextprotocol.io/) — the open protocol for exposing tools/resources to LLM hosts.
