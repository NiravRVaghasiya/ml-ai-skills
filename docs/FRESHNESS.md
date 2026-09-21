# Freshness & version awareness policy

ML/AI APIs change fast. This repo separates **conceptual knowledge** (math,
algorithms, evaluation theory — doesn't go stale) from **implementation
knowledge** (a specific package's API surface — does go stale), and is explicit
about what its tooling can and cannot verify about the latter.

## What `scripts/check_freshness.py` actually checks

Three purely mechanical signals, each clearly labeled by severity:

1. **`STALE` — known-removed/renamed APIs.** A fixed, hand-maintained list of
   regex patterns for things that are *definitely* gone as of this repo's
   authoring (e.g. `sklearn.cross_validation`, `openai.Completion.create`,
   `tensorflow.contrib`). This list only grows when someone confirms a pattern
   is actually dead — never speculatively. Matching one fails CI.
2. **`REVIEW_DUE` — `last_verified` older than 180 days.** A staleness clock,
   not a correctness signal — see docs/SKILL-SPEC.md "What last_verified
   means". Informational only; does not fail CI.
3. **`REVIEW_DUE` — Python code imports a package but declares no
   `version_constraints`.** A prompt to go add one, not proof anything is
   wrong. Informational only; does not fail CI.

## What it does NOT do, stated explicitly

- It does not call any package registry, vendor API, or documentation site.
  There is no network access assumed during either authoring or CI's default
  job.
- It cannot tell you whether a currently-passing pattern-free skill is actually
  compatible with whatever the latest release of a package is *today* — absence
  of a `STALE` finding means "nothing matched a known-dead pattern," not
  "verified against the current release."
- It cannot detect a *behavior* change in an API that kept the same name/
  signature (e.g. a default value changing) — only removals/renames it's been
  told about.

## The `--check-links` live check

`scripts/validate_skills.py --check-links` does a real `HEAD` request against
every `https://` URL in every skill's `## References`. This is the closest
thing in the repo to live verification, and it is deliberately **not** run in
the default CI job (`.github/workflows/ci.yml`'s `link-check` job only runs on
manual `workflow_dispatch`), because external site availability/rate-limiting
is not something merges should be blocked on.

## Closing the gap (future work, not implemented)

A real freshness system would run on a schedule (not per-PR), actually
`pip install` the packages named in each skill's `version_constraints`, import
the referenced symbols, and flag anything that now raises `ImportError`/
`AttributeError`/`DeprecationWarning`. That's a meaningfully bigger piece of
infrastructure (a sandboxed install-and-import job per package per skill) than
anything else in this repo, and was not built here because no such job exists
yet to point at — see docs/SKILL-SPEC.md "Known limitations" item 1.
