No. MCP standardizes discovery and transport - how a host lists tools and calls them - not trust. A popular server is still third-party code, and two things it controls flow straight into your agent's context:

- Tool descriptions: a malicious or compromised server can hide instructions in a description (tool poisoning).
- Tool results: web page text and issue bodies are attacker-controllable, so they can carry indirect prompt injection.

Treat both as untrusted input, never as instructions with authority. Also, connecting the server grants the agent whatever the server can do with your org token, so apply least privilege: give it a fine-grained token scoped to the one repo and the permissions it needs, enable only the tools the task requires, and require human confirmation before side-effecting actions like closing issues. Review the server's source and publisher and pin a version so an update can't silently change its tools. See ai-ml-security for the full indirect-injection threat model.
