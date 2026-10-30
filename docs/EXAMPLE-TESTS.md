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

On Linux x86_64 with CPython 3.13 (as in CI), add `-c constraints/examples-linux-cp313.txt`
to the `pip install` line. On Linux, `implicit` also needs the system OpenMP runtime
(`libgomp.so.1`, Debian/Ubuntu package `libgomp1`), which pip does not install. Without it, 3
recommender-systems cases failed on WSL Ubuntu under `scripts/run_evals.py --run-executable`,
which runs each test in isolation. In the same WSL venv the one-process `tests/examples`
discover run passed (351 tests, none skipped; `docs/REPRODUCIBILITY.md`), so the failure was
seen only in isolated runs. It was reported by one workstream and not reproduced since.

`tests/examples/test_experiment_tracking.py` and `TestReleaseAgainstRealMlflow` in
`test_ci_cd_for_ml.py` (real MLflow on a SQLite store) are opt-in locally. They skip unless
`ML_AI_SKILLS_SLOW_EXAMPLES=1` or `ML_AI_SKILLS_REQUIRE_EXAMPLE_DEPS=1` is set, so a default
local run reports them as skipped, and so does a default `scripts/run_evals.py
--run-executable` for the experiment-tracking test ids that `kind: executable` cases list.
Skipped is not evidence. Run `python scripts/run_evals.py --run-executable --slow-examples
--require-executed` to execute those listed test ids through the eval harness; a skip is then
a failure. No eval case lists `TestReleaseAgainstRealMlflow`, so run it directly with the
environment variable set. In
the CI examples job, which sets `ML_AI_SKILLS_REQUIRE_EXAMPLE_DEPS=1`,
they must run, and `test_harness.py` guards that in three ways:

- `TestNothingSkipsAtImportUnderTheCiEnvironment` re-imports every example module under
  `ML_AI_SKILLS_REQUIRE_EXAMPLE_DEPS=1` and fails if a decorator skips any class or test
  method. It also checks that both MLflow classes run under either variable.
- `TestRuntimeSkipsAndTheCiJobUnderTheRequireEnv` checks that the `examples` job in
  `.github/workflows/ci.yml` still sets the variable at job level and still runs
  `tests/examples`. The import-time check sets the variable itself, so without this check it
  would keep passing if CI dropped it.
- The same class scans for `self.skipTest` calls made at run time. Each must either fail
  under the variable in the same test, or be listed with a reason in
  `RUNTIME_SKIP_CLASSIFICATION`.

The listed runtime skips:

- three version gates that do not fire on the pins: evidently 0.7, transformers >= 5,
  pandas < 3;
- the CPU-only `pin_memory` case in `test_pytorch_patterns.py`, which skips when CUDA *is*
  present.

`test_data_pipelines.py`'s "pendulum not installed" check is not on that list: it skips in a
default local run, and under `ML_AI_SKILLS_REQUIRE_EXAMPLE_DEPS=1` it fails instead
(`pendulum` is pinned in `requirements-examples.txt`).

The CI job also fails on any skip at all: its "No example test was skipped" step greps
`examples.log` for skipped tests and classes after the run, so even a classified runtime skip
fails the job there. The skipped tests are listed in the job summary as well.

**Last observed full run:** 2026-10-29, on Windows 11 with CPython 3.13.2, in a fresh venv.
The venv held the `requirements-examples.txt` pins as of 21:32 that day, installed with
`pip install --prefer-binary -r requirements-examples.txt`, which resolved every pin on
Windows, including mlflow 3.17.2 and torch 2.14.1+cpu. Differences from CI's environment:

- It was installed without CI's `constraints/examples-linux-cp313.txt` lock, which is for
  Linux.
- `wrapt==2.5.1` was pinned later. This venv has wrapt 2.5.2rc1 (a prerelease) as a
  transitive dependency, and that is its only difference from today's pins.
- lime, which publishes only an sdist, came from a locally cached wheel.

