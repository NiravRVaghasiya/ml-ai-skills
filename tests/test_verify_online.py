"""Tests for scripts/verify_online.py with the HTTP layer mocked. Never touches the network."""
import contextlib
import email.message
import io
import json
import socket
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

import testutil  # noqa: F401  (inserts scripts/ and schema/ onto sys.path)
from testutil import make_skill

import verify_online as vo

URL = "https://docs.example.org/page"
BODY = ("\n## Overview\nx\n\n## Workflow\n1. s\n```python\nprint(1)\n```\n\n## Gotchas\n- a\n- b\n- c\n\n"
        f"## References\n- [Docs]({URL}) - why\n- [Other](https://other.example.net/x) - why\n")


def headers(**kv):
    msg = email.message.Message()
    for k, v in kv.items():
        msg[k.replace("_", "-")] = v
    return msg


class FakeResponse:
    def __init__(self, status=200, body=b"", hdrs=None):
        self.status, self._body, self.headers = status, body, hdrs or headers()

    def read(self, n=-1):
        return self._body if n is None or n < 0 else self._body[:n]

    def getcode(self):
        return self.status

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def http_error(url, code, **hdrs):
    return urllib.error.HTTPError(url, code, "err", headers(**hdrs), None)


class FakeNet:
    """Scripted opener: routes[(method, url)] is a list of outcomes consumed in order (last one repeats)."""

    def __init__(self, routes=None, default=None):
        self.routes = {k: list(v) for k, v in (routes or {}).items()}
        self.default = default
        self.calls = []

    def __call__(self, req, timeout):
        key = (req.get_method(), req.full_url)
        self.calls.append((key[0], key[1], req.get_header("User-agent"), timeout))
        seq = self.routes.get(key)
        outcome = (seq.pop(0) if len(seq) > 1 else seq[0]) if seq else self.default
        if outcome is None:
            raise AssertionError(f"unexpected request {key}")
        if isinstance(outcome, BaseException):
            raise outcome
        if isinstance(outcome, int):
            if outcome >= 400:
                raise http_error(req.full_url, outcome)
            return FakeResponse(outcome)
        if callable(outcome):
            return outcome(req)
        return outcome


class FakeClock:
    def __init__(self):
        self.now, self.sleeps = 0.0, []

    def clock(self):
        return self.now

    def sleep(self, s):
        self.sleeps.append(s)
        self.now += s


def make_fetcher(net, **kw):
    clk = FakeClock()
    kw.setdefault("min_interval", 0.0)
    return vo.Fetcher(opener=net, sleep=clk.sleep, clock=clk.clock, **kw), clk


def pypi_payload(*versions, yanked=()):
    rel = {v: [{"yanked": v in yanked}] for v in versions}
    return json.dumps({"info": {"version": versions[-1]}, "releases": rel}).encode()


