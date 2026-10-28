# Formal Skill Specification

This is the binding contract every `SKILL.md` must satisfy. `scripts/validate_skills.py`
enforces the parts of it that are mechanically checkable; the rest (Phase 15 audit
judgments like "is the scope appropriate") is enforced by human/agent review and
recorded in `docs/SKILL-AUDIT.md`.

Allowed enum values, required/optional keys and length limits live in exactly one
place: `schema/allowed_values.py`. `schema/skill-frontmatter.schema.json` is
*generated* from it (see "Formal schema" below). If you're about to hardcode a list of
domains or risk levels somewhere else, import it from there instead.

## Frontmatter contract

```yaml
name: kebab-case-name          # MUST equal the folder name; <= 64 chars
display_name: Human Readable Name
description: >                  # "Use when..." + 3+ quoted trigger phrases + "NOT for"
  Use when the user wants to do X. Trigger phrases: "phrase one", "phrase two",
  "phrase three". NOT for Y, which another skill owns.
type: workflow                 # workflow | reference
domain: classical-ml           # see schema/allowed_values.py DOMAIN
level: beginner                # beginner | intermediate | advanced
lifecycle: stable              # stable | draft | deprecated
risk_level: low                # low | medium | high | critical
evidence_level: established-practice   # primary | official-documentation |
                                        # established-practice | heuristic | opinion
last_verified: 2026-09-21      # YYYY-MM-DD — see "What last_verified means" below
capabilities:                  # routing tags — short noun/verb phrases, used by
  - thing-one                  # scripts/router.py; NOT free-form keywords, be
  - thing-two                  # specific enough to disambiguate from siblings
requires:                      # HARD prerequisites — leave empty unless the skill
  - other-skill-slug           # genuinely cannot be applied without another skill's
                                # output/context already established. Most skills
                                # have none; use `related` for soft pointers instead.
conflicts:                     # skills that give contradictory guidance for the
                                # same situation. Expected to be empty for nearly
                                # every skill; exists so a real audit finding has
                                # somewhere structured to go instead of prose.
related: [sibling-skill-one, sibling-skill-two]   # flow lists are fine too
inputs: One-line description of what the skill expects as input.
outputs: One-line description of what the skill produces.
version_constraints:           # OPTIONAL — only for skills whose Workflow embeds
  - "scikit-learn>=1.4: GridSearchCV(cv=...) (model-knowledge estimate, not live-verified)"
---
```

The key set is **closed**: a key that is not in `REQUIRED_KEYS` or `OPTIONAL_KEYS` is an
error (`unknown-key`). Claude Code silently ignores frontmatter fields it does not
recognise (stated in the Claude Code skills documentation,
https://code.claude.com/docs/en/skills, read 2026-10-27), so a typo such as
`risk-level:` would otherwise vanish without a trace. To adopt a Claude-native field
such as `allowed-tools`, add it to `OPTIONAL_KEYS` deliberately, regenerate the schema,
and re-lock the contract (adding an optional key is a non-breaking change). The same
page says claude.ai uploads, the Skills API and `package_skill.py` are stricter and
*reject* keys outside the six Agent Skills fields; see Known limitation 7.

### Field-by-field notes

- **`name` / `description` limits.** Claude Agent Skills require `name` to be at most
  64 characters of lowercase letters, numbers and hyphens, with no XML tags and none of
  the reserved words `anthropic` / `claude`; `description` must be non-empty, at most
  1,024 characters, with no XML tags. Confirmed against
  https://platform.claude.com/docs/en/agents-and-tools/agent-skills/overview (fetched
  2026-10-27). The validator measures the description *as YAML parses it* (a folded
  `>` block keeps one trailing newline, which counts).
