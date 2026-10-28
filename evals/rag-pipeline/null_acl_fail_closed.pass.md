I'd do the opposite: fail closed. A null or missing ACL means "we don't know who may read this", not "everyone may". If you map it to no restriction, the first time the connector fails on an HR or legal folder, those documents are exposed to every user in the tenant through retrieval - and nothing about the answers will tell you it happened.

Instead:
- At ingestion, reject (or quarantine) any document whose `allowed_groups` is missing, `None` or `""`, as malformed. Tenant-wide access must be an explicit `allowed_groups: []`.
- Log and count the rejections by document id (never the text) and alert when they spike, so the connector gets fixed rather than silently widening access.
- Keep the access check in code before anything reaches the prompt; never ask the model to hide chunks a caller may not see.

Dropped documents cost recall until the connector is fixed; leaked documents can't be un-leaked.