class TestFetcher(unittest.TestCase):
    def test_success_sends_user_agent_and_timeout(self):
        net = FakeNet({("HEAD", URL): [200]})
        f, clk = make_fetcher(net, timeout=7)
        res = vo.check_url(f, URL)
        self.assertEqual((res["verdict"], res["http_status"], res["attempts"]), ("ok", 200, 1))
        self.assertEqual(net.calls, [("HEAD", URL, vo.USER_AGENT, 7)])
        self.assertEqual(clk.sleeps, [])

    def test_404_is_broken_after_get_fallback(self):
        net = FakeNet({("HEAD", URL): [404], ("GET", URL): [404]})
        f, _ = make_fetcher(net)
        res = vo.check_url(f, URL)
        self.assertEqual((res["verdict"], res["http_status"], res["method"]), ("broken", 404, "GET"))
        self.assertEqual([c[0] for c in net.calls], ["HEAD", "GET"])

    def test_head_rejected_then_get_succeeds(self):
        net = FakeNet({("HEAD", URL): [405], ("GET", URL): [200]})
        f, _ = make_fetcher(net)
        res = vo.check_url(f, URL)
        self.assertEqual((res["verdict"], res["method"], res["head_status"]), ("ok", "GET", 405))

    def test_timeout_retries_with_exponential_backoff_then_inconclusive(self):
        net = FakeNet(default=socket.timeout("timed out"))
        f, clk = make_fetcher(net, retries=2, backoff=1.5)
        res = vo.check_url(f, URL)
        self.assertEqual((res["verdict"], res["error_kind"]), ("inconclusive", "timeout"))
        # 1 + 2 retries on HEAD only: a GET fallback would just repeat the same timeouts.
        self.assertEqual(res["attempts"], 3)
        self.assertEqual([c[0] for c in net.calls], ["HEAD"] * 3)
        self.assertEqual(clk.sleeps, [1.5, 3.0])
        wrapped = FakeNet(default=urllib.error.URLError(socket.timeout("slow")))
        f, _ = make_fetcher(wrapped, retries=0)
        self.assertEqual(f.request(URL).error_kind, "timeout")

    def test_429_honours_retry_after_then_succeeds(self):
        net = FakeNet({("HEAD", URL): [http_error(URL, 429, Retry_After="7"), 200]})
        f, clk = make_fetcher(net)
        res = vo.check_url(f, URL)
        self.assertEqual((res["verdict"], res["attempts"]), ("ok", 2))
        self.assertEqual(clk.sleeps, [7.0])

    def test_503_without_retry_after_backs_off_and_retry_after_is_capped(self):
        net = FakeNet({("GET", URL): [503, 503, 200]})
        f, clk = make_fetcher(net, backoff=2.0)
        self.assertTrue(f.request(URL).ok)
        self.assertEqual(clk.sleeps, [2.0, 4.0])
        net = FakeNet({("GET", URL): [http_error(URL, 503, Retry_After="86400"), 200]})
        f, clk = make_fetcher(net, max_wait=30)
        self.assertTrue(f.request(URL).ok)
        self.assertEqual(clk.sleeps, [30])

    def test_retries_are_bounded(self):
        net = FakeNet({("GET", URL): [429]})
        f, _ = make_fetcher(net, retries=3, backoff=0.01)
        res = f.request(URL)
        self.assertEqual((res.status, res.attempts, len(net.calls)), (429, 4, 4))

    def test_retry_after_http_date(self):
        import datetime as dt

        now = dt.datetime(2026, 10, 27, 12, 0, 0, tzinfo=dt.timezone.utc)
        self.assertEqual(vo.parse_retry_after("Tue, 27 Oct 2026 12:00:30 GMT", now=lambda: now), 30.0)
        self.assertEqual(vo.parse_retry_after("Tue, 27 Oct 2026 11:00:00 GMT", now=lambda: now), 0.0)
        self.assertIsNone(vo.parse_retry_after("soon"))
        self.assertIsNone(vo.parse_retry_after(None))

    def test_per_host_rate_limit(self):
        net = FakeNet(default=200)
        f, clk = make_fetcher(net, min_interval=1.0)
        f.request("https://a.example/1")
        f.request("https://b.example/1")  # different host: no wait
        f.request("https://a.example/2")  # same host, no time elapsed: waits the full interval
        self.assertEqual(clk.sleeps, [1.0])

    def test_non_http_schemes_are_refused_without_a_request(self):
        net = FakeNet()
        f, _ = make_fetcher(net)
        for url in ("file:///etc/passwd", "ftp://example.com/x"):
            self.assertEqual(f.request(url).error_kind, "refused-scheme")
        self.assertEqual(net.calls, [])


class SlowNet(FakeNet):
    """FakeNet where each request costs `cost` seconds and a timeout costs the full timeout, on a FakeClock."""

    def __init__(self, clk, routes=None, default=None, cost=0.0):
        super().__init__(routes, default)
        self.clk, self.cost = clk, cost

    def __call__(self, req, timeout):
        self.clk.now += self.cost
        try:
            return super().__call__(req, timeout)
        except socket.timeout:
            self.clk.now += timeout
            raise


def budget_fetcher(routes=None, default=None, cost=0.0, **kw):
    clk = FakeClock()
    net = SlowNet(clk, routes, default, cost)
    kw.setdefault("min_interval", 0.0)
    return vo.Fetcher(opener=net, sleep=clk.sleep, clock=clk.clock, **kw), clk, net


