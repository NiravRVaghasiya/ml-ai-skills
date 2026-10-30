This is indirect prompt injection: the email body is untrusted data, not instructions from you. Remove the "forward all customer records" capability from the agent entirely (least privilege) and require human approval for any data export. A system prompt telling the model to ignore embedded instructions is not enough on its own.

The refund line is different, though. A customer asking for a refund is a normal support request, so the agent should still process the refund request for order #9911 automatically; only the data export needs a human.
