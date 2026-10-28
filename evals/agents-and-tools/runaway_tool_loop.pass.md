No. A line in the system prompt is not a reliable stopping condition: the model could already see that the calls were failing and kept going, and an instruction can't enforce anything. Termination has to live in the harness code, outside the model.

Add these guards:
- A hard max-iterations / max-tool-calls cap per run, sized from what your normal tasks need.
- A cost or token budget that aborts the run when exceeded, whatever the model wants to do next.
- Loop detection: if the same tool is called with identical arguments, or the same error comes back several times in a row, stop or force a different strategy instead of re-issuing the call.
- Handle the 429 in the tool layer, not via the model: exponential backoff honouring Retry-After, plus a circuit breaker that returns a structured "search unavailable" error after repeated failures.
- Fail loudly: end the run with a clear error and whatever partial result exists, and alert on it, rather than retrying silently.

Then replay runs like this one with agent-evaluation to check that the guards actually trigger.
