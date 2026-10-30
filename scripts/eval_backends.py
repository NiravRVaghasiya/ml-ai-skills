"""Model backends for scripts/run_evals.py: who answers (or judges) an eval prompt.

Stdlib-only, like the rest of scripts/. Every backend implements one call,
`complete(Request) -> Completion`, and one self-description, `describe()`
(configuration only, never a secret). See docs/EVALUATION.md.

* ``anthropic``  - Messages API over urllib. The API key is read from an
  environment variable by the caller and only ever placed in a request header.
  Retries 408/429/5xx/529 and connection errors with bounded, jittered backoff.
* ``claude-cli`` - shells out to ``claude -p --output-format json`` with a
  bounded timeout, in a fresh temporary working directory. It uses whatever
  authentication the CLI is configured with; run_evals never reads or records it.
* ``replay``     - deterministic and offline: answers come from a recorded JSONL
  file keyed by (case_id, condition, sample). For tests and for re-running the
  grading pipeline on saved responses.
* ``CallableBackend`` - in-process fake for unit tests.

Every completion carries an explicit status. Only ``ok`` and ``refusal`` are
model outcomes; ``error``, ``timeout``, ``rate-limited``, ``server-error`` and
``budget-exhausted`` are failures to obtain one. None of them is ever a pass.
"""
from __future__ import annotations

import http.client
import json
import math
import random
import re
import shutil
import subprocess
import tempfile
import time
import urllib.error
import urllib.request
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable

# --------------------------------------------------------------------------
# Statuses
# --------------------------------------------------------------------------

COMPLETED_STATUSES = ("ok", "refusal")  # the model produced an outcome; a resumed run reuses these
FAILURE_STATUSES = ("error", "timeout", "rate-limited", "server-error", "budget-exhausted")
ATTEMPT_STATUSES = COMPLETED_STATUSES + FAILURE_STATUSES


@dataclass(frozen=True)
class Request:
    system: str
    user: str
    max_tokens: int
    temperature: float | None = None
    case_id: str = ""
    condition: str = ""
    sample: int = 0
    role: str = "answer"  # answer | judge


@dataclass
class Completion:
    status: str
    text: str = ""
    model_reported: str | None = None
    stop_reason: str | None = None
    usage: dict | None = None
    cost_usd: float | None = None  # observed: reported by the backend or usage x configured prices
    error: str | None = None
    attempts: int = 1
    duration_s: float | None = None
    meta: dict = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> Completion:
        known = {k: d[k] for k in cls.__dataclass_fields__ if k in d}
        return cls(**known)


# --------------------------------------------------------------------------
# Secret redaction (defence in depth: keys are never put in reports to begin with)
# --------------------------------------------------------------------------

SECRET_NAME_RE = re.compile(r"KEY|TOKEN|SECRET|PASSWORD|CREDENTIAL|AUTH", re.IGNORECASE)
REDACTED = "[REDACTED]"


def secret_values(env: dict, extra_names: tuple[str, ...] = ()) -> list[str]:
    """Values of environment variables whose names look secret (>= 8 chars), longest first."""
    vals = {v for k, v in env.items()
            if isinstance(v, str) and len(v) >= 8 and (SECRET_NAME_RE.search(k) or k in extra_names)}
    return sorted(vals, key=len, reverse=True)


def redact(text: str, secrets: list[str]) -> str:
    for s in secrets:
        text = text.replace(s, REDACTED)
        escaped = json.dumps(s)[1:-1]  # the form a secret takes inside a JSON string
        if escaped != s:
            text = text.replace(escaped, REDACTED)
    return text


# --------------------------------------------------------------------------
# Cost accounting
# --------------------------------------------------------------------------

def estimate_input_tokens(text: str) -> int:
    """Deliberately pessimistic token estimate: one token per two UTF-8 bytes, plus overhead.

    English prose is closer to four characters per token, so this over-reserves
    budget; it is an estimate, not a tokenizer.
    """
    return math.ceil(len(text.encode("utf-8")) / 2) + 32


