Don't just re-run it until it goes green - a pipeline that passed for months and now fails without a code change usually means something else changed, and that is a real regression signal.

1. Read the CI logs for the failing step: which test or assertion fails, and with what error or traceback?
2. Diff the environment against the last green run: unpinned dependencies (a new pandas/scikit-learn/numpy release pulled in transitively), the runner or base image, the Python version.
3. Check the data side: did the upstream data source, schema, or the test data snapshot change? A data-contract test failing is often exactly this.
4. Reproduce locally with the last green lockfile vs. the current resolution to confirm the cause, then fix it - pin the dependency or update the code/tests deliberately - rather than retrying.
