"""Opt-in integration tests: SKILL.md claims checked against real models, hardware and services.

Every class here is gated by an ``ML_AI_SKILLS_INTEGRATION_<NAME>=1`` environment variable
(see ``tests/integration/gate.py`` and docs/EXAMPLE-TESTS.md, "Integration tests"). Without it
the class is skipped with a reason naming the variable, so the default suite stays offline,
stdlib-only and fast. With it, a missing dependency, binary or device is a failure, not a skip:
an opt-in run that silently skipped would look like evidence.

Run from the repository root:

    ML_AI_SKILLS_INTEGRATION_TF=1 python -m unittest discover -s tests/integration -t tests -v
"""
