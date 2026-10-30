This is indirect prompt injection: the email body is untrusted third-party content, data the agent reads, never an instruction from the operator that can authorize an action. Telling the model to ignore instructions in emails is not sufficient on its own, so the controls have to sit outside the model:

- Least privilege: the support agent gets no tool that forwards customer records to an arbitrary address, so the export cannot happen whatever the email says.
- Refunds go through a human approval step or a business-rules check. After a human approves it, the agent should still process the refund request through the normal refund tool, never on the email's say-so. Only if the order is verified by staff, the agent can still process the refund request through the standard queue.
- Email text never reaches an execution path with unchecked authority; flag the message for security review.