class TestBudgetAndCircuitBreaker(unittest.TestCase):
    def test_head_429_after_retries_does_not_repeat_on_get(self):
        net = FakeNet({("HEAD", URL): [http_error(URL, 429, Retry_After="5")]})
        f, clk = make_fetcher(net, retries=2)
        res = vo.check_url(f, URL)
        self.assertEqual((res["verdict"], res["http_status"], res["attempts"]), ("inconclusive", 429, 3))
        self.assertEqual([c[0] for c in net.calls], ["HEAD"] * 3)
        self.assertEqual(clk.sleeps, [5.0, 5.0])

    def test_circuit_opens_after_consecutive_throttled_requests(self):
        urls = [f"https://slow.example/{i}" for i in range(6)]
        f, _, net = budget_fetcher(default=socket.timeout("timed out"), retries=1, backoff=1.0,
                                   timeout=10, host_failure_limit=3)
        results = [vo.check_url(f, u) for u in urls]
        self.assertEqual([r["error_kind"] for r in results], ["timeout"] * 3 + ["circuit-open"] * 3)
        self.assertTrue(all(r["verdict"] == "inconclusive" for r in results))
        self.assertEqual(len(net.calls), 3 * 2)  # (1 + 1 retry) x 3 URLs; nothing sent once open
        self.assertEqual(f.open_circuits, {"slow.example"})
        # other hosts are unaffected
        net.routes[("HEAD", "https://fine.example/x")] = [200]
        self.assertEqual(vo.check_url(f, "https://fine.example/x")["verdict"], "ok")

    def test_success_resets_the_breaker(self):
        host = "https://flaky.example/"
        routes = {("HEAD", host + "a"): [429], ("HEAD", host + "b"): [429], ("HEAD", host + "c"): [200],
                  ("HEAD", host + "d"): [429], ("HEAD", host + "e"): [429], ("HEAD", host + "f"): [200]}
        f, _ = make_fetcher(FakeNet(routes), retries=0, host_failure_limit=3)
        verdicts = [vo.check_url(f, host + x)["verdict"] for x in "abcdef"]
        self.assertEqual(verdicts[2], "ok")
        self.assertEqual(verdicts[5], "ok")  # never 3 in a row, so the circuit stayed closed
        self.assertEqual(f.open_circuits, set())

    def test_deadline_stops_work_and_marks_unchecked_urls(self):
        urls = [f"https://h{i}.example/x" for i in range(20)]  # distinct hosts: breaker never trips
        f, clk, net = budget_fetcher(default=http_error(URL, 429, Retry_After="3600"), cost=5.0, retries=3,
                                     max_wait=120, max_seconds=300)
        results = [vo.check_url(f, u) for u in urls]
        self.assertLessEqual(clk.now, 300)
        self.assertTrue(f.deadline_hit)
        # the first URL used its retries until the next Retry-After wait would overrun the budget
        self.assertEqual(results[0]["attempts"], 3)
        self.assertIn("retry abandoned", results[0]["error"])
        kinds = [r["error_kind"] for r in results]
        self.assertIn("deadline", kinds)
        first_skipped = kinds.index("deadline")
        self.assertTrue(all(k == "deadline" for k in kinds[first_skipped:]))
        self.assertTrue(all(r["verdict"] == "inconclusive" and r["attempts"] == 0 for r in results[first_skipped:]))
        sent = len(net.calls)
        vo.check_url(f, "https://late.example/x")
        self.assertEqual(len(net.calls), sent)  # nothing is sent after the budget is gone

    def test_reviewer_worst_case_stays_inside_budget(self):
        # 121 URLs, 27 on one host that always times out, the rest on hosts that 429 with Retry-After: 3600,
        # with the real CLI defaults. Without a budget this was ~148 s per timing-out URL and ~721 s per 429 URL.
        urls = [f"https://arxiv.example/{i}" for i in range(27)] + [f"https://h{i}.example/x" for i in range(94)]

        clk = FakeClock()

        def opener(req, timeout):
            if "arxiv" in req.full_url:
                clk.now += timeout
                raise socket.timeout("timed out")
            raise http_error(req.full_url, 429, Retry_After="3600")

        f = vo.Fetcher(opener=opener, sleep=clk.sleep, clock=clk.clock, max_seconds=1200)
        results = [vo.check_url(f, u) for u in urls]
        # The per-request timeout is min(--timeout, remaining budget), so nothing in flight crosses the deadline.
        self.assertLessEqual(clk.now, 1200)
        self.assertEqual(len(results), 121)
        self.assertIn("arxiv.example", f.open_circuits)
        self.assertTrue(all(r["verdict"] == "inconclusive" for r in results))


