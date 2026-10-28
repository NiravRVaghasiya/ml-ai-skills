# Security policy

## Reporting a vulnerability

Please report security issues privately. Do not open a public issue for them.

- **Preferred:** GitHub private vulnerability reporting for this repository (the repository's
  **Security** tab, then **Report a vulnerability**), if the maintainer has enabled it.
- **Contact:** `TODO(maintainer): add a security contact address here`. No contact address
  has been published yet; until one is, use private vulnerability reporting.

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
  (`verify_online.py`, `validate_skills.py --check-links`, `run_evals.py` model mode).
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
- **Release provenance shows origin, not safety.** A verified checksum and build attestation
  (`docs/RELEASE.md`) show the tarball came from this repository's release workflow at the
  tagged commit. They say nothing about whether the content is correct or safe.
- **The workflow audit is static.** `scripts/audit_workflows.py` does not trace data passed
  through files, `$GITHUB_ENV`/`$GITHUB_OUTPUT`, composite-action inputs, or
  `workflow_dispatch`/`workflow_call` inputs.
