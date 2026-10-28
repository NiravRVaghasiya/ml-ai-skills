# Executable example tests

`tests/examples/` runs the Python code blocks embedded in `SKILL.md` files, so a
skill's published code is exercised, not a hand-maintained copy of it.

## How it works

- `tests/examples/harness.py` loads the canonical `<slug>/SKILL.md`, extracts its
  fenced `python` blocks in document order, and `exec`s the selected blocks in one
  shared namespace inside a fresh temporary working directory.
- Inputs the test environment cannot provide — downloaded models, API clients,
  files on disk — are injected through `namespace=` (pre-seeded variables) or
  `stubs=` (fake modules in `sys.modules`). Stubs must reproduce the documented
  behavior the block relies on (e.g. FAISS padding missing neighbours with `-1`),
  and the test must say what is stubbed and why.
- `replace=` performs exact, must-match-once textual substitutions (e.g. shrinking
  `n_trials=200` to `n_trials=3`). A substitution that no longer matches fails the
  test, so a skill edit can't silently disable it.
- A block whose first non-blank line starts with `# illustrative` is documentation
  only (needs a GPU cluster, a paid API, a running service, ...). It is not
  expected to run standalone, and it does not count as executed coverage.

## Dependencies and skips

The default suite is stdlib-only. Example tests guard themselves with
`@harness.requires("numpy", "sklearn", ...)`:

- dependency missing → **skipped**, with the missing module named in the reason;
- `ML_AI_SKILLS_REQUIRE_EXAMPLE_DEPS=1` (set by the CI `examples` job) → a missing
  dependency is a **failure**, so CI cannot silently skip the suite.

Pinned versions for the CI job live in `requirements-examples.txt`. It pulls CPU-only
`torch`/`torchvision` from the PyTorch index through its `--extra-index-url` line, so
install it into a dedicated virtual environment:

```bash
python -m venv .venv-examples
.venv-examples/bin/python -m pip install -r requirements-examples.txt   # Windows: .venv-examples\Scripts\python.exe
.venv-examples/bin/python -m unittest discover -s tests/examples -t tests -p "test_*.py" -v
```

`tests/examples/test_experiment_tracking.py` (MLflow, about 100-160 s) is opt-in. It skips
unless `ML_AI_SKILLS_SLOW_EXAMPLES=1` or `ML_AI_SKILLS_REQUIRE_EXAMPLE_DEPS=1` is set, so a
default local run, and a default `scripts/run_evals.py --run-executable`, report it as
skipped. Skipped is not evidence.

**Last observed run:** 2026-10-28, Windows 11, Python 3.13.2, local example venv,
`ML_AI_SKILLS_SLOW_EXAMPLES=1`: 327 tests, OK, in about 256 s. The venv had mlflow 3.16.1,
while `requirements-examples.txt` pins 3.17.2. The Linux pins have not been installed or run
by us; the CI `examples` job is configured but has not run.

## Coverage

Set `ML_AI_SKILLS_EXAMPLE_COVERAGE=<path>` to append one JSON line per successful
`run_blocks` call (`{"skill", "blocks", "total_blocks"}`). A line is written only after
every selected block ran without raising. `scripts/quality_report.py` folds these lines into
the machine-readable quality report, either via `--run-examples PYTHON` (which sets the
variable itself) or via `--example-coverage FILE`. It counts only non-illustrative block
indices. A record whose `total_blocks` differs from the skill's current number of python
blocks (or that has none) is not counted, and the skill is reported as `coverage-stale`: the
harness records no git revision or content hash, so the block count is the only link between
an ingested coverage file and the current tree. A skill whose example tests fail is reported
as `example-tests-failing` whatever its block coverage, but only when example test results
are also supplied (`--run-examples`, or `--test-results` with a file written by
`--collect-tests --suite examples`); with `--example-coverage` alone, test failures are not
visible, and the report lists `example-test-outcomes` under `skipped_checks`. In the
2026-10-28 run, all runnable blocks of all 26
code-bearing skills were executed at least once. A block that runs is evidence the
code executes against the pinned library versions with the stubbed inputs — it is
not evidence that the advice is optimal, or that it behaves identically against a
real downloaded model or remote service.
