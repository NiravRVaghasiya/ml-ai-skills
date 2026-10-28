Unfortunately not. Strict mode constrains the *format* of the call: the model will always send a JSON object with a `path` string or a `query` string. It does not constrain *what* those strings contain. `delete_file("/")` and `run_sql("DROP DATABASE prod")` are both perfectly schema-compliant.

Keep server-side checks on the values:
1. For delete_file, resolve the path and verify it sits under the workspace directory; reject symlinks that escape it.
2. For run_sql, connect with a database user that only has the grants the agent needs (ideally SELECT-only), or replace free-form SQL with specific tools like get_order(id).
3. Require a human to approve any destructive action.

Think of the JSON schema as a parser guarantee, not a security control.
