"""Executable-example tests: run the code blocks embedded in SKILL.md files.

These tests import third-party ML libraries (numpy, scikit-learn, torch, ...)
that the stdlib-only default suite does not assume. Each test module guards
itself with ``harness.requires(...)``: missing dependencies produce a SKIP
with a reason, unless ``ML_AI_SKILLS_REQUIRE_EXAMPLE_DEPS=1`` is set (the CI
examples job sets it), in which case a missing dependency is a FAILURE — so
CI cannot silently skip the example suite.
"""