@dataclass(frozen=True)
class Pricing:
    """User-supplied prices in USD per million tokens. No price table is shipped:
    prices change, and a stale built-in number would make the ceiling meaningless."""
    input_per_mtok: float
    output_per_mtok: float

    def worst_case(self, req: Request) -> float:
        est_in = estimate_input_tokens(req.system + req.user)
        return (est_in * self.input_per_mtok + req.max_tokens * self.output_per_mtok) / 1e6

    def actual(self, usage: dict | None) -> float | None:
        if not isinstance(usage, dict):
            return None
        def n(key: str) -> int:
            v = usage.get(key)
            return v if isinstance(v, int) and not isinstance(v, bool) else 0
        tokens_in = n("input_tokens") + n("cache_creation_input_tokens") + n("cache_read_input_tokens")
        return (tokens_in * self.input_per_mtok + n("output_tokens") * self.output_per_mtok) / 1e6

    def to_dict(self) -> dict[str, float]:
        return {"input_usd_per_mtok": self.input_per_mtok, "output_usd_per_mtok": self.output_per_mtok}


class Budget:
    """A spend ceiling shared by every call of one invocation (answers and judge).

    A call is made only if `spent + worst_case <= ceiling`. Without a per-call
    worst case (an unpriced backend), calls stop once `spent >= ceiling`. A paid
    call whose cost is unknown (no prices, and the backend reported no cost)
    stops the run: spend can no longer be bounded, so every later attempt is
    budget-exhausted. `ceiling=None` is allowed only for free backends (replay,
    fakes); run_evals refuses paid backends without one.
    """

    def __init__(self, ceiling_usd: float | None):
        self.ceiling = ceiling_usd
        self.spent = 0.0
        self.calls = 0
        self.uncosted_calls = 0
        self.exhausted = False
        self.stop_reason: str | None = None

    def can_afford(self, worst_case: float | None) -> bool:
        if self.exhausted:
            return False
        if self.ceiling is None:
            return True
        ok = self.spent < self.ceiling if worst_case is None else self.spent + worst_case <= self.ceiling
        if not ok:
            self.exhausted = True
        return ok

    def charge(self, cost: float | None, *, paid: bool = False) -> None:
        self.calls += 1
        if cost is not None:
            self.spent += cost
            return
        self.uncosted_calls += 1
        if paid and self.ceiling is not None and not self.exhausted:
            self.exhausted = True
            self.stop_reason = ("a paid call reported no cost and no prices were given, so spend can no longer be "
                                "bounded by the ceiling; give --price-* (or --judge-price-*) to go further")

    def to_dict(self) -> dict[str, Any]:
        return {"ceiling_usd": self.ceiling, "spent_usd_estimate": round(self.spent, 6), "calls": self.calls,
                "uncosted_calls": self.uncosted_calls, "exhausted": self.exhausted, "stop_reason": self.stop_reason,
                "scope": "this invocation only: a --resume starts from $0 and does not count earlier spend",
                "note": "spend is computed from reported usage x the prices you supplied (or the cost the "
                        "claude CLI reports); it is an estimate, not an invoice"}


# --------------------------------------------------------------------------
# Anthropic Messages API over stdlib urllib
# --------------------------------------------------------------------------

API_URL = "https://api.anthropic.com/v1/messages"
DEFAULT_ANTHROPIC_VERSION = "2023-06-01"
RETRY_STATUSES = frozenset({408, 429, 500, 502, 503, 504, 529})

# transport(url, body_bytes, headers, timeout) -> (status, lowercased headers, body_bytes)
Transport = Callable[[str, bytes, dict, float], tuple[int, dict, bytes]]


class TransportError(Exception):
    """Connection-level failure (DNS, reset, timeout): retryable."""


def _error_kind(status: int | None, message: str) -> str:
    if status == 429:
        return "rate-limited"
    if status == 408 or (status is None and "timeout" in message.lower()):
        return "timeout"
    if status is not None and (500 <= status < 600 or status == 529):
        return "server-error"
    return "error"


class ApiError(Exception):
    def __init__(self, message: str, status: int | None = None, kind: str | None = None):
        super().__init__(message)
        self.status = status
        self.kind = kind or _error_kind(status, message)


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Refuse every redirect. The stock handler re-sends all non-content headers - x-api-key
    included - to the Location, even on another host or plain http://. The Messages API
    never redirects, so a 3xx surfaces as a non-retryable HTTP error instead."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None  # -> HTTPDefaultErrorHandler raises HTTPError(code)


# Same handlers as urllib.request.urlopen's default opener, minus redirect following.
_OPENER = urllib.request.build_opener(_NoRedirectHandler)


