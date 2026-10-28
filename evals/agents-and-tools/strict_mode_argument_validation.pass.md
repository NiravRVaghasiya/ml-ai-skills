No - keep the validation. `strict: true` guarantees that the arguments conform to the schema's shape and types (for example, `path` is a string), not that the values are safe or authorized. A schema-valid call can still be dangerous:

- `delete_file(path="../../.env")` or `delete_file(path="/etc/passwd")` is a perfectly valid string, but it reaches outside the sandbox through path traversal.
- `run_sql(query="DELETE FROM users")` is a valid string too: an unbounded, destructive statement. So is a query that reads another tenant's rows.

Before executing, still validate argument values and authorization on the server: canonicalize the path (resolve symlinks and `..`) and check that it is inside an allowed root; run SQL through read-only or narrowly scoped database credentials, or replace raw SQL with parameterized, purpose-built operations; check that any IDs belong to the caller. For destructive actions, require human confirmation. The sandbox and the permission scope are the real safety boundary; the schema only guarantees the call is well-formed.