class TestBudgetBound(unittest.TestCase):
    def test_request_timeout_is_capped_by_remaining_budget(self):
        f, clk, net = budget_fetcher(default=200, timeout=15, max_seconds=10)
        clk.now = 6.5  # 3.5 s of budget left
        self.assertTrue(f.request(URL).ok)
        self.assertEqual(net.calls[-1][3], 3.5)

    def test_no_request_starts_with_less_than_the_minimum_left(self):
        f, clk, net = budget_fetcher(default=200, max_seconds=10)
        clk.now = 10 - vo.MIN_REQUEST_SECONDS / 2
        res = f.request(URL)
        self.assertEqual((res.error_kind, net.calls), ("deadline", []))

    def test_pacing_wait_that_would_cross_the_deadline_is_not_slept(self):
        f, clk, net = budget_fetcher(default=200, max_seconds=10, min_interval=30)
        self.assertTrue(f.request("https://a.example/1").ok)
        res = f.request("https://a.example/2")
        self.assertEqual((res.error_kind, len(net.calls), clk.sleeps), ("deadline", 1, []))
        self.assertLessEqual(clk.now, 10)

    def test_timeouts_never_carry_the_clock_past_the_deadline(self):
        f, clk, _ = budget_fetcher(default=socket.timeout("timed out"), timeout=15, retries=3, backoff=1.0,
                                   max_seconds=40, host_failure_limit=0)
        for i in range(10):
            vo.check_url(f, f"https://slow{i}.example/x")
        self.assertTrue(f.deadline_hit)
        self.assertLessEqual(clk.now, 40)

    def test_watchdog_abandons_a_request_that_outlives_its_timeout(self):
        # Real clock, no network: the opener blocks (like a server dripping bytes, or a DNS lookup that
        # urllib cannot time out) and ignores its timeout. The watchdog must return anyway.
        import threading
        import time

        release = threading.Event()
        self.addCleanup(release.set)

        def stuck(req, timeout):
            release.wait(30)
            raise socket.timeout("released")

        f = vo.Fetcher(opener=stuck, timeout=0.2, retries=0, min_interval=0, watchdog_grace=0.3)
        started = time.monotonic()
        res = f.request(URL)
        elapsed = time.monotonic() - started
        self.assertEqual(res.error_kind, "timeout")
        self.assertIn("watchdog", res.error)
        self.assertEqual(f.abandoned, 1)
        self.assertLess(elapsed, 5.0)  # about 0.5 s expected; generous for a loaded machine

    def test_exception_in_the_request_thread_reaches_the_caller(self):
        def boom(req, timeout):
            raise KeyboardInterrupt

        f = vo.Fetcher(opener=boom, retries=0, min_interval=0)
        with self.assertRaises(KeyboardInterrupt):
            f.request(URL)