def urllib_transport(url: str, body: bytes, headers: dict, timeout: float) -> tuple[int, dict, bytes]:
    if not url.startswith("https://"):  # never let a file:/ or custom scheme reach the opener
        raise ValueError(f"refusing non-https Messages API URL: {url!r}")
    req = urllib.request.Request(url, data=body, headers=headers, method="POST")  # noqa: S310 - https enforced above
    try:
        with _OPENER.open(req, timeout=timeout) as resp:
            return resp.status, {k.lower(): v for k, v in resp.headers.items()}, resp.read()
    except urllib.error.HTTPError as e:
        try:
            body = e.read() or b""
        except (OSError, http.client.HTTPException):  # a truncated error body still has its status
            body = b""
        return e.code, {k.lower(): v for k, v in (e.headers or {}).items()}, body
    # HTTPException covers IncompleteRead (body cut off mid-read), BadStatusLine and LineTooLong,
    # none of which is an OSError: without it one flaky connection would crash the whole run.
    except (urllib.error.URLError, TimeoutError, ConnectionError, OSError, http.client.HTTPException) as e:
        raise TransportError(type(e).__name__ + ": " + str(getattr(e, "reason", e))) from e
    # http.client rejects an invalid header value (e.g. a key with a trailing CR/LF) with a
    # ValueError whose message quotes the value: report the type only, never the text.
    except (ValueError, UnicodeError) as e:
        raise TransportError(f"{type(e).__name__}: invalid request (a header value was rejected; "
                             "check the API key for whitespace or control characters)") from None


class MessagesClient:
    """Minimal Messages API client: JSON POST, bounded retries with backoff.

    Retries 408/429/5xx/529 and connection errors; honours a numeric
    `retry-after` header (capped). The API key goes in a header only and is
    never included in exceptions, reports, or logs. `unconfirmed_attempts`
    counts connection-level failures (timeouts, resets, truncated bodies): the
    server may have processed, and billed, those requests.
    """

    def __init__(self, api_key: str, *, anthropic_version: str = DEFAULT_ANTHROPIC_VERSION, timeout: float = 120.0,
                 max_retries: int = 4, backoff_base: float = 1.0, backoff_cap: float = 60.0,
                 transport: Transport | None = None, sleep: Callable[[float], None] = time.sleep,
                 rng: Callable[[], float] = random.random, url: str = API_URL):
        self._api_key = api_key
        self.anthropic_version = anthropic_version
        self.timeout = timeout
        self.max_retries = max_retries
        self.backoff_base = backoff_base
        self.backoff_cap = backoff_cap
        self.transport = transport or urllib_transport
        self.sleep = sleep
        self.rng = rng
        self.url = url
        self.attempts = 0  # total HTTP attempts, for the report
        self.unconfirmed_attempts = 0  # attempts that may have been billed without a usable response

    def __repr__(self) -> str:  # never show the key, even in a debugger or traceback
        return f"MessagesClient(url={self.url!r}, anthropic_version={self.anthropic_version!r})"

    def _delay(self, attempt: int, headers: dict) -> float:
        retry_after = headers.get("retry-after")
        if retry_after is not None:
            try:
                return min(max(float(retry_after), 0.0), self.backoff_cap)
            except ValueError:
                pass
        # Full exponential backoff with jitter so parallel runners don't sync up.
        return min(self.backoff_cap, self.backoff_base * (2 ** attempt)) * (0.5 + self.rng() / 2)

    def create(self, body: dict) -> dict:
        data = json.dumps(body).encode("utf-8")
        headers = {"x-api-key": self._api_key, "anthropic-version": self.anthropic_version,
                   "content-type": "application/json"}
        last = "no attempt made"
        status: int | None = None
        for attempt in range(self.max_retries + 1):
            self.attempts += 1
            try:
                status, resp_headers, raw = self.transport(self.url, data, headers, self.timeout)
            except TransportError as e:
                last, resp_headers, status = f"connection error: {e}", {}, None
                self.unconfirmed_attempts += 1
            else:
                if status == 200:
                    try:
                        msg = json.loads(raw.decode("utf-8"))
                    except (UnicodeDecodeError, json.JSONDecodeError) as e:
                        raise ApiError(f"HTTP 200 with a non-JSON body: {e}", status, "error") from e
                    if not isinstance(msg, dict):
                        raise ApiError("HTTP 200 with a JSON body that is not an object", status, "error")
                    return msg
                last = f"HTTP {status}: {_api_error_message(raw)}"
                if status not in RETRY_STATUSES:
                    raise ApiError(last, status, "error")
            if attempt < self.max_retries:
                self.sleep(self._delay(attempt, resp_headers))
        raise ApiError(f"gave up after {self.max_retries + 1} attempt(s); last error: {last}", status)