With `ML_AI_SKILLS_REQUIRE_EXAMPLE_DEPS=1` and each module in its own interpreter: 29
modules, 374 tests, all OK, 0 skipped. Other changes were being made in the working tree
during the run, so it is not tied to one commit. Earlier, 2026-10-28: 327 tests, OK, on a venv
with mlflow 3.16.1.

**MLflow 3.17.2 (the pin):** on 2026-10-29, `test_experiment_tracking` and `test_ci_cd_for_ml`
ran with `ML_AI_SKILLS_SLOW_EXAMPLES=1 ML_AI_SKILLS_REQUIRE_EXAMPLE_DEPS=1` in a venv with only
mlflow 3.17.2 and the scikit-learn/pandas/numpy/matplotlib/pytest/SQLAlchemy/pyarrow pins: 22
tests (9 + 13), OK, none skipped. Two runs of the same command took 47 s and 169 s, which
shows the load on the shared machine. In the full venv above they ran 9 and 13 tests, OK. On
mlflow 3.16.1, only `test_serialization_defaults_match_the_gotcha` fails, as expected, because
the gotcha describes the pinned 3.17.2.

**Runtime.** Per-module wall times in that run (fresh interpreter per module, on a 12-core
machine shared with other heavy jobs, so expect large run-to-run variance): nlp-tasks 525 s,
computer-vision 464 s, fine-tuning-llms 317 s, llm-evaluation 227 s, rag-pipeline 212 s,
ci-cd-for-ml 170 s, experiment-tracking 129 s, model-optimization 127 s; the other 21 modules
took 2-97 s each; 2857 s summed. Where it goes, from profiles of the two slowest modules:
imports paid once per interpreter (computer-vision: 111 s, of which `import torchmetrics`
took 72 s because it imports `transformers` when installed; nlp-tasks: 209 s), then real
model compute (computer-vision: DeepLabV3/Faster R-CNN ResNet-50 forward and backward steps;
nlp-tasks: 9 beam-search `generate` calls in the summarization step, 335 s of 425 s). Every
module keeps its heavy imports inside tests or `setUpClass`, so `discover` in one process
(as CI runs it) pays each import once, and the per-module sum overstates a single run. Setting
`OMP_NUM_THREADS=1` did not help (nlp-tasks 502 s against 525 s, within noise), so it is not set.

