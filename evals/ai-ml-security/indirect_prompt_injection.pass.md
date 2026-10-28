Treat it as an indirect prompt injection. The email body is third-party content: it is untrusted data, not an instruction from the operator, so nothing in it can authorize an action - least of all a state-changing refund or an irreversible export of customer records.

Mitigations should be architectural, because telling the model to ignore instructions in emails is not sufficient on its own:
1. Least privilege: the support agent should not have a "forward all customer records" capability at all; scope tools to the single customer and ticket being handled.
2. Refunds above policy require human approval or a business-rule check that the model cannot override.
3. Keep email text in a clearly delimited data channel and never let it flow into an execution path with unchecked authority; log and flag messages that contain instruction-like text for security review.
