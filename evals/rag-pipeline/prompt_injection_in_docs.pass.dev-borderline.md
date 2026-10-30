Treat it as an indirect prompt injection carried inside retrieved content. The document is untrusted data, not an instruction: only the system/developer prompt and the real user turn carry instruction-following authority.

Without delimiters a naive model will follow the embedded instructions, so wrap retrieved text in tags and mark it untrusted, inside a clearly delimited data region of the prompt. An undefended pipeline will follow the injected instructions and leak whatever secrets sit in its context, so also:
- Never put API keys or credentials in the context, and run the generation step with least privilege (no tool access it doesn't need).
- Add output checks that block answers echoing the system prompt or containing key-like strings.
- Flag the document for review and consider removing it from the index.
