# Security policy

## Reporting a vulnerability

Please report security issues privately. Do not put vulnerability details in a public issue.

1. **GitHub private vulnerability reporting.** On the repository page, open the **Security and
   quality** tab (named **Security** on older GitHub layouts) and click **Report a
   vulnerability**. The button exists only if the maintainer has enabled the feature (see
   "Maintainer configuration" below).
2. **Security contact,** if one is configured below.
3. **If neither is available,** open a public issue that asks the maintainer for a private
   security contact, with no details of the vulnerability. This is the fallback GitHub's own
   documentation describes.

### Maintainer configuration

<!-- MAINTAINER-CONFIG: security-contact. Replace the PENDING line with a monitored address or
     URL, or with "none: use private vulnerability reporting". Never put a guessed address here.
     tests/test_doc_consistency.py checks this line. -->
Security contact: PENDING (maintainer action required; no contact has been published)

Until that line is filled in, there is no published private contact (`docs/KNOWN-ISSUES.md`
#23). The maintainer has to do two things, and neither can be done from a pull request:

- **Enable private vulnerability reporting** (public repositories only): **Settings**, then
  **Advanced Security** in the "Security and quality" section of the sidebar, then **Enable**
  next to "Private vulnerability reporting". Steps as written in GitHub's documentation,
  "Configuring private vulnerability reporting for a repository"
  (https://docs.github.com/en/code-security/how-tos/report-and-fix-vulnerabilities/configure-vulnerability-reporting/configure-for-a-repository,
  read 2026-10-29). It has not been confirmed that this repository has the feature enabled.
- **Replace the `PENDING` line** above, and then remove item 23 from `docs/KNOWN-ISSUES.md`.

Please include the affected file or command, the commit or release, steps to reproduce, and
the impact you observed. This is a single-maintainer project with no response-time
commitment, but reports will be acknowledged and fixes noted in `CHANGELOG.md` under
"Security".

## Scope

In scope:

- **The installer and its helpers.** `install.sh`, `uninstall.sh`, `bin/ml-ai-skills` and
  `scripts/list_skills.py`: writes outside the target directory, following links, deleting
  files they did not create, or overwriting local edits without `--force`. The threat model
  and guarantees are in [`docs/INSTALLER.md`](docs/INSTALLER.md).
- **Repository tooling in `scripts/`**, for example a script that executes repo content it
  should only parse, or a network call outside the documented opt-in modes
  (`verify_online.py`, `validate_skills.py --check-links`, `run_evals.py` model mode, and its
  `claude-cli` backend, a subprocess that uses the CLI's own authentication).
- **CI and release workflows** in `.github/workflows/`: script injection, excessive token
  permissions, unpinned actions, or a release path that publishes something other than
  the tagged commit.
- **Skill content that gives insecure guidance**, such as code that loads untrusted pickles,
  exposes an unauthenticated endpoint, or passes untrusted retrieved text to a model with
  tool access, without saying so. Report these too; they are fixed like any other defect.

Out of scope: vulnerabilities in third-party packages the skills mention (report those
upstream), and the behaviour of Claude Code or any other agent that loads these files.

## What this project does not guarantee

- **Skills are guidance, not vetted secure code.** Code blocks are executed in tests against
  tiny synthetic data and stubbed services (`docs/EXAMPLE-TESTS.md`). That is not a security
  review of the code in your environment. Review code before running it, especially skills
  with `risk_level: medium` or `high`.
- **No skill has had a recorded human security review.** Phase 1 and 2 audits were done by
  agents (`docs/SKILL-AUDIT.md`).
- **Prompt-injection and agent-safety advice reduces risk; it does not prevent attacks.**
  `ai-ml-security`, `rag-pipeline` and `agents-and-tools` describe mitigations. None of them
  claims to make a system injection-proof.
- **The installer's guarantees are limited to its documented threat model.** A local
  attacker who can write the target directory during a run, and malicious `SKILL.md`
  content in a checkout you chose to install, are out of scope (`docs/INSTALLER.md`).
- **Dependency audits are point-in-time.** CI runs `pip-audit` on the pinned and installed
  environments. A newly published advisory is caught only on the next CI run, and no
  `pip-audit` result from GitHub Actions has been recorded yet (`docs/KNOWN-ISSUES.md` #4).
- **Dependencies are pinned, not hash-checked.** Python dependencies are exact pins plus full
  Linux x86_64 / CPython 3.13 locks in `constraints/`, with no hashes. CI runs `pip-audit` on
  the direct pins, both locks and both installed environments. On 2026-10-29, run locally
  (not on GitHub Actions), `pip-audit` 2.10.1 found no known vulnerabilities in the direct
  pins and both locks (`docs/REPRODUCIBILITY.md`). Accepted residual risk: no hash checking
  (three example packages build from sdists, and torch comes from the PyTorch index), so
  integrity rests on exact versions and TLS to the indexes. The example environment is
  test-only.
- **Release provenance shows origin, not safety.** A verified checksum and build attestation
  (`docs/RELEASE.md`) show the tarball came from this repository's release workflow at the
  tagged commit. They say nothing about whether the content is correct or safe.
- **The workflow audit is static.** `scripts/audit_workflows.py` does not trace data passed
  through files, `$GITHUB_ENV`/`$GITHUB_OUTPUT`, composite-action inputs, or
  `workflow_dispatch`/`workflow_call` inputs.
