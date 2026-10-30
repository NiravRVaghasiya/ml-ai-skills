#!/usr/bin/env python3
"""Network freshness check (automated-doc-check). Scheduled job only; never in the default test suite.

Two checks, both flags for human review -- this script never edits a skill:

1. Reference URLs: every http(s) link under each skill's `## References`, plus
   every `source_url` in docs/claims.json. HEAD first, GET fallback (many
   sites reject HEAD), per-host rate limiting, retries with exponential
   backoff that honour `Retry-After` on 429/503, per-request timeouts capped
   by the remaining run budget and enforced by a watchdog, a per-host circuit
   breaker (--host-failure-limit, --breaker-on), and an identifying
   User-Agent. A reachable page is NOT proof its content still supports the
   skill; it only rules out link rot.
2. PyPI latest release: for every dist named in a parseable
   `version_constraints` entry, fetch https://pypi.org/pypi/<dist>/json and
   flag the entry when its specifier excludes the latest stable release (e.g.
   pinned below a new major). That is a prompt to re-test, not proof of breakage.

Usage:
    python scripts/verify_online.py                         # human summary
    python scripts/verify_online.py --json-out report.json  # + JSON report file
    python scripts/verify_online.py --json                  # JSON on stdout

Exit codes: 0 = no findings; 1 = findings for human review; 2 = network
unavailable (report status SKIPPED with the reason -- NOT a pass).
"""
from __future__ import annotations

import argparse
import datetime as dt
import email.utils
import json
import os
import re
import socket
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

sys.path.insert(0, str(Path(__file__).resolve().parent))
import check_freshness as cf  # noqa: E402
import skills_lib as sl  # noqa: E402

USER_AGENT = "ml-ai-skills-verify-online/1.0 (scheduled reference/version freshness check; python-urllib)"
PYPI_JSON = "https://pypi.org/pypi/{dist}/json"
PROBE_URL = "https://pypi.org/simple/"
RETRY_STATUSES = {429, 502, 503, 504}
BROKEN_STATUSES = {404, 410}
MAX_BODY = 32 * 1024 * 1024  # PyPI JSON for large projects (torch) is several MB


@dataclass
class HttpResult:
    url: str
    method: str
    status: int | None = None  # final HTTP status, None when no response arrived
    error: str | None = None
    error_kind: str | None = None  # "http" | "timeout" | "network" | "invalid-url" | "refused-scheme"
    attempts: int = 0
    body: bytes = b""
    headers: dict = field(default_factory=dict)  # lower-cased names

    @property
    def ok(self) -> bool:
        return self.status is not None and 200 <= self.status < 400


def _lower(headers) -> dict:
    """Header names are case-insensitive; store them lower-cased."""
    return {k.lower(): v for k, v in headers.items()} if headers else {}