class TestCircuitBreakerClasses(unittest.TestCase):
    def results(self, outcome, n=5, **kw):
        f, _ = make_fetcher(FakeNet(default=outcome), retries=0, host_failure_limit=3, **kw)
        return f, [f.request(f"https://bad.example/{i}").error_kind for i in range(n)]

    def test_server_errors_and_network_failures_open_the_circuit(self):
        for outcome in (500, 502, urllib.error.URLError(OSError("Name or service not known")),
                        ConnectionResetError("reset by peer")):
            with self.subTest(outcome=outcome):
                f, kinds = self.results(outcome)
                self.assertEqual(kinds[3:], ["circuit-open", "circuit-open"])
                self.assertEqual(f.open_circuits, {"bad.example"})

    def test_breaker_counts_only_the_configured_classes(self):
        f, kinds = self.results(urllib.error.URLError(OSError("down")), breaker_on=("throttle", "timeout"))
        self.assertEqual(kinds, ["network"] * 5)
        self.assertEqual(f.open_circuits, set())
        f, kinds = self.results(500, breaker_on=("server-error",))
        self.assertEqual(kinds[3:], ["circuit-open", "circuit-open"])

    def test_definitive_answer_resets_and_unlisted_failure_is_neutral(self):
        routes = {("GET", f"https://h.example/{i}"): [code] for i, code in enumerate([500, 500, 403, 500, 500])}
        f, _ = make_fetcher(FakeNet(routes), retries=0, host_failure_limit=3)
        for i in range(5):
            f.request(f"https://h.example/{i}")
        self.assertEqual(f.open_circuits, set())  # the 403 broke the run of 5xx
        down = urllib.error.URLError(OSError("down"))
        routes = {("GET", "https://n.example/0"): [500], ("GET", "https://n.example/1"): [down],
                  ("GET", "https://n.example/2"): [500], ("GET", "https://n.example/3"): [500]}
        f, _ = make_fetcher(FakeNet(routes), retries=0, host_failure_limit=3, breaker_on=("server-error",))
        for i in range(3):
            f.request(f"https://n.example/{i}")
        self.assertEqual(f.open_circuits, set())  # 500, network (not counted, does not reset), 500: two so far
        f.request("https://n.example/3")
        self.assertEqual(f.open_circuits, {"n.example"})  # third counted 500

    def test_failure_class_mapping_and_validation(self):
        self.assertEqual(vo.failure_class(vo.HttpResult(URL, "GET", 429)), "throttle")
        self.assertEqual(vo.failure_class(vo.HttpResult(URL, "GET", 503)), "throttle")
        self.assertEqual(vo.failure_class(vo.HttpResult(URL, "GET", 504)), "server-error")
        self.assertEqual(vo.failure_class(vo.HttpResult(URL, "GET", None, "x", "timeout")), "timeout")
        self.assertEqual(vo.failure_class(vo.HttpResult(URL, "GET", None, "x", "network")), "network")
        self.assertIsNone(vo.failure_class(vo.HttpResult(URL, "GET", 404)))
        self.assertIsNone(vo.failure_class(vo.HttpResult(URL, "GET", 200)))
        with self.assertRaises(ValueError):
            vo.Fetcher(breaker_on=("throttle", "dns"))
        with mock.patch.object(vo, "default_opener", FakeNet()), contextlib.redirect_stderr(io.StringIO()), \
                self.assertRaises(SystemExit) as ctx:
            vo.main(["--breaker-on", "throttle,bogus"])
        self.assertEqual(ctx.exception.code, 2)


