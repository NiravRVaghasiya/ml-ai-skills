Don't touch the reranker yet - this is a stale index. Your queries are now embedded by the new revision, but the stored vectors come from the old one. Even with the same dimension they live in different vector spaces, so the similarity scores are close to meaningless, and if documents were re-ingested since the build, some ids now point at outdated chunks. A reranker can only reorder what retrieval hands it.

"The loader didn't complain" just means nothing checked: a width check passes whenever the dimensions match. To fix it:
- Rebuild the index with the new revision (build it alongside the old one and switch over when it's done), or roll the serving container back to the revision the index was built with until the rebuild lands.
- Persist a manifest with the build - embedding model and revision, dim, normalization, chunk counts, corpus/build hash - and compare it with the running config at load time, refusing to serve (raise, fail at startup) on any mismatch.