class _SafeRedirect(urllib.request.HTTPRedirectHandler):
    """Follow redirects only to http(s); urllib would otherwise also follow ftp://."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if urllib.parse.urlsplit(newurl).scheme not in ("http", "https"):
            raise urllib.error.URLError(f"refusing redirect to non-http(s) URL {newurl!r}")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


_OPENER = urllib.request.build_opener(_SafeRedirect)


def default_opener(req: urllib.request.Request, timeout: float):
    return _OPENER.open(req, timeout=timeout)


def parse_retry_after(value: str | None, now: Callable[[], dt.datetime] | None = None) -> float | None:
    """Retry-After is either delta-seconds or an HTTP-date (RFC 9110 10.2.3)."""
    if not value:
        return None
    value = value.strip()
    if value.isdigit():
        return float(value)
    try:
        when = email.utils.parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=dt.timezone.utc)
    current = now() if now else dt.datetime.now(dt.timezone.utc)
    return max(0.0, (when - current).total_seconds())


THROTTLE_STATUSES = {429, 503}
# What the per-host circuit breaker can count. A request (after its own retries) is classified by
# failure_class(); classes listed in Fetcher.breaker_on add one to the host's consecutive-failure
# count, a definitive HTTP answer (2xx/3xx/4xx other than 429) resets it, and a failure of a class
# that is not listed leaves it unchanged.
BREAKER_CLASSES = ("throttle", "timeout", "server-error", "network")
# Extra wall-clock seconds the watchdog gives a request beyond its socket timeout before abandoning
# it. urllib's timeout applies to each blocking socket operation, not to the whole request, and DNS
# resolution has no timeout at all, so without the watchdog a slow-dripping or unresolvable host
# could hold one request far past --timeout and --max-seconds.
WATCHDOG_GRACE = 2.0
# A request is not started with less budget than this left (it would be cut off almost at once).
MIN_REQUEST_SECONDS = 1.0


def failure_class(res: HttpResult) -> str | None:
    """Breaker class of a finished request; None for a definitive HTTP answer (success or 4xx)."""
    if res.status in THROTTLE_STATUSES:
        return "throttle"
    if res.status is not None and 500 <= res.status <= 599:
        return "server-error"
    if res.error_kind in ("timeout", "network"):
        return res.error_kind
    return None


class Fetcher:
    """HTTP with per-host spacing, bounded retries, a run-wide time budget and a per-host circuit breaker.

    `opener`, `sleep`, `clock` are injectable for tests. `max_seconds` bounds the whole run (the
    scheduled job has a hard timeout; without a budget a throttling host could eat all of it and
    leave no report). Budget bound: a request is only started with at least MIN_REQUEST_SECONDS of
    budget left, its socket timeout is min(timeout, remaining budget), a pacing or retry wait that
    would cross the deadline is not slept, and a watchdog abandons a request that has not finished
    `watchdog_grace` seconds after its socket timeout. So `clock()` passes the deadline by at most
    `watchdog_grace` seconds (plus the caller's own work, e.g. writing the report). An abandoned
    request keeps running in a daemon thread until its socket gives up; its result is discarded.

    Circuit breaker: after `host_failure_limit` consecutive requests to one host end in a failure
    class listed in `breaker_on` (default: all of BREAKER_CLASSES, i.e. 429/503, other 5xx, timeouts
    and network errors such as DNS failures or resets, each after its retries), further requests to
    that host are not sent this run. 0 disables the breaker. HEAD and a GET fallback are two requests.
    """

    def __init__(self, opener=None, sleep=time.sleep, clock=time.monotonic, timeout: float = 15.0,
                 retries: int = 3, backoff: float = 2.0, max_wait: float = 120.0, min_interval: float = 1.0,
                 user_agent: str = USER_AGENT, max_seconds: float | None = None, host_failure_limit: int = 3,
                 breaker_on=BREAKER_CLASSES, watchdog_grace: float = WATCHDOG_GRACE):
        # Resolved at construction (not def) time so tests can patch module-level default_opener.
        self.opener = opener if opener is not None else default_opener
        self.sleep, self.clock = sleep, clock
        self.timeout, self.retries, self.backoff = timeout, retries, backoff
        self.max_wait, self.min_interval, self.user_agent = max_wait, min_interval, user_agent
        self.max_seconds = max_seconds if max_seconds and max_seconds > 0 else None
        self.deadline = clock() + self.max_seconds if self.max_seconds else None
        self.host_failure_limit = host_failure_limit
        unknown = set(breaker_on) - set(BREAKER_CLASSES)
        if unknown:
            raise ValueError(f"unknown circuit-breaker class(es) {sorted(unknown)}; choose from {BREAKER_CLASSES}")
        self.breaker_on = frozenset(breaker_on)
        self.watchdog_grace = watchdog_grace
        self._last_hit: dict[str, float] = {}
        self._host_failures: dict[str, int] = {}
        self.open_circuits: set[str] = set()
        self.deadline_hit = False
        self.requests = 0  # attempts made
        self.responses = 0  # attempts that got any HTTP status back (even 4xx/5xx)
        self.abandoned = 0  # attempts the watchdog gave up on

    def remaining(self) -> float:
        return float("inf") if self.deadline is None else self.deadline - self.clock()

    def _pace_wait(self, host: str) -> float:
        last = self._last_hit.get(host)
        return 0.0 if last is None else max(0.0, self.min_interval - (self.clock() - last))

    def _record_outcome(self, host: str, res: HttpResult) -> None:
        cls = failure_class(res)
        if cls is None:
            self._host_failures[host] = 0
        elif cls in self.breaker_on:
            self._host_failures[host] = self._host_failures.get(host, 0) + 1
            if self.host_failure_limit and self._host_failures[host] >= self.host_failure_limit:
                self.open_circuits.add(host)

    def _once(self, url: str, method: str, read_body: bool, timeout: float) -> HttpResult:
        # request() refuses non-http(s) schemes before calling _once().
        req = urllib.request.Request(url, method=method, headers={  # noqa: S310
            "User-Agent": self.user_agent, "Accept": "application/json" if read_body else "*/*"})
        try:
            with self.opener(req, timeout) as resp:  # body read inside try: it can time out too
                status = getattr(resp, "status", None) or resp.getcode()
                body = resp.read(MAX_BODY) if read_body else resp.read(1024 if method == "GET" else 0)
                return HttpResult(url, method, status, None, None, body=body, headers=_lower(resp.headers))
        except urllib.error.HTTPError as e:
            return HttpResult(url, method, e.code, f"HTTP {e.code}", "http", headers=_lower(e.headers))
        except (socket.timeout, TimeoutError) as e:
            return HttpResult(url, method, None, f"timeout after {timeout:g}s ({e})", "timeout")
        except urllib.error.URLError as e:
            reason = e.reason
            kind = "timeout" if isinstance(reason, (socket.timeout, TimeoutError)) else "network"
            return HttpResult(url, method, None, f"{type(reason).__name__}: {reason}", kind)
        except OSError as e:  # connection reset etc.
            return HttpResult(url, method, None, f"{type(e).__name__}: {e}", "network")
        except ValueError as e:  # http.client.InvalidURL and friends
            return HttpResult(url, method, None, f"invalid URL: {e}", "invalid-url")

    def _guarded_once(self, url: str, method: str, read_body: bool, timeout: float) -> HttpResult:
        """_once() under a wall-clock watchdog of timeout + watchdog_grace seconds (see class docstring)."""
        if self.watchdog_grace is None:
            return self._once(url, method, read_body, timeout)
        box: dict = {}
        done = threading.Event()

        def work():
            try:
                box["result"] = self._once(url, method, read_body, timeout)
            except BaseException as e:  # noqa: BLE001 - re-raised in the calling thread below
                box["error"] = e
            finally:
                done.set()

        threading.Thread(target=work, name="verify-online-request", daemon=True).start()
        limit = timeout + self.watchdog_grace
        if not done.wait(limit):
            self.abandoned += 1
            return HttpResult(url, method, None, f"timeout: no complete response within {limit:g}s (watchdog "
                                                 "abandoned the request)", "timeout")
        if "error" in box:
            raise box["error"]  # e.g. KeyboardInterrupt from a test opener
        return box["result"]

    def _out_of_budget(self, url: str, method: str, attempt: int) -> HttpResult:
        self.deadline_hit = True
        return HttpResult(url, method, None, f"not sent: run time budget of {self.max_seconds:g}s "
                                             "(--max-seconds) exhausted", "deadline", attempts=attempt)

    def request(self, url: str, method: str = "GET", read_body: bool = False) -> HttpResult:
        if urllib.parse.urlsplit(url).scheme not in ("http", "https"):
            return HttpResult(url, method, None, "only http(s) URLs are fetched", "refused-scheme")
        host = urllib.parse.urlsplit(url).netloc.lower()
        if host in self.open_circuits:
            return HttpResult(url, method, None, f"not sent: {host} ended {self.host_failure_limit} consecutive "
                                                 f"requests failed ({', '.join(sorted(self.breaker_on))}); "
                                                 "circuit open for this run", "circuit-open")
        attempt = 0
        while True:
            if self.remaining() < MIN_REQUEST_SECONDS:
                return self._out_of_budget(url, method, attempt)
            pace = self._pace_wait(host)
            if pace > 0:
                if pace + MIN_REQUEST_SECONDS > self.remaining():
                    return self._out_of_budget(url, method, attempt)
                self.sleep(pace)
            self._last_hit[host] = self.clock()
            attempt += 1
            res = self._guarded_once(url, method, read_body, min(self.timeout, self.remaining()))
            res.attempts = attempt
            self.requests += 1
            self.responses += res.status is not None
            retryable = res.status in RETRY_STATUSES or res.error_kind == "timeout"
            if not retryable or attempt > self.retries:
                self._record_outcome(host, res)
                return res
            wait = self.backoff * (2 ** (attempt - 1))
            if res.status in (429, 503):
                hinted = parse_retry_after(res.headers.get("retry-after"))
                if hinted is not None:
                    wait = hinted
            wait = min(wait, self.max_wait)
            if wait + MIN_REQUEST_SECONDS > self.remaining():  # the retry could not start in budget: stop here
                self.deadline_hit = True
                res.error = f"{res.error}; retry abandoned: run time budget (--max-seconds) exhausted"
                self._record_outcome(host, res)
                return res
            self.sleep(wait)


# HEAD outcomes after which a GET fallback would only repeat the same wait: the server is
# throttling or not answering, or we chose not to send at all. (405/403/404/5xx still fall back:
# many servers mishandle HEAD specifically.)
_NO_GET_FALLBACK = {"timeout", "deadline", "circuit-open", "refused-scheme", "invalid-url"}


# ---------------------------------------------------------------------------
# Inputs
# ---------------------------------------------------------------------------
_LINK_RE = re.compile(r"\[[^\]]+\]\((https?://[^\s)]+)\)")


def collect_urls(skills: list[sl.Skill], claims_path: Path | None) -> dict[str, list[str]]:
    """url -> sorted list of where it is cited (skill slugs / claim ids)."""
    urls: dict[str, set[str]] = {}
    for s in skills:
        for bullet in s.bullets_under("References"):
            for url in _LINK_RE.findall(bullet):
                urls.setdefault(url, set()).add(s.slug)
    if claims_path and claims_path.is_file():
        data = json.loads(claims_path.read_text(encoding="utf-8"))
        for c in data.get("claims", []):
            if isinstance(c, dict) and isinstance(c.get("source_url"), str):
                urls.setdefault(c["source_url"], set()).add(f"claim:{c.get('id')}")
    return {u: sorted(v) for u, v in sorted(urls.items())}


def collect_constraints(skills: list[sl.Skill]) -> dict[str, list[dict]]:
    """dist -> [{skill, specifier, entry}] for parseable version_constraints entries."""
    out: dict[str, list[dict]] = {}
    for s in skills:
        for entry in s.frontmatter.get("version_constraints") or []:
            c = cf.parse_constraint(entry)
            if c.status == "parsed" and c.dist:
                out.setdefault(c.dist, []).append({"skill": s.slug, "specifier": c.spec_text, "entry": c.raw})
    return dict(sorted(out.items()))


# ---------------------------------------------------------------------------
# Checks
# ---------------------------------------------------------------------------
def utc_now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def check_url(fetcher: Fetcher, url: str, acknowledged=(), today: dt.date | None = None,
              ack_max_age_days: int | None = None, now: Callable[[], dt.datetime] = utc_now) -> dict:
    """Check one URL. An acknowledgement applies up to and including its expires_on, judged against
    `today` (default: now()'s UTC date; both injectable). ack_max_age_days, if given, is an extra cap."""
    head = fetcher.request(url, "HEAD")
    retry_with_get = not head.ok and head.status not in THROTTLE_STATUSES and head.error_kind not in _NO_GET_FALLBACK
    res = fetcher.request(url, "GET") if retry_with_get else head
    if res.ok:
        verdict = "ok"
    elif res.status in BROKEN_STATUSES:
        verdict = "broken"
    else:
        verdict = "inconclusive"  # 401/403 bot walls, 429/5xx after retries, timeouts, DNS, budget: needs a human
    out = {"url": url, "verdict": verdict, "http_status": res.status, "method": res.method,
           "error": res.error, "error_kind": res.error_kind,
           "attempts": head.attempts + (0 if res is head else res.attempts),
           "head_status": head.status}
    if verdict == "inconclusive":
        ack = next((a for a in acknowledged if a.matches(url, res.status)), None)
        if ack is not None:
            today = today if today is not None else now().date()
            lapsed = ack.lapsed(today) or (ack_max_age_days is not None
                                          and (today - ack.acknowledged_on).days > ack_max_age_days)
            out["acknowledgement"] = {"reason": ack.reason, "acknowledged_on": ack.acknowledged_on.isoformat(),
                                      "expires_on": ack.expires_on.isoformat(), "lapsed": lapsed}
            if not lapsed:  # a lapsed acknowledgement surfaces again so someone re-confirms it
                out["verdict"] = "acknowledged"
    return out


def latest_stable(payload: dict) -> str | None:
    """Highest non-yanked, non-pre-release version in a PyPI JSON payload; falls back to info.version."""
    best = None
    for ver, files in (payload.get("releases") or {}).items():
        if not files or all(f.get("yanked") for f in files):
            continue
        try:
            v = cf.Version.parse(ver)
        except cf.InvalidVersion:
            continue
        if v.is_prerelease:
            continue
        if best is None or v > best:
            best = v
    if best is not None:
        return best.text
    return (payload.get("info") or {}).get("version")


def check_package(fetcher: Fetcher, dist: str, uses: list[dict]) -> dict:
    res = fetcher.request(PYPI_JSON.format(dist=urllib.parse.quote(dist)), "GET", read_body=True)
    out = {"dist": dist, "uses": uses, "latest": None, "verdict": "ok", "error": None, "flags": []}
    if res.status == 404:
        out.update(verdict="broken", error="not found on PyPI (wrong dist name?)")
        return out
    if not res.ok:
        out.update(verdict="inconclusive", error=res.error or f"HTTP {res.status}")
        return out
    try:
        latest = latest_stable(json.loads(res.body.decode("utf-8")))
    except (ValueError, UnicodeDecodeError) as e:
        out.update(verdict="inconclusive", error=f"unreadable PyPI JSON: {e}")
        return out
    out["latest"] = latest
    if latest is None:
        out.update(verdict="inconclusive", error="PyPI lists no stable release")
        return out
    for use in uses:
        specs = cf.parse_specifier_set(use["specifier"])
        if specs and not cf.contains(specs, latest):
            out["flags"].append({**use, "message": f"{dist}{use['specifier']} excludes the latest release "
                                                   f"{latest}; re-test against it and update the constraint "
                                                   "or document why it stays pinned"})
    if out["flags"]:
        out["verdict"] = "excludes-latest"
    return out


URL_VERDICTS = ("ok", "broken", "inconclusive", "acknowledged")  # acknowledged = not a finding
PKG_VERDICTS = ("ok", "excludes-latest", "broken", "inconclusive")


def new_report(now: Callable[[], dt.datetime] = utc_now) -> dict:
    return {
        "tool": "verify_online", "report_version": 2, "verification_kind": "automated-doc-check",
        "meaning": cf.VERIFICATION_KINDS["automated-doc-check"],
        "generated_at": now().replace(microsecond=0).isoformat(),
        "status": "INCOMPLETE", "reason": "run in progress", "budget": {}, "urls": [], "packages": [],
        "summary": {},
        "note": "Flags for human review only; this script never edits skills. Record outcomes in docs/claims.json; "
                "acknowledge persistent bot walls in schema/freshness_policy.json acknowledged_inconclusive.",
    }


def _update_counts(report: dict, fetcher: Fetcher) -> int:
    """Refresh summary/budget in place; return the number of findings so far."""
    url_counts = {v: sum(u["verdict"] == v for u in report["urls"]) for v in URL_VERDICTS}
    pkg_counts = {v: sum(p["verdict"] == v for p in report["packages"]) for v in PKG_VERDICTS}
    report["summary"] = {"urls": url_counts, "packages": pkg_counts}
    report["budget"] = {"max_seconds": fetcher.max_seconds, "exhausted": fetcher.deadline_hit,
                        "open_circuits": sorted(fetcher.open_circuits),
                        "breaker_on": sorted(fetcher.breaker_on), "abandoned_requests": fetcher.abandoned}
    return (sum(n for k, n in url_counts.items() if k not in ("ok", "acknowledged"))
            + sum(n for k, n in pkg_counts.items() if k != "ok"))


def run(skills: list[sl.Skill], fetcher: Fetcher, *, claims_path: Path | None, check_urls: bool = True,
        check_pypi: bool = True, probe: bool = True, max_urls: int | None = None,
        policy: cf.Policy | None = None, today: dt.date | None = None, report: dict | None = None,
        on_progress: Callable[[dict], None] | None = None, now: Callable[[], dt.datetime] = utc_now) -> dict:
    """Fill `report` (status INCOMPLETE until the end) and call on_progress after each item, so a run
    killed from outside still leaves a partial report on disk. `today` (default now()'s UTC date)
    decides whether an acknowledgement has passed its expires_on."""
    report = new_report(now) if report is None else report
    today = today if today is not None else now().date()
    progress = on_progress or (lambda _r: None)
    if probe:
        p = fetcher.request(PROBE_URL, "HEAD")
        if p.status is None:
            report.update(status="SKIPPED", reason=f"network unavailable: probe {PROBE_URL} failed ({p.error})")
            return report

    # PyPI first: one well-behaved host and ~40 requests, so a slow reference host can't starve it.
    if check_pypi:
        for dist, uses in collect_constraints(skills).items():
            report["packages"].append(check_package(fetcher, dist, uses))
            _update_counts(report, fetcher)
            progress(report)
    if check_urls:
        cited = collect_urls(skills, claims_path)
        acks = policy.acknowledged if policy else ()
        for url in list(cited)[:max_urls]:
            report["urls"].append({**check_url(fetcher, url, acks, today, now=now), "cited_by": cited[url]})
            _update_counts(report, fetcher)
            progress(report)

    if fetcher.requests and not fetcher.responses:
        report.update(status="SKIPPED", reason="network unavailable: no request got an HTTP response",
                      urls=[], packages=[])
        return report
    findings = _update_counts(report, fetcher)
    report["status"] = "FINDINGS" if findings else "OK"
    report["reason"] = ("run time budget exhausted; items not checked are 'inconclusive' with error_kind "
                        "'deadline'" if fetcher.deadline_hit else None)
    return report


def exit_code(report: dict) -> int:
    # INCOMPLETE only reaches here if run() was interrupted; it is never a pass.
    return {"OK": 0, "FINDINGS": 1, "SKIPPED": 2, "INCOMPLETE": 1}[report["status"]]


def human_summary(report: dict) -> str:
    lines = [f"verify_online ({report['verification_kind']}): {report['status']}"]
    if report["status"] == "SKIPPED":
        lines.append(f"  SKIPPED - {report['reason']}. Nothing was checked; this is not a pass.")
        return "\n".join(lines)
    if report.get("reason"):
        lines.append(f"  NOTE {report['reason']}")
    for u in report["urls"]:
        if u["verdict"] == "acknowledged":
            lines.append(f"  URL ACKNOWLEDGED {u['url']}  status={u['http_status']} (acknowledged "
                         f"{u['acknowledgement']['acknowledged_on']}, expires "
                         f"{u['acknowledgement']['expires_on']}; not a finding)")
        elif u["verdict"] != "ok":
            lapsed = " (acknowledgement lapsed)" if u.get("acknowledgement", {}).get("lapsed") else ""
            lines.append(f"  URL {u['verdict'].upper():12s} {u['url']}  status={u['http_status']} "
                         f"error={u['error']} cited_by={','.join(u['cited_by'])}{lapsed}")
    for p in report["packages"]:
        if p["verdict"] == "excludes-latest":
            for f in p["flags"]:
                lines.append(f"  PKG EXCLUDES-LATEST {f['skill']}: {f['message']}")
        elif p["verdict"] != "ok":
            lines.append(f"  PKG {p['verdict'].upper():12s} {p['dist']}: {p['error']}")
    s = report["summary"]
    if s:
        lines.append(f"  urls: {s['urls']}  packages: {s['packages']}")
    if report.get("budget", {}).get("abandoned_requests"):
        lines.append(f"  watchdog abandoned {report['budget']['abandoned_requests']} request(s) that outlived "
                     "their timeout")
    if report.get("budget", {}).get("open_circuits"):
        lines.append(f"  circuit opened for: {', '.join(report['budget']['open_circuits'])}")
    lines.append(f"  meaning: {report['meaning']}")
    lines.append(f"  {report['note']}")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    ap.add_argument("--root", type=Path, default=sl.REPO_ROOT)
    ap.add_argument("--claims", type=Path, default=sl.REPO_ROOT / "docs" / "claims.json")
    ap.add_argument("--json", action="store_true", help="print the JSON report instead of the summary")
    ap.add_argument("--json-out", type=Path, help="also write the JSON report to this file")
    ap.add_argument("--skip-urls", action="store_true")
    ap.add_argument("--skip-pypi", action="store_true")
    ap.add_argument("--no-probe", action="store_true", help="skip the connectivity probe")
    ap.add_argument("--max-urls", type=int, default=None)
    ap.add_argument("--timeout", type=float, default=15.0)
    ap.add_argument("--retries", type=int, default=3)
    ap.add_argument("--min-interval", type=float, default=1.0, help="seconds between requests to one host")
    ap.add_argument("--max-seconds", type=float, default=1200.0,
                    help="run time budget (default 1200; 0 = unlimited). Keep it below the CI job timeout: "
                         "unchecked items are reported 'inconclusive' (deadline) instead of the job being killed")
    ap.add_argument("--host-failure-limit", type=int, default=3,
                    help="stop contacting a host after this many consecutive failed requests (0 = never)")
    ap.add_argument("--breaker-on", default=",".join(BREAKER_CLASSES),
                    help="comma-separated failure classes the circuit breaker counts: "
                         f"{', '.join(BREAKER_CLASSES)} (default: all)")
    ap.add_argument("--policy", type=Path, default=cf.DEFAULT_POLICY_PATH,
                    help="freshness policy (for acknowledged_inconclusive)")
    ap.add_argument("--today", type=dt.date.fromisoformat, default=None,
                    help="date used for acknowledgement expiry (default: today in UTC)")
    args = ap.parse_args(argv)

    skills = sl.load_all_skills(args.root)
    try:
        policy = cf.load_policy(args.policy, skills)
    except cf.PolicyError as e:
        print(f"ERROR {e}", file=sys.stderr)
        return 3
    breaker_on = [c.strip() for c in args.breaker_on.split(",") if c.strip()]
    try:
        fetcher = Fetcher(timeout=args.timeout, retries=args.retries, min_interval=args.min_interval,
                          max_seconds=args.max_seconds, host_failure_limit=args.host_failure_limit,
                          breaker_on=breaker_on)
    except ValueError as e:
        ap.error(str(e))
    report = new_report()

    def checkpoint(r: dict) -> None:
        if args.json_out:
            _write_json_atomic(args.json_out, r)

    try:
        run(skills, fetcher, claims_path=args.claims, check_urls=not args.skip_urls, check_pypi=not args.skip_pypi,
            probe=not args.no_probe, max_urls=args.max_urls, policy=policy,
            today=args.today, report=report, on_progress=checkpoint)
    except BaseException as e:  # incl. KeyboardInterrupt: keep whatever was checked, marked INCOMPLETE
        report.update(status="INCOMPLETE", reason=f"run aborted: {type(e).__name__}: {e}")
        checkpoint(report)
        raise
    checkpoint(report)
    print(json.dumps(report, indent=2) if args.json else human_summary(report))
    return exit_code(report)


def _write_json_atomic(path: Path, report: dict) -> None:
    """Replace the report in one step so a kill mid-write never leaves truncated JSON."""
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, path)


if __name__ == "__main__":
    sys.exit(main())