class TestAcknowledged(unittest.TestCase):
    def setUp(self):
        import datetime as dt

        import check_freshness as cf

        self.dt = dt
        self.ack = cf.AckInconclusive(url=URL, host=None, http_status=frozenset({403}), reason="bot wall",
                                      acknowledged_on=dt.date(2026, 10, 1), expires_on=dt.date(2027, 3, 30))
        self.host_ack = cf.AckInconclusive(url=None, host="other.example.net", http_status=frozenset({403}),
                                           reason="bot wall", acknowledged_on=dt.date(2026, 10, 1),
                                           expires_on=dt.date(2027, 3, 30))

    def test_acknowledged_403_is_not_a_finding(self):
        f, _ = make_fetcher(FakeNet({("HEAD", URL): [403], ("GET", URL): [403]}))
        res = vo.check_url(f, URL, [self.ack], self.dt.date(2026, 10, 27), 180)
        self.assertEqual(res["verdict"], "acknowledged")
        self.assertFalse(res["acknowledgement"]["lapsed"])

    def test_host_ack_and_status_must_match(self):
        other = "https://other.example.net/x"
        f, _ = make_fetcher(FakeNet({("HEAD", other): [403], ("GET", other): [403], ("HEAD", URL): [403],
                                     ("GET", URL): [503]}, ))
        day = self.dt.date(2026, 10, 27)
        self.assertEqual(vo.check_url(f, other, [self.host_ack], day)["verdict"], "acknowledged")
        # URL ack is for 403, but the final answer was 503: still inconclusive
        self.assertEqual(vo.check_url(f, URL, [self.ack], day)["verdict"], "inconclusive")

    def test_broken_is_never_acknowledged_and_lapsed_ack_resurfaces(self):
        f, _ = make_fetcher(FakeNet({("HEAD", URL): [404], ("GET", URL): [404]}))
        self.assertEqual(vo.check_url(f, URL, [self.ack])["verdict"], "broken")
        f, _ = make_fetcher(FakeNet({("HEAD", URL): [403], ("GET", URL): [403]}))
        res = vo.check_url(f, URL, [self.ack], self.dt.date(2027, 4, 1), 180)  # 182 days later
        self.assertEqual(res["verdict"], "inconclusive")
        self.assertTrue(res["acknowledgement"]["lapsed"])

    def test_explicit_expiry_date_and_injectable_clock(self):
        def verdict(**kw):
            f, _ = make_fetcher(FakeNet({("HEAD", URL): [403], ("GET", URL): [403]}))
            return vo.check_url(f, URL, [self.ack], **kw)

        self.assertEqual(verdict(today=self.dt.date(2027, 3, 30))["verdict"], "acknowledged")  # last valid day
        lapsed = verdict(today=self.dt.date(2027, 3, 31))
        self.assertEqual((lapsed["verdict"], lapsed["acknowledgement"]["lapsed"]), ("inconclusive", True))
        self.assertEqual(lapsed["acknowledgement"]["expires_on"], "2027-03-30")
        # without `today`, the injected clock decides (no hidden dependency on the machine's date)
        utc = self.dt.timezone.utc
        self.assertEqual(verdict(now=lambda: self.dt.datetime(2027, 4, 2, tzinfo=utc))["verdict"], "inconclusive")
        self.assertEqual(verdict(now=lambda: self.dt.datetime(2026, 10, 2, tzinfo=utc))["verdict"], "acknowledged")
        # the legacy max-age argument still caps an acknowledgement further
        self.assertEqual(verdict(today=self.dt.date(2026, 10, 20), ack_max_age_days=10)["verdict"], "inconclusive")

    def test_run_uses_the_injected_clock_for_expiry_and_report_time(self):
        import check_freshness as cf

        policy = cf.policy_from_dict({"policy_version": 1, "default_max_age_days": 180,
                                      "acknowledged_inconclusive": [{"url": URL, "http_status": [403],
                                                                     "reason": "bot wall",
                                                                     "acknowledged_on": "2026-10-01",
                                                                     "expires_on": "2026-11-01"}]})
        routes = {("HEAD", URL): [403], ("GET", URL): [403], ("HEAD", "https://other.example.net/x"): [200]}

        def run_at(when):
            f, _ = make_fetcher(FakeNet(routes))
            return vo.run([make_skill("a", body=BODY)], f, claims_path=None, check_pypi=False, probe=False,
                          policy=policy, now=lambda: when)

        utc = self.dt.timezone.utc
        before = run_at(self.dt.datetime(2026, 11, 1, 23, 0, tzinfo=utc))
        after = run_at(self.dt.datetime(2026, 11, 2, 0, 30, tzinfo=utc))
        self.assertEqual((before["status"], after["status"]), ("OK", "FINDINGS"))
        self.assertEqual(after["generated_at"], "2026-11-02T00:30:00+00:00")
        self.assertIn("acknowledgement lapsed", vo.human_summary(after))

    def test_policy_expiry_default_and_validation(self):
        import check_freshness as cf

        def policy(**ack):
            return {"policy_version": 1, "default_max_age_days": 180, "acknowledged_inconclusive": [
                {"url": URL, "http_status": [403], "reason": "bot wall", "acknowledged_on": "2026-10-01", **ack}]}

        self.assertEqual(cf.policy_from_dict(policy()).acknowledged[0].expires_on, self.dt.date(2027, 3, 30))
        self.assertEqual(cf.policy_from_dict(policy(expires_on="2026-12-01")).acknowledged[0].expires_on,
                         self.dt.date(2026, 12, 1))
        for bad, why in (("2026-09-30", "before acknowledged_on"), ("2027-03-31", "more than default_max_age_days"),
                         ("2026-13-01", "YYYY-MM-DD")):
            with self.subTest(expires_on=bad):
                errs = cf.validate_policy(policy(expires_on=bad))
                self.assertTrue(any(why in e for e in errs), errs)
        # the repo's own entries carry an explicit expiry
        for ack in json.loads(cf.DEFAULT_POLICY_PATH.read_text(encoding="utf-8"))["acknowledged_inconclusive"]:
            self.assertIn("expires_on", ack)

    def test_run_with_only_acknowledged_urls_is_ok(self):
        import check_freshness as cf

        policy = cf.policy_from_dict({"policy_version": 1, "default_max_age_days": 180,
                                      "acknowledged_inconclusive": [{"url": URL, "http_status": [403],
                                                                     "reason": "bot wall",
                                                                     "acknowledged_on": "2026-10-01"}]})
        net = FakeNet({("HEAD", vo.PROBE_URL): [200], ("HEAD", URL): [403], ("GET", URL): [403],
                       ("HEAD", "https://other.example.net/x"): [200]})
        f, _ = make_fetcher(net)
        report = vo.run([make_skill("a", body=BODY)], f, claims_path=None, check_pypi=False, policy=policy,
                        today=self.dt.date(2026, 10, 27))
        self.assertEqual((report["status"], report["summary"]["urls"]["acknowledged"]), ("OK", 1))
        self.assertIn("ACKNOWLEDGED", vo.human_summary(report))


