Plan the rollout so you can always get back to the current model:
1. Package and version the new model; keep the current production artifact and image addressable so rollback is a single config change, not a rebuild.
2. Deploy the new version alongside the old one and verify it: readiness/liveness probes pass, and a smoke test on known inputs returns valid predictions with the expected schema.
3. Shadow or canary first: mirror traffic, or send a small slice (e.g. 5%) to the new version, and compare latency, error rate and the prediction distribution with the current model.
4. Ramp up gradually (5% -> 25% -> 100%) only while those metrics stay within thresholds; define the rollback trigger before you start.
5. After full cutover, keep monitoring and keep the previous version deployable until the new one has been stable for an agreed period.
