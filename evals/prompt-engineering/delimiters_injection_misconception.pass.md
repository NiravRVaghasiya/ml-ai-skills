No. Wrapping documents in tags and telling the model to ignore instructions inside them reduces the risk, but it does not prevent prompt injection. The model still reads the text inside the tags, and a well-crafted instruction there can still be followed; the system-prompt rule is a request to the model, not an enforcement mechanism.

Specific gaps:
- Breakout: an uploaded document can contain a literal </document> followed by fake "system" text. Escape or strip tag-like sequences in untrusted content before inserting it, or use a randomized delimiter.
- Authority: whatever the model can do after reading the document, an attacker can try to make it do. Limit that with architectural controls: least privilege (only the tools and data this call needs), output validation before anything is shown or executed, and human approval for sensitive actions.

Keep the tags, since they make it clearer to the model what is data, but test the setup with adversarial injection documents, and see ai-ml-security for the full threat model.