- **`lifecycle`** — `stable` (default for anything meeting the Definition of Done
  below), `draft` (written but not yet evidence/freshness-reviewed to the `stable`
  bar), `deprecated` (kept for backward compatibility; the router should prefer
  whatever it's superseded by — track that via a note in the skill body, since
  `supersedes` was considered and dropped as a separate field to avoid a rarely-used
  duplicate of `conflicts`; if it's ever needed for real, add it back deliberately).
- **`risk_level`** — risk of *harm if the guidance/code is followed without
  additional review*, not a quality score. `model-deployment` is `high` even when
  perfectly written, because a mistake there can cause a production outage;
  `attention-mechanisms` is `low` because acting on it has no side effects.
- **`evidence_level`** — the *dominant* tier backing the skill's substantive claims,
  not a claim that every sentence is independently sourced:
  - `primary` — grounded in a specific original paper/standard.
  - `official-documentation` — grounded in vendor/framework docs as the primary source.
  - `established-practice` — widely taught/consensus knowledge, not tied to one paper.
  - `heuristic` — a rule of thumb with known exceptions (the skill body MUST phrase
    it conditionally, e.g. "as a starting point, above roughly N categories,
    consider..." — never as a universal law like "always do X").
  - `opinion` — a style/preference call with no objective backing; used rarely.
- **What `last_verified` means.** It is the date an agent/human last *reviewed this
  skill's structure and claims for accuracy to the best of the reviewer's
  knowledge*. It is explicitly **NOT** "this code was run against the current
  version of every referenced package/API today" — unless a `version_constraints`
  entry says that specific code was executed, and against which version. Treat it as
  a staleness clock, not a correctness certificate. See `docs/FRESHNESS.md`. It must be
  a real calendar date and may not be in the future (one day of grace absorbs time-zone
  differences between a reviewer and a UTC CI runner).
- **`capabilities` / `requires` / `conflicts` / `related` items** are lowercase-kebab
  (`[a-z0-9]+(-[a-z0-9]+)*`), unique within the list, and never the skill's own slug.
  A slug may not appear in both `requires` and `conflicts`. `conflicts` is symmetric in
  meaning, so a one-sided entry is a warning. A capability tag shared by two skills is
  a warning (routing collision: `scripts/router.py` cannot tell them apart on that tag).
- **`version_constraints` format.** Each entry is one string:

  ```
  "<dist><specifier>: <API> (<verification>)"
  ```

  - `<dist>` is the distribution name you would `pip install` (`scikit-learn`, not
    `sklearn`); `<specifier>` is an optional PEP 440 specifier set (`>=1.4`,
    `>=0.7,<0.8`, `==1.15.1`).
  - `<API>` names the specific calls the Workflow relies on.
  - `(<verification>)` says how the claim is known — e.g. `(executed against
    scikit-learn 1.9.0 on 2026-10-27)` when an example test actually ran it, or
    `(model-knowledge estimate, not live-verified)` when it did not. Never imply
    execution that did not happen.

  Entries that don't match are a **warning** (`version-constraint-format`), not an
  error, while existing skills migrate to this format; tighten it to an error once no
  skill warns. If you're not reasonably confident of a specific milestone, omit the
  entry rather than invent one — an absent list is honest; a fabricated version is not.
- **`requires` vs `related`.** `requires` is rare and strict: a directed, acyclic,
  "cannot be meaningfully applied without" relationship (checked by
  `scripts/build_dependency_graph.py`). `related` is the common case: a soft
  sibling pointer with no ordering or cycle constraint. When in doubt, use `related`.

## Supported YAML subset

Claude Code reads frontmatter with a real YAML parser — and if the YAML doesn't parse,
the skill loads with *no* fields set (Claude Code skills documentation, read
2026-10-27). The repo's tooling is stdlib-only, so `scripts/skills_lib.py` implements
the subset below. Inside it, the result is identical to PyYAML's `yaml.safe_load`
(dates excepted: they stay ISO strings); `tests/test_skills_lib.py` asserts that for
every `SKILL.md` and `evals/*/*.yaml` file and for a seeded random-document fuzz.
Anything outside the subset is not guessed at: it produces a diagnostic with a line
number, which the validator reports as `yaml-syntax` (the `problem` field names it).

Supported:

- Top-level `key: value` pairs, one per line, keys at column 0. Keys may contain
  letters, digits, `_`, `-`, `.` and spaces (so `risk-level:` is *read*, then rejected
  as an unknown key with a suggestion — not silently dropped).
- Plain scalars, including multi-line plain scalars continued on more-indented lines
  (folded with single spaces, as YAML does). ` #` starts a comment; `#` without a
  preceding space is content (`c#`, `x#y`).
- `'single-quoted'` scalars (`''` is a literal quote) and `"double-quoted"` scalars with
  YAML escapes (`\"`, `\\`, `\n`, `\t`, `\uXXXX`, …), each on a single line.
- Block scalars `>` and `|` with at most one chomping indicator (`-` or `+`) and an
  optional indentation indicator (`1`–`9`), in either order (`|-1`, `|1-`). Default
  "clip" chomping keeps one trailing newline, exactly like YAML.
- Only spaces and tabs count as whitespace around a plain scalar, as in YAML: a
  trailing no-break space (U+00A0) or other Unicode space is part of the value (so
  `name: my-skill<NBSP>` fails the name checks instead of validating as `my-skill`).
  YAML line-break characters other than LF/CRLF are diagnosed (see the table below).
- Block lists (`- item`, at the key's indentation or deeper) and single-line flow
  lists (`[a, b]`, `[]`, trailing comma allowed) of scalars; `key:` with nothing after
  it is null (used for "no items").
- Full-line and trailing comments.

Diagnosed instead of parsed (each is a `yaml-syntax` error):

| `problem` | Example | Why |
|---|---|---|
| `duplicate-key` | `level:` twice | YAML keeps only the last value |
| `mapping-in-scalar` | `inputs: A table: rows` / `- note: detail` | YAML reads `: ` as a nested mapping (or rejects it) — quote the value |
| `implicit-type` | `level: yes`, `x: 1.5`, `2026-01-01: a` | YAML resolves these to bool/number/date, not strings — quote them |
| `tab-indentation` / `tab-character` | a tab before `- item` or in a plain value | YAML forbids tab indentation; PyYAML rejects tabs in many other positions |
| `unparseable-line` / `unexpected-indentation` | `this is prose`, a stray indented line | not `key: value` / not attached to any value |
| `unsupported-syntax` | `{a: b}`, `&anchor`, `*alias`, `!tag`, multi-line quoted or flow values, nested lists | outside the subset |
| `unsupported-syntax` | a value starting with `,`, `]` or `}` (`inputs: ,x`); `[?x]` / `[:x]` in a flow list | YAML rejects the first form outright (the skill would load with no fields); PyYAML reads the second as a mapping — quote the value |
| `implicit-type` | `x: =`, `x: <<` | PyYAML resolves a bare `=` to its `value` type and `<<` to its merge key; `safe_load` fails on both — quote it |
| `implicit-type` | `x: 2026-02-30` | date-shaped but not a real calendar date: PyYAML resolves it as a date and raises, rejecting the whole document — fix the date or quote it |
| `unsupported-syntax` | `[a}]`, `[}]` | a `}` inside a flow list (YAML rejects the document) — quote the value |
| `unsupported-syntax` | a lone CR, U+0085 (NEL), U+2028, U+2029 anywhere in the frontmatter; VT, FF or another control character | YAML reads the first group as line breaks (`name: x<NEL>` is `x` to PyYAML) and PyYAML's reader rejects the rest; the subset splits lines on LF only, so it reports them instead of reading a different value (CRLF line endings are fine) |
| `unparseable-line` | `\|-+`, `>+-` | two chomping indicators in a block-scalar header (YAML rejects it) |
| `invalid-escape` | `"\q"` | not a YAML escape |

The same parser reads `evals/*/*.yaml` (`scripts/eval_lib.py` uses
`parse_frontmatter_with_diagnostics`), so eval cases follow the same subset — in
particular, quote any list item that contains `: `.

## Code-block convention: `# illustrative`

Workflow code is meant to run. A fenced `python` block whose first non-blank line
starts with `# illustrative` is documentation only — it needs something the test
environment cannot provide (a GPU cluster, a paid API, a running service) and is not
expected to run standalone. `tests/examples/harness.py` skips such blocks by default,
and they do not count as executed coverage (see `docs/EXAMPLE-TESTS.md`). Use the
marker sparingly and only for that reason; a block that merely needs a small input
should get one (or be fed one by its example test) instead. The validator's
`python-syntax` warning still applies to illustrative blocks: they should at least
parse.

## Section contract (unchanged from the original authoring pass)

- **Every skill:** `## Overview` (2–4 sentences).
- **Workflow skills:** `## Overview` → `## Workflow` → `## Gotchas` → `## References`.
  No `## Key Concepts` section.
- **Reference skills:** `## Overview` → `## Key Concepts` → `## Gotchas` → `## References`.
  No `## Workflow` section.
- `## Gotchas` ≥ 3 specific, real pitfalls. `## References` ≥ 2 real, resolvable links.
- Links in the body must survive the skill folder being packaged or installed on its
  own (that is how `plugins/` and `install.sh` ship it): use absolute `https://` URLs
  for external material and `[[slug]]` wiki-links for sibling skills. A relative link
  may only point inside the skill's own folder; `../` (or `/`-rooted) paths are errors.
  Links and wiki-links inside code blocks and inline code are ignored.

## Validation rules

`python scripts/validate_skills.py [--warnings] [--strict] [--json] [--root DIR] [--today YYYY-MM-DD]`

Every finding carries a stable `code`, the human `rule` text, the `file` (relative to
`--root`), the frontmatter `field` when there is one, a `line` when known, and a `fix`
string. `--root` points the validator at another tree (e.g. a fixture copy, as
`tests/test_validator_mutations.py` does); `--today` pins the clock for the
future-date rule. Exit code: 1 on any error (or any warning with `--strict`), 2 if
`--root` is not a directory.

| Code | Severity | What it checks |
|---|---|---|
| `frontmatter-malformed` | error | file starts with a `---` line and has a closing `---` line |
| `yaml-syntax` | error | frontmatter stays inside the supported YAML subset (see `problem`) |
| `missing-required-key` | error | every `REQUIRED_KEYS` entry present; scalars non-empty (`key:` with no items is valid for list keys) |
| `unknown-key` | error | no keys outside `REQUIRED_KEYS`/`OPTIONAL_KEYS`; suggests the closest key |
| `invalid-enum` | error | enum fields use an allowed value |
| `invalid-type` | error | list keys are lists, scalar keys are strings, list items are non-empty strings |
| `invalid-date` | error | `last_verified` is a real `YYYY-MM-DD` calendar date |
| `future-date` | error | `last_verified` is not more than one day ahead of today |
| `invalid-list-item` | error | `capabilities`/`requires`/`conflicts`/`related` items are lowercase-kebab |
| `duplicate-list-item` | error | no repeated item in any list |
| `name-mismatch` | error | `name` equals the folder name |
| `invalid-folder-name` | error | folder name is lowercase-kebab |
| `invalid-name` | error | `name` is lowercase-kebab |
| `name-too-long` | error | `name` ≤ 64 characters (Claude Agent Skills) |
| `name-reserved-word` | error | `name` does not contain `anthropic` or `claude` (Claude Agent Skills) |
| `description-too-long` | error | `description` ≤ 1,024 characters (Claude Agent Skills) |
| `description-xml-tag` | error | `description` has no `<tag>`-like text (Claude Agent Skills) |
| `description-not-for` | error | `description` has a "NOT for …" clause |
| `description-trigger-phrases` | error | `description` has ≥ 3 quoted trigger phrases |
| `self-reference` | error | a skill does not list itself in `related`/`requires`/`conflicts` |
| `requires-conflicts-overlap` | error | no slug in both `requires` and `conflicts` |
| `unknown-skill-reference` | error | `related`/`requires`/`conflicts` name existing skill folders |
| `duplicate-name` | error | no two folders declare the same `name` |
| `requires-cycle` | error | the `requires` graph is acyclic |
| `missing-section` | error | Overview/Gotchas/References, plus Workflow or Key Concepts per `type` |
| `wrong-section-for-type` | error | workflow skills have no Key Concepts; reference skills have no Workflow |
| `too-few-gotchas` | error | ≥ 3 top-level bullets under `## Gotchas` |
| `too-few-references` | error | ≥ 2 top-level bullets under `## References` |
| `no-code-blocks` | error | workflow skills have at least one fenced code block |
| `unbalanced-fence` | error | an even number of ```` ``` ```` fences |
| `broken-link` | error | relative links resolve to an existing file |
| `link-escapes-skill` | error | relative links stay inside the skill folder. Checked for inline links (with or without a `"title"`, `<…>` destinations), reference definitions (`[id]: path`) and HTML `href`/`src`; `\` counts as `/`; `file:` URLs and drive paths (`C:/…`) always count as escaping |
| `unresolved-wiki-link` | error | every `[[slug]]` (outside code) names an existing skill |
| `index-mismatch` | error | INDEX.md lists exactly the skill folders, with the right `[W]`/`[R]` tag and ✅ |
| `schema-stale` | error | `schema/skill-frontmatter.schema.json` equals the generator's output |
| `fewer-code-blocks-than-steps` | warning | numbered Workflow steps each have code (a step may legitimately continue the previous block) |
| `python-syntax` | warning | python blocks pass `ast.parse` (a block may be a deliberate fragment) |
| `url-unreachable` | warning | `--check-links` only: reference URLs answer a HEAD request (sites rate-limit/flake) |
| `conflicts-asymmetric` | warning | if A conflicts with B, B lists A (the fix is a one-line edit in the other skill, which may be owned by someone else) |
| `capability-collision` | warning | a capability tag belongs to one skill only (overlap is sometimes deliberate for broad siblings; it is a routing signal, not a correctness defect) |
| `version-constraint-format` | warning | `version_constraints` entries follow `"<dist><specifier>: <API> (<verification>)"` (warning during migration, see above) |

## Formal schema

`schema/skill-frontmatter.schema.json` (JSON Schema draft 2020-12) is generated from
`schema/allowed_values.py` — never edit it by hand:

```
python scripts/generate_schema.py --write   # after changing allowed_values.py
python scripts/generate_schema.py --check   # CI: fails if the committed file is stale
```

The validator enforces the frontmatter rules above that JSON Schema can express by
validating each skill against this schema with a small stdlib validator
(`skills_lib.schema_errors`: `type`, `enum`, `const`, `pattern`, `required`,
`properties`, `additionalProperties`, `items`, `minLength`/`maxLength`,
`minItems`/`maxItems`, `uniqueItems`, `format: date`). `pattern` uses ECMA-262 semantics
as JSON Schema requires: `$` matches only at the very end, so a slug with a trailing
newline (e.g. from a `|` list item) is rejected. Python's `re` lets `$` match before a
final `\n`, and the `jsonschema` package uses Python `re`, so that one input is the
documented, tested exception to the agreement check below. It raises on any other keyword,
so the schema cannot quietly depend on something the validator ignores, and
`tests/test_schema.py` checks it reaches the same verdicts as the `jsonschema` package
on every skill and on a set of deliberately broken frontmatters (skipped when
`jsonschema` isn't installed). Rules the schema cannot express (future dates, reserved
words, XML tags, cross-skill references, body checks) stay in `validate_skills.py`.

## Public contract lock

`schema/public-contract.json` records everything downstream users can depend on by
name: the skill slugs, the plugin names and which skills each plugin ships
(`scripts/generate_plugins.build_plan`), the marketplace name, the required and
optional frontmatter keys, every enum value, and `VERSION`.

```
python scripts/check_contract.py --check   # CI: fails if the contract differs from the lock
python scripts/check_contract.py --write   # re-lock after an intended change
```

- `--check` fails whenever the computed contract differs from the lock and prints each
  change, labelled `BREAKING` or not, with the command to run. A contract change is
  therefore always an explicit, reviewed `--write`.
- **Breaking** changes: a removed skill, plugin, plugin member, enum value or optional
  key; a removed or demoted required key; a new required key (including promoting an
  optional key); a renamed marketplace. `--write` refuses these unless `VERSION`'s major
  number is greater than the major recorded in the lock. It also refuses a `VERSION`
  lower than the locked one.
- **Non-breaking** changes (additions, a version bump) are re-locked without a major
  bump; describe them in `CHANGELOG.md` like any release.
- Both modes require `CHANGELOG.md` to contain a `## [<VERSION>]` heading for the
  current `VERSION`.

## Validation philosophy

`scripts/validate_skills.py` draws a hard line between ERROR (blocks CI) and
WARNING (informational):

- **ERROR** — anything the spec makes non-negotiable and that is checkable without
  false positives: frontmatter valid against the generated schema and inside the
  supported YAML subset, correct section set for the declared type, minimum
  gotcha/reference counts, relationships pointing at real skills with no cycles or
  self-references, `name` matching the folder and Claude's limits, links that survive
  packaging, INDEX.md matching the actual skill set.
- **WARNING** — signals with a real false-positive rate or an intentional transition
  period: a Python block that fails `ast.parse` (could be a deliberately truncated
  fragment), fewer code blocks than numbered steps, an unreachable reference URL when
  checked live, one-sided `conflicts`, shared capability tags, and the
  `version_constraints` format during migration.

This split exists so CI failing actually means something is wrong, instead of
training everyone to ignore CI because it's noisy. `tests/test_validator_mutations.py`
applies representative defects to a copy of the real skills and asserts each one is
reported with the right code, file and field — and that the unmodified copy reports
nothing.

## What `evals/` is and is not

See `evals/SCHEMA.md`. In short:

- **It is** a set of structured behavioural cases, one `plan.json` per skill, and per-case
  rubrics (`expected_behavior`, `must_not`) with deterministic **hard assertions**
  (`must_include` / `must_not_include` regexes). `scripts/validate_evals.py` checks that every
  case is well-formed, every pattern is valid and non-vacuous, and every case's `.pass*.md`
  fixtures pass and `.fail*.md` fixtures fail. `scripts/run_evals.py` writes a
  machine-readable report offline, can grade *recorded* responses against the hard
  assertions, runs the `tests/examples` ids of `kind: executable` cases, and compares two
  reports for regressions.
- **It is not** a measured benchmark of model behaviour. `run_evals.py` has a model mode
  (explicit opt-in: provider, model, `--allow-network` and an API key), but it has never
  been run. No pass rate, judge agreement or with-skill vs without-skill uplift exists
  anywhere in this repository, and reports say `baseline: unmeasured`.
- Regex hard assertions show that concepts are named and that anticipated wrong conclusions
  are absent. They do not show that the reasoning is correct. Independent probes found
  large miss rates before the patterns were tuned, and those probe answers are now
  fixtures (`docs/KNOWN-ISSUES.md`). The rubric stays authoritative; grading against it is
  a human or LLM-judge step.

## Known limitations of this spec/tooling (stated explicitly, not hidden)

1. **No live API/package verification by default.** `last_verified` and most
   `version_constraints` entries are review-date and model-knowledge markers, not proof
   of current correctness. Execution evidence comes only from the example tests in
   `tests/examples/`, against the versions in `requirements-examples.txt`, and only for the
   stubbed inputs those tests use. Individual checkable claims and how each was checked are
   in `docs/claims.json`. Network checks (`scripts/verify_online.py`,
   `validate_skills.py --check-links`) run only on demand or on the scheduled workflow,
   never as a merge gate.
2. **The router is lexical, not semantic.** `scripts/router.py` scores stemmed words
   and word pairs from each skill's name, tags, trigger phrases, description and
   overview (IDF-weighted), with a domain anchor, confidence gating and rule-based
   negation scoping. It has no embeddings. It still fails on paraphrases that share no
   vocabulary with the metadata, on homonyms, and on vocabulary that appears in no skill;
   `docs/ROUTING.md` lists the measured failure modes. Its benchmark (`evals/routing/`)
   measures this router, not Claude Code's own skill selection. The
   `capability-collision` warning catches exact tag overlap only, not near-synonyms.
3. **Evidence classification is skill-level, not claim-level.** `evidence_level`
   describes the dominant tier for a skill's claims as a whole; it does not tag
   every individual sentence. A skill classified `established-practice` can still
   contain a `primary`-sourced claim or a `heuristic` aside — the body text is
   expected to phrase heuristics conditionally regardless of the frontmatter tier.
4. **No model-graded behaviour.** Eval fixtures are graded offline by regex hard
   assertions; real model output has never been graded. See "What `evals/` is and is
   not" above.
5. **The YAML parser is a subset, compared against PyYAML (YAML 1.1).** Equality is
   asserted for the repo's files and a seeded fuzz, not proven for all inputs, and
   Claude Code's own parser may differ from PyYAML in corners (e.g. YAML 1.2 reads
   `yes` as a string). The subset sidesteps those corners by diagnosing implicit
   bools/numbers instead of interpreting them. `tests/test_skills_lib.py` additionally
   requires every repo `SKILL.md` and eval case to parse without diagnostics and equal
   `yaml.safe_load` (skipped if PyYAML isn't installed, as in the stdlib-only CI job).
6. **Markdown checks are line/regex based.** Section and bullet counting, fence
   balancing and link extraction do not use a full CommonMark parser; e.g. `~~~` fences
   are honoured when blanking code for link checks but not by the ```` ``` ```` balance
   count, and anchors (`#section`) in relative links are not verified. Link extraction
   works one line at a time, so these forms are **not** checked by `broken-link` /
   `link-escapes-skill`: link text or a destination that wraps onto the next line, a
   reference definition whose destination is on the following line, an HTML tag split
   across lines, and link text that itself contains `]` (e.g. `[see [1]](x)`).
7. **Skills are Claude Code-compatible, not upload-compatible.** The Claude Code skills
   documentation (https://code.claude.com/docs/en/skills, read 2026-10-27) says claude.ai
   skill uploads, the Skills API and `package_skill.py` from anthropics/skills accept
   only `name`, `description`, `license`, `compatibility`, `metadata` and
   `allowed-tools`, and fail with a hard error (`Unexpected key(s) in SKILL.md
   frontmatter`) on anything else. Every skill here carries about 14 repo-specific keys
   (`display_name`, `type`, `domain`, …), so it installs in Claude Code and its
   plugins (installation is tested; runtime loading is not, see `docs/COMPATIBILITY.md`)
   but would be rejected by those upload paths unless the extra keys are stripped or
   moved under `metadata` first. The validator does not check this, and the upload paths
   have not been tried.
8. **The Claude limits are as documented on 2026-10-27.** If Anthropic changes them,
   update `NAME_MAX_LENGTH`/`DESCRIPTION_MAX_LENGTH`/`RESERVED_NAME_WORDS` in
   `schema/allowed_values.py` and regenerate the schema.
