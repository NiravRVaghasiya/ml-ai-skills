I wouldn't. A refusal or a reply cut off at max_tokens isn't an answer of low quality - it's a missing answer, and averaging it in mixes two different questions ("how good are the answers?" and "how often did we fail to get one?"). It also wastes judge calls on rows that can't be graded.

Keep them separate instead:
- Treat any row whose stop_reason isn't end_turn (refusal, max_tokens) or whose text is empty as incomplete, and list those ids in the report with a count.
- Compute ROUGE and the judge score over completed answers only, and don't send incomplete rows to the judge.
- Make the regression gate fail while any generation is incomplete, then investigate: raise max_tokens for the truncations (thinking tokens count against it too) and look at why the model refused those three prompts - over-refusal can be a real regression in its own right.
