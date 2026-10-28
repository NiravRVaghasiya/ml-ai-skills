It helps a little, but no. The model ignored the 429s once and it can ignore a sentence too; a system-prompt rule is a request, not a control. Put the stop condition in your agent loop:

- A hard cap on tool calls per run (say 15) and a per-run spend limit that aborts the run when hit.
- Track the last few (tool, args) pairs; if the same call repeats 3 times, stop and return an error to the caller.
- Handle 429 inside the search tool: wait for the Retry-After interval with exponential backoff, and after N failures return a structured "rate_limited, do not retry" result.
- Alert when a run is aborted so it's visible.
