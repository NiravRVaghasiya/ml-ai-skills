# Freshness & version awareness policy

ML/AI APIs change fast. This repo separates **conceptual knowledge** (math,
algorithms, evaluation theory; doesn't go stale) from **implementation
knowledge** (a specific package's API surface; does go stale). It also says
exactly what its tooling checks about the second kind, and what it doesn't.

## Verification kinds: one vocabulary everywhere

Every tool output, `docs/claims.json` entry and doc in this repo uses these
names. They are defined once, in `scripts/check_freshness.py`
(`VERIFICATION_KINDS`). Each report prints what its kinds mean.

| Kind | What actually happened | Network | Where it runs | What it does NOT tell you |
|---|---|---|---|---|
| `static-pattern` | A regex over SKILL.md text matched (or didn't match) an API listed as removed in `known_stale_patterns` (how sure that list is: each entry's backing claim) | no | every CI run (`check_freshness.py`) | **Not verification.** No match only means no known-dead pattern was found. |
| `local-version-check` | A `version_constraints` specifier was compared with the version `importlib.metadata` reports in *this* Python environment | no | opt-in: `check_freshness.py --local-versions` | Anything about environments it didn't run in, or about API behaviour. A package that isn't installed is `SKIPPED`, not passed. |
| `automated-doc-check` | `verify_online.py` fetched reference URLs and PyPI's latest release | yes | scheduled workflow only | Whether a reachable page still says what the skill claims. |
| `runtime-test` | A test under `tests/` (usually `tests/examples/`) executed the code against the library versions installed in that run | no | locally, in an environment with the example dependencies (see docs/EXAMPLE-TESTS.md); in CI, the `examples` job in `ci.yml` installs `requirements-examples.txt`, sets `ML_AI_SKILLS_REQUIRE_EXAMPLE_DEPS=1` (a missing dependency fails instead of skipping), and runs `tests/examples` and `tests/test_check_freshness.py`. The `unit` and `unit-with-dev-deps` jobs do not install the example dependencies, so tests needing numpy/faiss/openai skip there. Of the CI jobs, only the `examples` job's results have been observed so far (docs/KNOWN-ISSUES.md #4) | Behaviour on other versions, with real models or remote services the test stubbed, or anything in a run where the test skipped. |
| `human-review` | A named person read the cited source and recorded reviewer, date and URL in `docs/claims.json` | n/a | by hand | Anything after `last_reviewed`. |

`last_verified` in frontmatter is a **review date**, not any of the above (see
docs/SKILL-SPEC.md, "What last_verified means"). Nothing in this repo is
"live-verified" unless a `runtime-test`, `automated-doc-check` or
`human-review` record says so.

## `scripts/check_freshness.py` (offline, every CI run)

```bash
python scripts/check_freshness.py                  # human report + legend
python scripts/check_freshness.py --json           # report_version 2: {kinds, levels, counts, findings, ...}
python scripts/check_freshness.py --local-versions # + compare specifiers with installed packages
python scripts/check_freshness.py --policy PATH --today 2026-10-27
python scripts/check_freshness.py --claims PATH    # claims registry that gates STALE patterns (default docs/claims.json)
python scripts/check_freshness.py --validate-policy
```

Each finding has a `kind` (`static-pattern`, `review-clock`, `metadata`,
`local-version-check`) and a `level`:

| Level | Meaning | Fails CI? |
|---|---|---|
| `STALE` | Matches a `known_stale_patterns` entry with level `STALE` whose backing claim in `docs/claims.json` is verified by a runtime test or human review (`verification` is `runtime-test` or `human-review`, status `current` or `review-due`), or `last_verified` isn't a real date | **yes** (exit 1) |
| `REVIEW_DUE` | Needs a human look: `last_verified` older than the policy allows, free-text or malformed `version_constraints`, imports with no constraints at all, a `REVIEW_DUE` pattern (e.g. a dated model snapshot), a `STALE` pattern whose backing claim is missing, `disputed`, or not `runtime-test`/`human-review` (the finding carries `policy_level: STALE` and a `level_reason`), or an installed version outside the specifier | no |
| `INFO` | Bookkeeping: an entry names a package but has no specifier, an import has no matching constraint entry, a constraint name was aliased to its PyPI dist | no |
| `SKIPPED` | `local-version-check` couldn't run (package not installed / unparseable installed version). **Not a pass.** | no |
| `OK` | `local-version-check`: the installed version satisfies the specifier, in this environment only | no |

Exit code 2 means the policy file or the claims registry is missing or invalid.

**A STALE verdict needs a verified claim.** A `STALE` pattern fails CI for every
skill it matches, so `check_freshness.py` reads `docs/claims.json` (`--claims`)
and only reports `STALE` when the pattern's backing claim was actually checked:
`verification` must be `runtime-test` or `human-review`. `local-version-check`
and `automated-doc-check` don't count, because neither says anything about
whether an API still exists (see the kinds table in `check_freshness.py`).
If the claim is missing, `disputed` or backed only by one of those weaker
kinds (including `unverified`), a match is still reported,
as `REVIEW_DUE`, with the policy level and the reason in the finding; the JSON
report lists such patterns under `stale_patterns_not_applied` (and the human
report prints a `NOTE` line) even when no skill matches them. A verified claim
whose review clock ran out (`status: review-due`) keeps failing CI:
`check_claims.py` asks for the re-check instead. Library callers of
`check_skill()` that pass no registry get the policy level unchanged.

### Freshness policy: `schema/freshness_policy.json`

How old `last_verified` (and a claim's `last_reviewed`) may get before it is
`REVIEW_DUE`. The most specific rule wins:

1. `skill_max_age_days[<slug>]`
2. `capability_max_age_days[<capability>]`: if several of a skill's
   capabilities match, the **strictest** applies
3. `domain_max_age_days[<domain>]`
4. `default_max_age_days`

Every override is `{"max_age_days": N, "reason": "..."}` and must give a
reason. The day counts are maintainer policy choices, not measured churn
rates. Today: default 180; `foundations` 365, `classical-ml` 270, `mlops` 120,
`llm` and `security` 90; capability `mcp-tool-integration` 60; skill
`model-optimization` 90. Age exactly equal to the limit is still fresh; one
day more is due.

The same file holds:

- `ignore_imports`: modules that aren't PyPI dists (e.g. `src`, the user's own
  package in ci-cd-for-ml), each with a reason.
- `known_stale_patterns`: `{id, pattern, level, message, claim}`. These used
  to live in code. They moved to the policy so the list can be reviewed in one
  place, and so each entry can point at the `docs/claims.json` record that
  backs it. `level: STALE` means "believed removed", not "confirmed": a
  "check whether this still exists" pattern (dated model snapshots) is
  `REVIEW_DUE`, so it can't fail CI on a guess. A `STALE` pattern whose
  backing claim is missing, `disputed`, or not `runtime-test`/`human-review`
  is reported as `REVIEW_DUE` by `check_freshness.py` (see above), and `check_claims.py`
  lists it as `stale-pattern-unverified` so someone verifies the claim.
  Today:
  - The three scikit-learn patterns are backed by `runtime-test` claims
    (`tests/test_check_freshness.py::TestKnownStaleApisStillGone`, scikit-learn
    1.9.0).
  - The two openai-python patterns are backed by
    `TestOpenAiModuleLevelApiRemoved` (openai 3.40.0, run in the example venv;
    it skips where openai isn't installed).
  - `tensorflow-contrib` is backed by
    `TestTensorflowContribGone` (tensorflow-cpu 2.21.0, run in a scratch venv
    on 2026-10-29; no CI job installs TensorFlow, so it skips there). That
    STALE verdict rests on one local run that CI does not repeat.
  - `openai-text-davinci` is still `unverified`: OpenAI's deprecations page
    could not be read from the test machine. Its matches are `REVIEW_DUE`
    until a person confirms the claim.

- `acknowledged_inconclusive`: reference URLs (or hosts) that answer
  automated clients with a fixed 4xx/5xx, e.g. a 403 bot wall. Each entry has
  `url` or `host`, `http_status`, `reason`, `acknowledged_on` and an
  optional `expires_on`. `verify_online.py` reports a match as
  `acknowledged`, not a finding, up to and including `expires_on`, judged
  against `--today` (default: today's UTC date; the date source is injectable
  in `run()`/`check_url()` for tests). Without `expires_on`, the expiry is
  `acknowledged_on` + `default_max_age_days`; an `expires_on` before
  `acknowledged_on` or later than that is a validation error, so an
  acknowledgement always lapses and needs re-confirming. 404/410 can't be
  acknowledged. The process is in docs/EVIDENCE.md, "Human review".

`validate_policy` rejects unknown keys and domains, non-positive day counts,
missing reasons, regexes that don't compile, and duplicate pattern ids. When
it runs against the real skills, it also rejects skill or capability keys that
don't exist, because a typo there would silently switch an override off. Any
error exits 2.

### `version_constraints` convention

```yaml
version_constraints:
  - "faiss-cpu==1.15.1: IndexFlatIP.search -1 padding (verified by runtime test tests/examples/test_rag_pipeline.py)"
  - "scikit-learn>=1.4: GridSearchCV, StratifiedKFold (executed against 1.9.0 on 2026-10-27; lower bound not verified)"
```

`<pip-dist-name><PEP 440 specifier>: <API relied on> (<verification note>)`.
The leading requirement is parsed with a small stdlib PEP 440 subset: `==`,
`!=`, `>=`, `<=`, `<`, `>`, `~=`, `==X.*`/`!=X.*`, comma-joined, optional
`[extras]` (ignored), and the `<V` / `>V` exclusions from PEP 440 read
literally: `<V` drops pre-releases of V unless V is one (so a final or post V
acts like `<V.dev0`), and `>V` drops V's local versions and, unless V is a
post- or dev-release, V's post-releases. "Of V" means V exactly:
`>1.0rc1` accepts `1.0+cpu`, `>1.0.dev0` accepts `1.0.post1`, `<1.0.post1`
accepts `1.0rc1`. That is how `packaging` 26.1 and later decide it; packaging
24.2 to 26.0 compared base versions instead and disagree on those cases
(measured 2026-10-29 with packaging 24.2, 25.0, 26.0, 26.1, 26.2 and 26.3).
Candidates may be pre-releases (like `pip check`). Inside the subset the
results agree with `packaging`:
`tests/test_check_freshness.py::TestPep440AgreesWithPackaging` compares
version parsing, ordering and containment over a generated matrix of epochs,
pre/dev/post (incl. implicit `-N`) and local versions against every operator.
It skips, saying why, when `packaging` isn't installed, and skips only the
`<`/`>` comparison when the installed `packaging` is older than 26.1; fixed
stdlib-only cases in `TestPep440Subset` pin the exclusions either way.
Rejected on purpose,
although `packaging` accepts them: `===` (arbitrary equality) and an empty
clause such as `>=1.0,` (more likely a typo here). Also rejected, as by
`packaging`: `.*` with anything but `==`/`!=` or after a pre/post/dev part,
local labels except with `==`/`!=`, and `~=` with one release segment.
Environment markers aren't supported. Import and alias
names map to their dists (`sklearn` → `scikit-learn`, `cv2` → `opencv-python`,
`faiss` → `faiss-cpu`, `PIL` → `pillow`, `PyTorch` → `torch`, ...; see
`IMPORT_TO_DIST`). Legacy entries that don't start with a requirement are
reported as `unparseable (free text) - review` (`REVIEW_DUE`), not as errors.
The verification note in parentheses is free text for humans. Tooling never
reads it as evidence; structured evidence goes in `docs/claims.json`. It says
which of three things each part of the entry is, and never implies more:

- **executed**: `executed against <dist> <version> on <date>`, the version a
  test actually ran (`requirements-examples.txt`, or the local environment
  where it differs, named as such);
- **documented lower bound**: `<bound> lower bound documented: <source>, read
  <date>`, when release notes, a changelog, package metadata or the source at
  a release tag shows the API first appearing in that release. The bound
  itself is still not executed unless the note says so;
- **estimate**: `<bound> lower bound not verified` (or "is an estimate"),
  for a bound nobody checked. Raise it to the executed version, document it,
  or leave it marked; never invent a minimum.

## `scripts/check_claims.py` (offline, validates `docs/claims.json`)

Validates the claims registry: schema and enums; real dates at most one day
in the future (timezone slack); the skill exists; evidence fits the
verification kind; status agrees with verification. `runtime-test` evidence
must name an existing `tests/**/test_*.py[::Class[::method]]`, resolved with
`ast` so the method must be inside the named class. It also confirms that
every `claim` id cited by `known_stale_patterns` exists, and reports
`REVIEW_DUE` for a `STALE` pattern whose claim is missing, unverified or
disputed. A claim whose `last_reviewed` is older than the
policy limit for its skill is `REVIEW_DUE`, which fails only with `--strict`.
The script doesn't re-run any evidence: a `runtime-test` claim is only as
current as the last test run that backs it. Field meanings and the review
process are in docs/EVIDENCE.md.

## `scripts/verify_online.py` (network, scheduled job only)

Never part of the default test suite (`tests/test_verify_online.py` mocks the
HTTP layer). It never edits skills; it only flags.

- **Reference URLs**: every `http(s)` link under `## References`, plus every
  `source_url` in `docs/claims.json`. HEAD first, then GET if HEAD fails. At
  least `--min-interval` seconds (default 1) between requests to the same host.
  Up to `--retries` (default 3) retries on 429/502/503/504 and timeouts, with
  exponential backoff that follows `Retry-After` (seconds or HTTP-date, capped
  at 120 s). Per-request `--timeout`, an identifying User-Agent, and only
  http(s), including on redirects. If HEAD ends throttled (429/503) or timed
  out after its retries, there's no GET fallback, which would only repeat the
  same waits. 404/410 is `broken`. 401/403 bot walls, persistent 429/5xx,
  timeouts and DNS failures are `inconclusive`, meaning a human should look,
  unless the URL is listed in `acknowledged_inconclusive` (then
  `acknowledged`, not a finding).
- **Bounded run time**: `--max-seconds` (default 1200; 0 = unlimited) is a
  budget for the whole run. A request is not started with less than 1 s of
  budget left; its socket timeout is `min(--timeout, remaining budget)`; a
  per-host pacing wait or a retry wait that would cross the deadline is not
  slept (the retry is abandoned). urllib's timeout applies to each socket
  operation, not the whole request, and DNS lookups have none, so each
  request also runs under a watchdog that abandons it 2 s after its timeout
  (`error_kind: timeout`, "watchdog"; the abandoned request finishes in a
  daemon thread and its result is discarded; the JSON `budget` block counts
  them in `abandoned_requests`). The run therefore passes the deadline by at
  most about 2 s, plus writing the report. Once the budget is spent, nothing
  more is sent, and the remaining items are `inconclusive` with
  `error_kind: deadline`. PyPI is checked before the reference URLs, so a
  slow reference host can't starve it.
- **Circuit breaker**: `--host-failure-limit` (default 3; 0 = off) stops
  contacting a host after that many consecutive failed requests to it; the
  rest are `circuit-open`. `--breaker-on` lists the failure classes that count
  (default all four): `throttle` (429/503), `server-error` (other 5xx),
  `timeout` (incl. watchdog) and `network` (DNS failure, refused or reset
  connection), each judged after the request's own retries. A definitive HTTP
  answer (2xx/3xx, or a 4xx other than 429) resets the count; a failure of a
  class that isn't listed leaves it unchanged. HEAD and its GET fallback are
  two requests. The JSON `budget` block records `breaker_on`.
- **PyPI latest release**: for each dist in a parseable constraint, it reads
  `https://pypi.org/pypi/<dist>/json`, takes the highest non-yanked,
  non-pre-release version, and flags `excludes-latest` when the specifier
  rejects it (e.g. `implicit>=0.7,<0.8` once 0.8.0 ships). That means "re-test
  against it", not "it's broken".
- **Exit codes**: 0 no findings, 1 findings for human review (or an
  interrupted run), 2 network unavailable, 3 invalid freshness policy. Exit 2 comes from the pre-flight probe to `https://pypi.org/simple/`
  failing, or no request getting any HTTP response. The report then has
  `status: SKIPPED` and a `reason`. Nothing was checked, and it isn't a pass.
- **Output**: a human summary on stdout, and the JSON report (report_version
  2, with a `budget` block: `max_seconds`, `exhausted`, `open_circuits`) via
  `--json` or `--json-out PATH`. With `--json-out`, the file is replaced
  atomically after every item with `status: INCOMPLETE`, and finally with the
  real status. A run that is killed or crashes therefore still leaves a
  partial report that says it is partial.

### `.github/workflows/freshness-scheduled.yml`

Runs weekly (Monday 06:23 UTC) and on `workflow_dispatch`, never on PRs.
`permissions: contents: read`, `persist-credentials: false`, actions pinned
to commit SHAs. The timeouts nest: `--max-seconds 1200` (20 min) inside a
25-minute step timeout inside a 30-minute job timeout. It runs the offline
scans and `verify_online.py`, and uploads all reports as the
`freshness-report-<run_id>` artifact (kept 90 days). The Result step reads the
report's `status`: `OK` is green, while `FINDINGS`, `SKIPPED`, `INCOMPLETE` or a
missing/unreadable report turns the run red. The offline scans' exit codes are
recorded rather than discarded and judged in the same step: a `STALE` finding from
`check_freshness.py`, an error from `check_claims.py`, a crash of either (any
other exit code), or a missing or unreadable JSON report from either also turns
the run red (`REVIEW_DUE` alone does not; neither scan runs with `--strict`).
So neither a finding, a skipped run nor a crash reads as green, and
acknowledged bot walls don't keep it red. It doesn't open issues or push anything:
a maintainer triages the artifact (see docs/EVIDENCE.md, "Human review").

The older `validate_skills.py --check-links` (a single HEAD per URL, run on
manual dispatch in ci.yml) still exists. `verify_online.py` is the more
careful version.

## What is still NOT checked

- **API behaviour in versions nobody ran.** No job installs the latest
  release of each package and runs the examples against it. `excludes-latest`
  only tells you when that's worth doing. A `runtime-test` claim covers the
  version recorded in its `applies_to.version` and nothing else.
- **Silent behaviour changes**: a default value that changed under the same
  name and signature. Only a `runtime-test` that asserts the behaviour catches
  that.
- **Content drift on reachable pages**: a 200 doesn't mean the page still
  supports the claim. Only `human-review` covers that.
- **Lower bounds** in specifiers (`>=0.20`) are rarely executed. The
  constraint's own note says whether a bound was run, documented from a
  cited source, or is an estimate (see the `version_constraints` convention
  above). On 2026-10-29 twelve more bounds were documented from release
  notes or source at release tags (an agent's reading, recorded in each
  note); the rest remain estimates or equal the executed version.
  Lower-bound-only specifiers can never "exclude the latest release", so
  `excludes-latest` only fires for pins (`==`) and upper bounds (`<`, `~=`).
  Skills written against an older major (e.g. `pandas>=1.0` while pandas 3.x
  is current) need the example tests, or a reviewer, to notice.