**Last observed CI run:** 2026-10-28, the `examples` job on Linux, Python 3.13.17: 338 tests,
2 errors, 1 skipped. Both errors came from the pandas 3.1.0 bump and are fixed; pandas is now
pinned to 2.2.3. No run with the current pins has been observed (`docs/KNOWN-ISSUES.md` #4).

## Watchdog for hung blocks

`ML_AI_SKILLS_EXAMPLE_TIMEOUT=<seconds>` arms a watchdog around each `run_blocks` call
(`harness.block_watchdog`). It covers only the `exec` of the selected blocks, not test setup
or imports done outside `run_blocks`. If one call runs longer than the limit, `faulthandler`
writes every thread's traceback to the real stderr and exits the whole test process with
status 1. That is a process exit, not a test failure: the tests after it do not run, and
unittest prints no summary. The timer is cancelled when the call returns or raises.

- Unset, empty, `0` or a negative number: no watchdog.
- A value that is not a finite number (`abc`, `inf`, `nan`): the `run_blocks` call raises
  `AssertionError`, so a typo cannot switch the watchdog off silently.
- `faulthandler` has one timer per process and no way to read it back. Arming the watchdog
  replaces any timer another layer set (for example pytest's `faulthandler_timeout`), and the
  timer is not restored afterwards. Leave the variable unset if something else owns that
  timer.

The CI `examples` job sets it to `900`. That limit does not come from a CI measurement. The
slowest step profiled locally, nlp-tasks' summarization step, took about 335 s on a 12-core
Windows machine ("Runtime" above); a slower runner could come closer to the limit. No run of
the job with the variable set has been observed. `TestBlockWatchdog` in
`tests/examples/test_harness.py` checks the parsing, the arming and cancelling, and that a
hung block exits a child process with a traceback.

## What is real and what is faked

Each module's docstring is the authority; this is the summary as of 2026-10-29. "Real" means
the installed library executes; it still runs on tiny synthetic data, so values (accuracy,
mAP, summaries) are meaningless and only shapes, contracts and failure behaviour are asserted.

| Module | Real | Faked, stubbed or replaced | Exercised only by an opt-in integration test |
|---|---|---|---|
| agent-evaluation, rag-evaluation, ml-problem-framing, model-evaluation, supervised-/unsupervised-learning, feature-engineering, hyperparameter-tuning, explainability, python-for-ml, recommender-systems, data-preprocessing | everything the blocks import (scikit-learn, optuna, shap, lime, implicit, surprise, pandas, numpy) | only the user's data (synthetic) and iteration counts (`replace=`) | none needed |
| time-series | pandas, statsmodels, pmdarima, prophet | the walk-forward block gets a recording fake `pm` | none |
| pytorch-patterns, training-deep-models | torch on CPU | `num_workers=4` -> 0 (harness artifact) | CUDA: `pin_memory`, fp16 autocast + `GradScaler` (`test_gpu.py`) |
| model-optimization | torch quantization, ONNX export, onnxruntime | nothing | none |
| computer-vision | torchvision detection/segmentation, albumentations, torchmetrics | `weights="DEFAULT"` -> random init (no download); compute-size knobs | pretrained weights: not covered |
| cnn-vision (prose only) | `torch.nn.Conv2d` output sizes, `padding="same"` rules | nothing | TF/Keras `"same"` = `ceil(H/S)` (`test_tensorflow.py`) |
| nlp-tasks | spaCy (blank), transformers pipelines/Trainer/`generate`, datasets, seqeval, rouge-score | every checkpoint -> tiny random BERT/BART with offline tokenizers; IMDB -> synthetic label-sorted split; `en_core_web_sm` -> `spacy.blank` | `dslim/bert-base-NER` labels, `bart-large-cnn` 1024-token limit and chunking, IMDB sort order (`test_hf_hub.py`); `en_core_web_sm` labels (`SPACY`) |
| fine-tuning-llms | datasets, transformers, peft, trl | the Llama checkpoint -> tiny random Llama; `bf16=False` | 4-bit/bf16 on GPU: not covered |
| llm-evaluation | transformers text-classification pipeline, the `anthropic` SDK's signature and types | NLI checkpoint -> tiny random BERT; Anthropic client -> fake | real NLI label spelling and grounding (`test_hf_hub.py`); real Claude calls: not covered |
| rag-pipeline | faiss-cpu, langchain-text-splitters, sentence-transformers (local static model), `anthropic` SDK over a mock transport | `fakes.py`: hashing embedder, cross-encoder, numpy FAISS (checked against real faiss), fake Anthropic client | real embedding models / API: not covered |
| ml-monitoring | evidently 0.7, pandas, `requests` (to show the pre-fix error) | `requests.post` recorder; `airflow` signature stub | none |
| experiment-tracking | MLflow tracking/registry/CLI on SQLite, scikit-learn, matplotlib | `infer_pip_requirements` (speed); `wandb` fake | none |
| ci-cd-for-ml | pytest (in-process), PyYAML, MLflow (opt-in class) | `src.pipeline` fake; `fake_mlflow` registry; workflows parsed, not run | GitHub Actions: not covered |
| model-deployment | FastAPI app via `TestClient`, `torch.load(weights_only=True)`, bash canary scripts | `kubectl` -> recording shell function; Dockerfile/YAML checked structurally | image build + hardened `docker run` + smoke test (`test_docker_deploy.py`); API-server dry-run of the manifests (`test_k8s_manifests.py`); TorchServe: not covered (block is `# illustrative`) |
| data-pipelines (+ `test_data_pipelines_object_store.py`) | pandas/pyarrow, SQLAlchemy on SQLite, pandera, fsspec `memory://` for the object-store branch | `airflow` and `feast` stubs that accept only the real keyword names; `memory://` shares the skill's `"://"` branch with `s3://` but not its I/O client | `s3://` against a local moto server, through pyarrow's native S3 client (the route pandas takes for the skill's calls) and through the s3fs fallback (`test_s3_publish.py`); real Airflow 3 `dags test`; Feast apply/materialize/online read (`test_airflow_feast.py`) |

## Integration tests (opt-in)

`tests/integration/` holds tests for what the offline suite fakes. Each class is gated by an
environment variable through `tests/integration/gate.py`: unset, it is skipped with a reason
naming the variable; set to `1`, a missing module, executable, device or cluster context is a
**failure**, so an opted-in run cannot pass by skipping. `test_gate.py` checks that rule and
needs no opt-in. The CI `unit` job runs `integration.test_gate` (stdlib only); no CI job runs
the other modules in `tests/integration/`. A class that acquires resources in `setUpClass`
registers each cleanup with `addClassCleanup` as soon as it holds the resource, because
unittest does not call `tearDownClass` when `setUpClass` raises.

```bash
ML_AI_SKILLS_INTEGRATION_TF=1 python -m unittest discover -s tests/integration -t tests -v
```

| Variable | Module | Checks | Last observed |
|---|---|---|---|
| `ML_AI_SKILLS_INTEGRATION_HF` | `test_hf_hub.py` | llm-evaluation step 5 with `cross-encoder/nli-deberta-v3-base` (entailment label found; entailed vs contradicted answer); `dslim/bert-base-NER` `id2label` is the CoNLL PER/ORG/LOC/MISC set; `bart-large-cnn` 1024-token limit and every `summarize_long` chunk fits after re-encoding (real tokenizer, stub model); IMDB splits stored sorted by label | not run: huggingface.co did not resolve on the test machine (2026-10-29) |
| `ML_AI_SKILLS_INTEGRATION_SPACY` | `test_hf_hub.py` | `en_core_web_sm` has the 18 OntoNotes entity types | not run |
| `ML_AI_SKILLS_INTEGRATION_GPU` | `test_gpu.py` | training-deep-models on CUDA (AMP on, `GradScaler` enabled with a finite scale, fp16 activations); pytorch-patterns pins memory with CUDA | not run: no CUDA device |
| `ML_AI_SKILLS_INTEGRATION_TF` | `test_tensorflow.py` | Keras `Conv2D` `"same"` = `ceil(H/S)`, `"valid"` = the formula with `P=0`, the extra padding row goes after the input; `tensorflow.contrib` is gone | 2026-10-29, Windows 11, CPython 3.13.2, tensorflow 2.21.0 / keras 3.15.1 (CPU), scratch venv: 5 tests OK |
| `ML_AI_SKILLS_INTEGRATION_S3` | `test_s3_publish.py` | data-pipelines step 1 with an `s3://` data dir against moto's server on 127.0.0.1 (never a real bucket; real credential files are hidden). The skill calls `to_parquet`/`read_parquet` without `storage_options`, so pandas 2.2 resolves the URL with `pyarrow.fs.FileSystem.from_uri` and the I/O goes through pyarrow's native S3 client, which finds moto through `AWS_ENDPOINT_URL`. A second case forces pandas' fsspec fallback (s3fs). Each case asserts which client every one of the skill's calls used. The test lists and reads back through s3fs | 2026-10-29, Windows 11, CPython 3.13.2, moto 5.2.5, s3fs 2026.9.0, botocore 1.43.117, pandas 2.2.3, pyarrow 25.0.1: 2 tests OK (pyarrow S3 route, s3fs fallback). Swapping the expected clients fails both, with "went through pyarrow._s3fs.S3FileSystem" and "went through s3fs.core.S3FileSystem" |
| `ML_AI_SKILLS_INTEGRATION_DOCKER` | `test_docker_deploy.py` | model-deployment image built from the skill's Dockerfile with hash-pinned requirements, run with the step-3 hardening flags on loopback; `/readyz` 200, authenticated `/predict` 200, unauthenticated 401, uid 10001; a wrong `MODEL_SHA256` stops the container | not run: no docker |
| `ML_AI_SKILLS_INTEGRATION_K8S` (+ `ML_AI_SKILLS_K8S_CONTEXT`) | `test_k8s_manifests.py` | `kubectl apply --dry-run=server --validate=strict` of the canary Deployment and of the probe/securityContext snippet in a stable Deployment; persists nothing | not run: no kubectl/cluster |
| `ML_AI_SKILLS_INTEGRATION_AIRFLOW` | `test_airflow_feast.py` | steps 1-2 as a DAG file under Airflow 3: no import errors, `airflow dags test` writes the `as_of=2026-04-01` partition with a 30-day window | not run: Airflow does not support native Windows |
| `ML_AI_SKILLS_INTEGRATION_FEAST` | `test_airflow_feast.py` | publish -> `feast apply` -> `feast materialize-incremental` -> step 5 online read returns the pipeline's values for customer 42 (the `FileSource` points at the publish directory) | 2026-10-29, Windows 11, CPython 3.13.2, feast 0.67.0, dask 2026.8.0, pandas 2.2.3, pyarrow 25.0.1, scratch venv: 1 test OK |

moto and s3fs are not in `requirements-examples.txt`; install them into a separate venv for
the S3 test (`pip install "moto[server]" s3fs` plus the pandas/pyarrow/SQLAlchemy pins).

## Coverage

Set `ML_AI_SKILLS_EXAMPLE_COVERAGE=<path>` to append one JSON line per successful
`run_blocks` call (schema `ml-ai-skills/example-coverage` version 2). Each line has `skill`,
`blocks`, `total_blocks`, `skill_sha256` (LF-normalised SHA-256 of the SKILL.md whose blocks
ran), `git_revision`, `git_dirty` (tracked files only) and `git_untracked_files` (the number of
untracked, non-ignored files other than the coverage file), the three git fields being `null`
outside a git checkout of this repo, plus `python`, `platform`, `recorded_at`, `env` and the
calling `test_id` / `test_module`. A line is written only after every selected block ran
without raising.

`scripts/check_example_coverage.py` fails on any runnable block not covered by a record for
the current SKILL.md, and on any stale or malformed line (CI truncates the file first).
`scripts/quality_report.py` folds the lines into its report (`--run-examples PYTHON`, which
sets the variable itself, or `--example-coverage FILE`). It counts only non-illustrative block
indices from records whose `skill_sha256` and `total_blocks` match the current SKILL.md. Full
current coverage wins over leftover stale lines, which are listed. Otherwise a stale line gives
`coverage-stale`, and coverage with no usable record is `coverage-unattributable`.
`--require-coverage-revision` also rejects records without a git revision. Coverage alone is
reported as `coverage-only-outcome-unknown`. A skill is `executed-and-passed` only when
example test results (`--run-examples`, or `--test-results` from `--collect-tests --suite
examples`; several files are merged) recorded at the same SKILL.md sha256 show its attributed
tests passing. It is `example-tests-failing` when any of them failed in a current or untied
results file (a failure recorded only against another SKILL.md sha256 is
`example-results-stale`). Test modules are attributed to skills by a module-level
`SKILL`/`SKILLS` declaration (`SKILLS = ()` means no skill), else by the slug they pass to
`run_blocks`/`python_blocks` or their `SLUG` constant, plus the `test_module` the harness
records. The module name is only a fallback, and unattributed modules are listed. Opt-in slow
tests use `@harness.slow(...)` and run under `ML_AI_SKILLS_SLOW_EXAMPLES=1` or
`ML_AI_SKILLS_REQUIRE_EXAMPLE_DEPS=1`. `quality_report.py --run-examples PYTHON
--require-example-deps` reproduces the CI environment variables (not CI's Linux lock). In the
2026-10-28 run, all runnable blocks of all 26
code-bearing skills were executed at least once. A block that runs is evidence the
code executes against the pinned library versions with the stubbed inputs — it is
not evidence that the advice is optimal, or that it behaves identically against a
real downloaded model or remote service.
