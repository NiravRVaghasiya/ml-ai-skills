No. Showing tenant B's document to tenant A is a cross-tenant data leak, however good the answer is, and a system-prompt instruction doesn't prevent it: the model still reads B's text, can quote or paraphrase it, and a document planted in the corpus can tell it to ignore that instruction. A prompt is not an access control.

Enforce the boundary in code, before anything reaches the prompt:
- Build one index per tenant (or use a vector store that pre-filters by tenant) and search only the caller's tenant index, so B's vectors are never candidates for A.
- As defense in depth, re-check each retrieved chunk's tenant and ACL against the caller and drop mismatches.
- Include tenant and ACL in the dedup key, so identical text from two customers is never merged under one tenant's metadata.

If A's answers are worse without B's documents, that's a content gap in A's corpus to fix, not a reason to search B.
