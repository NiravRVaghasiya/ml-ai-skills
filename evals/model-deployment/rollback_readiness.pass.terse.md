1. Build and push the new model image with a version tag; leave the current version running so you can revert instantly.
2. Smoke test the new deployment and wait for its health checks to pass.
3. Route a canary share of traffic (say 10%) to it and monitor latency, error rates and output distributions against the current model.
4. Increase traffic step by step; if any metric crosses its threshold, roll back to the previous version.