def _api_error_message(raw: bytes) -> str:
    try:
        err = json.loads(raw.decode("utf-8")).get("error") or {}
        return f"{err.get('type', 'error')}: {err.get('message', '')}"[:300]
    except (UnicodeDecodeError, json.JSONDecodeError, AttributeError):
        return raw[:200].decode("utf-8", "replace")


def response_text(message: dict) -> str:
    """Concatenate `text` blocks; thinking/tool blocks are not part of the graded answer."""
    return "".join(b.get("text", "") for b in message.get("content") or []
                   if isinstance(b, dict) and b.get("type") == "text")


class Backend:
    """Interface. `paid` backends need a cost ceiling (run_evals enforces it)."""
    provider = "abstract"
    paid = False

    def __init__(self, model: str, pricing: Pricing | None = None):
        self.model = model
        self.pricing = pricing

    def complete(self, req: Request) -> Completion:  # pragma: no cover - interface
        raise NotImplementedError

    def worst_case_cost(self, req: Request) -> float | None:
        return self.pricing.worst_case(req) if self.pricing else None

    def observed_cost(self, c: Completion) -> float | None:
        if c.cost_usd is not None:
            return c.cost_usd
        return self.pricing.actual(c.usage) if self.pricing else None

    def identity(self) -> dict[str, Any]:
        """What must be equal for a recorded answer to be reused on --resume."""
        return {"provider": self.provider, "model": self.model}

    def describe(self) -> dict[str, Any]:
        return {**self.identity(), "pricing": self.pricing.to_dict() if self.pricing else None}


class AnthropicBackend(Backend):
    provider = "anthropic"
    paid = True

    def __init__(self, client: MessagesClient, model: str, pricing: Pricing | None = None, api_key_env: str = ""):
        super().__init__(model, pricing)
        self.client = client
        self.api_key_env = api_key_env

    def complete(self, req: Request) -> Completion:
        body: dict[str, Any] = {"model": self.model, "max_tokens": req.max_tokens, "system": req.system,
                                "messages": [{"role": "user", "content": req.user}]}
        if req.temperature is not None:
            body["temperature"] = req.temperature
        before, unconfirmed_before = self.client.attempts, self.client.unconfirmed_attempts
        start = time.monotonic()
        try:
            msg = self.client.create(body)
        except ApiError as e:
            return Completion(status=e.kind, error=str(e), attempts=self.client.attempts - before,
                              duration_s=round(time.monotonic() - start, 3),
                              meta={"http_status": e.status,
                                    "unconfirmed_attempts": self.client.unconfirmed_attempts - unconfirmed_before})
        stop = msg.get("stop_reason")
        c = Completion(status="refusal" if stop == "refusal" else "ok", text=response_text(msg),
                       model_reported=msg.get("model"), stop_reason=stop,
                       usage=msg.get("usage") if isinstance(msg.get("usage"), dict) else None,
                       attempts=self.client.attempts - before, duration_s=round(time.monotonic() - start, 3),
                       meta={"unconfirmed_attempts": self.client.unconfirmed_attempts - unconfirmed_before})
        c.cost_usd = self.pricing.actual(c.usage) if self.pricing else None
        return c

    def identity(self) -> dict[str, Any]:
        return {"provider": self.provider, "model": self.model, "endpoint": self.client.url,
                "anthropic_version": self.client.anthropic_version}

    def describe(self) -> dict[str, Any]:
        return {**super().describe(), "timeout_s": self.client.timeout, "max_retries": self.client.max_retries,
                "backoff_base_s": self.client.backoff_base, "backoff_cap_s": self.client.backoff_cap,
                "retry_statuses": sorted(RETRY_STATUSES), "api_key_env": self.api_key_env,
                "http_attempts": self.client.attempts, "http_client": "urllib (Python stdlib), redirects refused"}


# --------------------------------------------------------------------------
# claude CLI (opt-in; uses whatever auth the CLI has)
# --------------------------------------------------------------------------

# Flags intended to make the CLI behave like a bare model call: the eval's system prompt
# replaces Claude Code's own, no tools, no skills or slash commands (meant to keep an
# installed copy of these skills out of the without_skill arm), no MCP servers, no saved
# session. This isolation is UNVERIFIED: it was never checked against a live run. Without
# --bare, hooks from settings and installed plugins (for example SessionStart context) and
# user-level CLAUDE.md / rules files may still reach both arms. Add --claude-cli-arg=--bare
# where your auth mode allows it (it limits Anthropic auth to an API key or apiKeyHelper),
# or other isolation flags your build supports (e.g. --setting-sources).
CLI_BASE_ARGS = ("-p", "--output-format", "json", "--tools", "", "--disable-slash-commands",
                 "--strict-mcp-config", "--no-session-persistence")
