Not as a blanket rule. Chain-of-thought helps on some tasks and models and not on others, so treat it as something to test, not a default.

- Small model: in Wei et al. (2022), CoT gains on reasoning benchmarks emerged only at large model scale, and smaller models sometimes did worse with it, so the 1B model may get no benefit or get worse.
- Reasoning model: with extended thinking on, the model already reasons before it answers. An extra "think step by step" may add little, and it can conflict with the provider's guidance for those models; follow their docs instead.
- Cost: visible CoT adds output tokens and latency, and the reasoning text may need to be hidden from users or stripped before you parse the final answer.

Measure it: run your fixed eval set for each model/prompt pair with and without the CoT line, and keep it only where it actually improves accuracy enough to justify the cost (see llm-evaluation).