class TestPypi(unittest.TestCase):
    def test_latest_stable_skips_prerelease_and_yanked(self):
        payload = json.loads(pypi_payload("0.7.2", "0.8.0", "1.0.0", "1.1.0rc1", yanked=("1.0.0",)))
        self.assertEqual(vo.latest_stable(payload), "0.8.0")

    def test_specifier_excluding_latest_is_flagged(self):
        url = vo.PYPI_JSON.format(dist="implicit")
        net = FakeNet({("GET", url): [FakeResponse(200, pypi_payload("0.7.2", "0.8.0"))]})
        f, _ = make_fetcher(net)
        uses = [{"skill": "a", "specifier": ">=0.7,<0.8", "entry": "implicit>=0.7,<0.8: x"},
                {"skill": "b", "specifier": ">=0.7", "entry": "implicit>=0.7: x"},
                {"skill": "c", "specifier": "", "entry": "implicit: x"}]
        res = vo.check_package(f, "implicit", uses)
        self.assertEqual((res["verdict"], res["latest"]), ("excludes-latest", "0.8.0"))
        self.assertEqual([x["skill"] for x in res["flags"]], ["a"])

    def test_unknown_dist_is_broken(self):
        url = vo.PYPI_JSON.format(dist="nope")
        f, _ = make_fetcher(FakeNet({("GET", url): [404]}))
        self.assertEqual(vo.check_package(f, "nope", [])["verdict"], "broken")