CLI_STDERR_CHARS = 400

Runner = Callable[..., subprocess.CompletedProcess]


class ClaudeCliBackend(Backend):
    provider = "claude-cli"
    paid = True

    def __init__(self, model: str, *, command: list[str] | None = None, timeout: float = 300.0,
                 extra_args: tuple[str, ...] = (), pricing: Pricing | None = None, budget_flag: bool = True,
                 runner: Runner = subprocess.run):
        super().__init__(model, pricing)
        self.command = list(command or ["claude"])
        self.timeout = timeout
        self.extra_args = tuple(extra_args)
        self.budget_flag = budget_flag
        self.runner = runner
        self._version: str | None = None
        self.max_budget_for_next: float | None = None  # set by the caller from the run budget

    def worst_case_cost(self, req: Request) -> float | None:
        # The CLI has no max-tokens flag, so req.max_tokens bounds nothing here and a
        # price-based worst case would understate a call. Unknown: the run budget then
        # admits a call only while spent < ceiling, and a call whose cost the CLI does
        # not report stops the run (Budget.charge(None, paid=True)).
        return None

    def argv(self, system_prompt_file: str) -> list[str]:
        argv = [*self.command, *CLI_BASE_ARGS, "--model", self.model, "--system-prompt-file", system_prompt_file]
        if self.budget_flag and self.max_budget_for_next is not None:
            argv += ["--max-budget-usd", f"{max(self.max_budget_for_next, 0.0):.4f}"]
        return argv + list(self.extra_args)

    def complete(self, req: Request) -> Completion:
        workdir = Path(tempfile.mkdtemp(prefix="run_evals-cli-"))
        start = time.monotonic()
        try:
            sp = workdir / "system-prompt.txt"
            sp.write_text(req.system, encoding="utf-8", newline="\n")
            try:
                proc = self.runner(self.argv(str(sp)), input=req.user, capture_output=True, text=True,
                                   encoding="utf-8", errors="replace", timeout=self.timeout, cwd=str(workdir))
            except subprocess.TimeoutExpired:
                return Completion(status="timeout", error=f"claude CLI did not finish within {self.timeout:g}s",
                                  duration_s=round(time.monotonic() - start, 3))
            except OSError as e:  # the CLI never started: nothing was sent, nothing spent
                return Completion(status="error", error=f"could not start the claude CLI: {type(e).__name__}: {e}",
                                  meta={"sent": False})
            return self._parse(proc, round(time.monotonic() - start, 3))
        finally:
            shutil.rmtree(workdir, ignore_errors=True)

    def _parse(self, proc: subprocess.CompletedProcess, duration: float) -> Completion:
        data = None
        for line in reversed((proc.stdout or "").strip().splitlines()):
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(obj, dict) and obj.get("type") == "result":
                data = obj
                break
        if data is None:
            tail = (proc.stderr or proc.stdout or "")[-CLI_STDERR_CHARS:]
            return Completion(status="error", duration_s=duration,
                              error=f"claude CLI exit code {proc.returncode}, no JSON result; output tail: {tail!r}")
        usage = data.get("usage") if isinstance(data.get("usage"), dict) else None
        cost = data.get("total_cost_usd")
        cost = float(cost) if isinstance(cost, (int, float)) and not isinstance(cost, bool) else None
        models = data.get("modelUsage")
        model_reported = ",".join(sorted(models)) if isinstance(models, dict) and models else None
        subtype = str(data.get("subtype") or "")
        common = {"model_reported": model_reported, "usage": usage, "duration_s": duration,
                  "stop_reason": data.get("stop_reason") or subtype or None,
                  "meta": {"cli_subtype": subtype, "num_turns": data.get("num_turns"), "exit_code": proc.returncode}}
        if cost is None and self.pricing:
            cost = self.pricing.actual(usage)
        if data.get("is_error") or proc.returncode != 0:
            status = "budget-exhausted" if "budget" in subtype else "error"
            msg = data.get("result") if isinstance(data.get("result"), str) else ""
            return Completion(status=status, cost_usd=cost, error=f"claude CLI {subtype or 'error'}: {msg[:300]}",
                              **common)
        text = data.get("result") if isinstance(data.get("result"), str) else ""
        return Completion(status="ok", text=text, cost_usd=cost, **common)

    def version(self) -> str | None:
        if self._version is None:
            try:
                proc = self.runner([*self.command, "--version"], capture_output=True, text=True, encoding="utf-8",
                                   errors="replace", timeout=30)
                self._version = (proc.stdout or "").strip()[:200] or None
            except (OSError, subprocess.SubprocessError):
                self._version = None
        return self._version

    def identity(self) -> dict[str, Any]:
        return {"provider": self.provider, "model": self.model, "cli_args": list(CLI_BASE_ARGS) + list(self.extra_args)}

    def describe(self) -> dict[str, Any]:
        return {**super().describe(), "command": self.command, "cli_version": self.version(),
                "argv_template": [*self.command, *CLI_BASE_ARGS, "--model", self.model, "--system-prompt-file",
                                  "<temp file>", *(["--max-budget-usd", "<remaining budget>"] if self.budget_flag
                                                   else []), *self.extra_args],
                "prompt_via": "stdin", "timeout_s": self.timeout, "budget_flag": self.budget_flag,
                "auth": "whatever the claude CLI is configured with; run_evals does not read or record it",
                "not_controlled": ["max_tokens", "temperature (the CLI has no flags for them)"],
                "isolation": ("fresh temporary working directory; unverified against a live CLI. Without --bare, "
                              "hooks, plugins and user-level CLAUDE.md/rules/settings may still apply to both arms")}


