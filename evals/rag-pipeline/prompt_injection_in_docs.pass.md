Treat it as data, never as an instruction. That text is an indirect prompt injection carried inside retrieved content; only the system/developer prompt and the real user turn have instruction-following authority, and a retrieved document is untrusted input no matter how relevant it scored.

What the pipeline should do:
- Keep retrieved chunks inside a clearly delimited data region in the prompt (e.g. wrapped in document tags, with any tag-like text inside escaped) and tell the model that content there is quoted material to answer from, not commands.
- Never put secrets in the context: API keys and credentials should not be reachable by the generation step, and the generator should run with least privilege (no tools it doesn't need).
- Check outputs before returning them: block answers that echo the system prompt, contain key-like strings, or cite sources that weren't retrieved.
- Log and flag the document for review, and consider removing it from the index.