class TestRun(unittest.TestCase):
    def skills(self):
        return [make_skill("a", body=BODY, version_constraints=["implicit>=0.7: ALS", "free text"])]

    def net(self, overrides=None):
        routes = {
            ("HEAD", vo.PROBE_URL): [200],
            ("HEAD", URL): [200],
            ("HEAD", "https://other.example.net/x"): [200],
            ("GET", vo.PYPI_JSON.format(dist="implicit")): [FakeResponse(200, pypi_payload("0.7.2"))],
        }
        routes.update(overrides or {})
        return FakeNet(routes)

    def test_all_ok_exit_0(self):
        f, _ = make_fetcher(self.net())
        report = vo.run(self.skills(), f, claims_path=None)
        self.assertEqual((report["status"], vo.exit_code(report)), ("OK", 0))
        self.assertEqual(report["verification_kind"], "automated-doc-check")
        self.assertEqual([u["cited_by"] for u in report["urls"]], [["a"], ["a"]])

    def test_findings_exit_1(self):
        f, _ = make_fetcher(self.net({("HEAD", URL): [404], ("GET", URL): [404]}))
        report = vo.run(self.skills(), f, claims_path=None)
        self.assertEqual((report["status"], vo.exit_code(report)), ("FINDINGS", 1))
        self.assertEqual(report["summary"]["urls"]["broken"], 1)
        self.assertIn("BROKEN", vo.human_summary(report))

    def test_network_unavailable_exit_2(self):
        down = urllib.error.URLError(OSError("Temporary failure in name resolution"))
        f, _ = make_fetcher(FakeNet(default=down), retries=0)
        report = vo.run(self.skills(), f, claims_path=None)
        self.assertEqual((report["status"], vo.exit_code(report)), ("SKIPPED", 2))
        self.assertIn("network unavailable", report["reason"])
        self.assertIn("not a pass", vo.human_summary(report))

    def test_network_unavailable_without_probe_exit_2(self):
        down = urllib.error.URLError(OSError("Network is unreachable"))
        f, _ = make_fetcher(FakeNet(default=down), retries=0)
        report = vo.run(self.skills(), f, claims_path=None, probe=False)
        self.assertEqual(report["status"], "SKIPPED")
        self.assertEqual(report["urls"], [])

    def test_claim_source_urls_are_checked(self):
        with tempfile.TemporaryDirectory() as d:
            claims = Path(d) / "claims.json"
            claims.write_text(json.dumps({"claims": [{"id": "c1", "source_url": "https://src.example/doc"}]}),
                              encoding="utf-8")
            net = self.net({("HEAD", "https://src.example/doc"): [200]})
            f, _ = make_fetcher(net)
            report = vo.run(self.skills(), f, claims_path=claims, check_pypi=False)
        self.assertIn(["claim:c1"], [u["cited_by"] for u in report["urls"]])

    def test_main_writes_json_report_and_never_edits_skills(self):
        before = {p: p.read_bytes() for p in testutil.REPO_ROOT.glob("*/SKILL.md")}
        down = urllib.error.URLError(OSError("offline"))
        with tempfile.TemporaryDirectory() as d, \
                mock.patch.object(vo, "default_opener", FakeNet(default=down)):
            out = Path(d) / "report.json"
            with contextlib.redirect_stdout(io.StringIO()) as stdout:
                code = vo.main(["--json-out", str(out), "--retries", "0", "--min-interval", "0"])
            report = json.loads(out.read_text(encoding="utf-8"))
        self.assertEqual((code, report["status"]), (2, "SKIPPED"))
        self.assertIn("SKIPPED", stdout.getvalue())
        self.assertEqual(before, {p: p.read_bytes() for p in testutil.REPO_ROOT.glob("*/SKILL.md")})

    def test_interrupted_run_still_leaves_a_partial_report(self):
        calls = {"n": 0}

        def opener(req, timeout):
            calls["n"] += 1
            if calls["n"] == 1:  # connectivity probe
                return FakeResponse(200)
            if calls["n"] == 2:  # first PyPI package
                return FakeResponse(200, pypi_payload("99.0"))
            raise KeyboardInterrupt  # e.g. the CI step being cancelled mid-run

        with tempfile.TemporaryDirectory() as d, mock.patch.object(vo, "default_opener", opener):
            out = Path(d) / "report.json"
            with contextlib.redirect_stdout(io.StringIO()), self.assertRaises(KeyboardInterrupt):
                vo.main(["--json-out", str(out), "--retries", "0", "--min-interval", "0"])
            report = json.loads(out.read_text(encoding="utf-8"))
            leftovers = [p.name for p in Path(d).iterdir()]
        self.assertEqual(report["status"], "INCOMPLETE")
        self.assertIn("KeyboardInterrupt", report["reason"])
        self.assertEqual(len(report["packages"]), 1)
        self.assertEqual(vo.exit_code(report), 1)
        self.assertEqual(leftovers, ["report.json"])  # atomic replace leaves no .tmp behind

    def test_invalid_policy_exits_3_without_network(self):
        with tempfile.TemporaryDirectory() as d:
            bad = Path(d) / "policy.json"
            bad.write_text("{}", encoding="utf-8")
            with mock.patch.object(vo, "default_opener", FakeNet()), contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(vo.main(["--policy", str(bad)]), 3)


if __name__ == "__main__":
    unittest.main()