# --------------------------------------------------------------------------
# Replay (deterministic, offline) and in-process fakes
# --------------------------------------------------------------------------

def load_replay(path: Path) -> tuple[list[dict], list[str]]:
    """Recorded rows: {case_id, condition, response[, sample, status, model_reported, stop_reason, usage]}."""
    rows, errors, seen = [], [], set()
    for n, line in enumerate(path.read_text(encoding="utf-8").split("\n"), 1):
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError as e:
            errors.append(f"{path.name}:{n}: not valid JSON ({e})")
            continue
        if not isinstance(row, dict) or not all(isinstance(row.get(k), str) for k in ("case_id", "condition")):
            errors.append(f"{path.name}:{n}: each line needs string fields case_id and condition")
            continue
        status = row.get("status", "ok")
        if status not in ATTEMPT_STATUSES:
            errors.append(f"{path.name}:{n}: status {status!r} not in {ATTEMPT_STATUSES}")
            continue
        if status in COMPLETED_STATUSES and not isinstance(row.get("response"), str):
            errors.append(f"{path.name}:{n}: a {status} row needs a string response")
            continue
        sample = row.get("sample", 0)
        if type(sample) is not int or sample < 0:
            errors.append(f"{path.name}:{n}: sample must be a non-negative integer")
            continue
        key = (row["case_id"], row["condition"], sample)
        if key in seen:
            errors.append(f"{path.name}:{n}: duplicate row for {key}")
            continue
        seen.add(key)
        rows.append({**row, "sample": sample, "status": status})
    return rows, errors


class ReplayBackend(Backend):
    provider = "replay"

    def __init__(self, rows: list[dict], *, model: str = "replay", source_sha256: str | None = None):
        super().__init__(model)
        self.rows = {(r["case_id"], r["condition"], r.get("sample", 0)): r for r in rows}
        self.source_sha256 = source_sha256

    def complete(self, req: Request) -> Completion:
        row = self.rows.get((req.case_id, req.condition, req.sample))
        if row is None:
            return Completion(status="error", attempts=0,
                              error=f"no replay record for {req.case_id} [{req.condition}] sample {req.sample}")
        return Completion(status=row.get("status", "ok"), text=row.get("response") or "",
                          model_reported=row.get("model_reported"), stop_reason=row.get("stop_reason"),
                          usage=row.get("usage") if isinstance(row.get("usage"), dict) else None,
                          error=row.get("error"), attempts=0)

    def identity(self) -> dict[str, Any]:
        return {"provider": self.provider, "model": self.model, "replay_file_sha256": self.source_sha256}


class CallableBackend(Backend):
    """Test double: `fn(Request)` returns a Completion or a plain answer string."""

    def __init__(self, fn: Callable[[Request], Completion | str], *, provider: str = "fake", model: str = "fake-model",
                 pricing: Pricing | None = None, paid: bool = False):
        super().__init__(model, pricing)
        self.fn = fn
        self.provider = provider
        self.paid = paid
        self.calls: list[Request] = []

    def complete(self, req: Request) -> Completion:
        self.calls.append(req)
        out = self.fn(req)
        return out if isinstance(out, Completion) else Completion(status="ok", text=out, model_reported=self.model)
