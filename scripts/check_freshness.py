#!/usr/bin/env python3
"""Offline freshness scanner: flags for human review, never proof of correctness.

Every finding carries a `kind` saying what produced it (see FINDING_KINDS):

* static-pattern      -- regex over SKILL.md text for APIs known (at authoring
                         time) to be removed/renamed. A heuristic, NOT verification.
* review-clock        -- `last_verified` age against schema/freshness_policy.json.
                         Date arithmetic only; says nothing about correctness.
* metadata            -- `version_constraints` presence/parseability and import
                         coverage. Bookkeeping, not verification.
* local-version-check -- (opt-in, --local-versions) compares each parsed
                         specifier with the version installed in THIS Python
                         environment via importlib.metadata. Offline; a package
                         that is not installed is SKIPPED, not passed.

Network checks (automated-doc-check) live in scripts/verify_online.py and run
only in the scheduled workflow. See docs/FRESHNESS.md.

Usage:
    python scripts/check_freshness.py                    # human-readable report
    python scripts/check_freshness.py --json             # machine-readable report
    python scripts/check_freshness.py --local-versions   # + installed-version comparison
    python scripts/check_freshness.py --policy PATH --today 2026-10-27
    python scripts/check_freshness.py --validate-policy  # validate the policy file only
    python scripts/check_freshness.py --claims PATH      # registry used to gate STALE patterns

A known_stale_patterns entry with level STALE fails CI only when its backing
claim in docs/claims.json (--claims) is verified; otherwise a match is reported
as REVIEW_DUE with the reason (see docs/FRESHNESS.md).

Exit codes: 0 = no STALE finding; 1 = at least one STALE finding (known-removed
API backed by a verified claim, or an invalid last_verified); 2 = the policy
file or the claims registry is missing or invalid.
"""
from __future__ import annotations

import argparse
import datetime as dt
import importlib.metadata
import json
import re
import sys
import urllib.parse
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterable

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "schema"))
import allowed_values as av  # noqa: E402
import skills_lib as sl  # noqa: E402

DEFAULT_POLICY_PATH = sl.REPO_ROOT / "schema" / "freshness_policy.json"
DEFAULT_CLAIMS_PATH = sl.REPO_ROOT / "docs" / "claims.json"

# ---------------------------------------------------------------------------
# Vocabulary. VERIFICATION_KINDS is the repo-wide meaning of each kind of
# evidence; docs/claims.json, check_claims.py and verify_online.py all use it.
# ---------------------------------------------------------------------------
VERIFICATION_KINDS: dict[str, str] = {
    "static-pattern": "Offline regex heuristic over SKILL.md text (check_freshness.py). Flags APIs listed as "
                      "removed in the freshness policy; absence of a match proves nothing. NOT verification.",
    "local-version-check": "Offline comparison of a version_constraints specifier against the version installed in "
                           "the environment that ran the check (importlib.metadata). Only covers that environment; "
                           "says nothing about API behaviour.",
    "automated-doc-check": "Network check by scripts/verify_online.py (scheduled job only): reference URL reachability "
                           "and PyPI latest release vs specifier. A reachable page is not proof its content still "
                           "supports the claim.",
    "runtime-test": "Code executed by a test under tests/ (e.g. tests/examples/) against the library versions "
                    "installed in that run, with any stubs the test declares.",
    "human-review": "A named reviewer read the cited source and recorded reviewer, date and source in docs/claims.json.",
}
# Values a claims.json entry may use. static-pattern is deliberately absent: a
# regex match is a heuristic, so a claim backed only by one is 'unverified'.
CLAIM_VERIFICATION = ("local-version-check", "automated-doc-check", "runtime-test", "human-review", "unverified")

FINDING_KINDS: dict[str, str] = {
    "static-pattern": VERIFICATION_KINDS["static-pattern"],
    "review-clock": "last_verified age compared with the freshness policy. A review reminder; not a correctness signal.",
    "metadata": "version_constraints presence/parseability and import coverage. Bookkeeping; not verification.",
    "local-version-check": VERIFICATION_KINDS["local-version-check"],
}
LEVELS: dict[str, str] = {
    "STALE": ("Matches a known_stale_patterns entry whose backing claim in docs/claims.json was verified "
              "by a runtime test or human review (see check_claims.py), or last_verified is not a real date. "
              "Fails CI. A STALE pattern whose claim is missing, disputed or otherwise backed is reported as "
              "REVIEW_DUE instead, with the reason."),
    "REVIEW_DUE": "Needs a human look. Informational; does not fail CI.",
    "INFO": "Bookkeeping note. Informational.",
    "SKIPPED": "A check could not run here (e.g. package not installed). Not a pass.",
    "OK": "local-version-check: installed version satisfies the specifier (this environment only).",
}

# Import name -> PyPI distribution name, for imports whose names differ from
# the dist after PEP 503 normalisation. faiss maps to faiss-cpu because that is
# the wheel the examples and CI install (faiss-gpu exposes the same module).
IMPORT_TO_DIST: dict[str, str] = {
    "sklearn": "scikit-learn",
    "cv2": "opencv-python",
    "faiss": "faiss-cpu",
    "PIL": "pillow",
    "yaml": "pyyaml",
    "skimage": "scikit-image",
    "bs4": "beautifulsoup4",
    "surprise": "scikit-surprise",
    "imblearn": "imbalanced-learn",
    "umap": "umap-learn",
    "airflow": "apache-airflow",
    "dateutil": "python-dateutil",
    "dotenv": "python-dotenv",
    "docx": "python-docx",
    "jwt": "pyjwt",
    "attr": "attrs",
    "mpl_toolkits": "matplotlib",
    "Crypto": "pycryptodome",
    "OpenSSL": "pyopenssl",
    "google_crc32c": "google-crc32c",
}
# Names people write in version_constraints that are not dist names.
NAME_ALIASES: dict[str, str] = {**{k.lower(): v for k, v in IMPORT_TO_DIST.items()}, "pytorch": "torch"}


def normalize_dist(name: str) -> str:
    """PEP 503 normalisation, then alias mapping (sklearn -> scikit-learn)."""
    norm = re.sub(r"[-_.]+", "-", name).lower()
    return NAME_ALIASES.get(norm, NAME_ALIASES.get(name.lower(), norm))


# ---------------------------------------------------------------------------
# PEP 440 subset. Enough to evaluate the specifiers this repo writes
# (==, !=, >=, <=, <, >, ~=, ==X.*, comma-joined). No `===`, no environment
# markers. Stdlib-only so the default CI job needs no `pip install packaging`.
# ---------------------------------------------------------------------------
_VERSION_RE = re.compile(
    r"^\s*v?(?:(?P<epoch>\d+)!)?(?P<release>\d+(?:\.\d+)*)"
    r"(?:[-_.]?(?P<pre_l>a|b|c|rc|alpha|beta|pre|preview)[-_.]?(?P<pre_n>\d+)?)?"
    r"(?:-(?P<post_n1>\d+)|[-_.]?(?P<post_l>post|rev|r)[-_.]?(?P<post_n2>\d+)?)?"
    r"(?:[-_.]?(?P<dev_l>dev)[-_.]?(?P<dev_n>\d+)?)?"
    r"(?:\+(?P<local>[a-z0-9]+(?:[-_.][a-z0-9]+)*))?\s*$",
    re.IGNORECASE,
)
_PRE_RANK = {"a": 0, "alpha": 0, "b": 1, "beta": 1, "c": 2, "rc": 2, "pre": 2, "preview": 2}


class InvalidVersion(ValueError):
    pass


@dataclass(frozen=True)
class Version:
    epoch: int
    release: tuple[int, ...]
    pre: tuple[int, int] | None
    post: int | None
    dev: int | None
    local: str | None
    text: str

    @classmethod
    def parse(cls, text: str) -> "Version":
        m = _VERSION_RE.match(text)
        if not m:
            raise InvalidVersion(f"not a PEP 440 version: {text!r}")
        pre = None
        if m["pre_l"]:
            pre = (_PRE_RANK[m["pre_l"].lower()], int(m["pre_n"] or 0))
        post = None
        if m["post_n1"] is not None:
            post = int(m["post_n1"])
        elif m["post_l"]:
            post = int(m["post_n2"] or 0)
        dev = int(m["dev_n"] or 0) if m["dev_l"] else None
        return cls(int(m["epoch"] or 0), tuple(int(p) for p in m["release"].split(".")), pre, post, dev,
                   m["local"].lower() if m["local"] else None, text.strip())

    @property
    def is_prerelease(self) -> bool:
        return self.pre is not None or self.dev is not None

    @property
    def public(self) -> "Version":
        return Version(self.epoch, self.release, self.pre, self.post, self.dev, None, self.text.split("+")[0])

    def key(self) -> tuple:
        # PEP 440 ordering: X.devN < X.aN < X.bN < X.rcN < X < X.postN; 1.0 == 1.0.0.
        rel = list(self.release)
        while len(rel) > 1 and rel[-1] == 0:
            rel.pop()
        if self.pre is None and self.post is None and self.dev is not None:
            pre = (-1, 0)
        elif self.pre is None:
            pre = (3, 0)
        else:
            pre = self.pre
        post = -1 if self.post is None else self.post
        dev = float("inf") if self.dev is None else self.dev
        local = () if self.local is None else tuple((1, int(p), "") if p.isdigit() else (0, 0, p)
                                                    for p in re.split(r"[-_.]", self.local))
        return (self.epoch, tuple(rel), pre, post, dev, local)

    def __lt__(self, other: "Version") -> bool:
        return self.key() < other.key()

    def __le__(self, other: "Version") -> bool:
        return self.key() <= other.key()

    def __gt__(self, other: "Version") -> bool:
        return self.key() > other.key()

    def __ge__(self, other: "Version") -> bool:
        return self.key() >= other.key()

    def __eq__(self, other: object) -> bool:
        return isinstance(other, Version) and self.key() == other.key()

    def __hash__(self) -> int:
        return hash(self.key())

    def __str__(self) -> str:
        return self.text


_SPEC_RE = re.compile(r"^\s*(?P<op>~=|==|!=|<=|>=|<|>)\s*(?P<ver>[A-Za-z0-9.!+_*-]+)\s*$")


@dataclass(frozen=True)
class Specifier:
    op: str
    version: str  # as written, may end in ".*" for ==/!=

    def __str__(self) -> str:
        return f"{self.op}{self.version}"


def parse_specifier_set(text: str) -> list[Specifier]:
    """Parse '>=1.2,<2' into Specifiers; raise InvalidVersion on anything outside the subset."""
    specs = []
    if not text.strip():
        return specs
    for part in text.split(","):
        m = _SPEC_RE.match(part)
        if not m:
            raise InvalidVersion(f"unsupported specifier clause {part.strip()!r}")
        op, ver = m["op"], m["ver"]
        if ver.endswith(".*"):
            if op not in ("==", "!="):
                raise InvalidVersion(f"'.*' is only valid with == or != ({part.strip()!r})")
            pv = Version.parse(ver[:-2])
            if pv.pre or pv.post is not None or pv.dev is not None or pv.local:
                raise InvalidVersion(f"'.*' prefixes must be release-only in this subset ({part.strip()!r})")
        else:
            v = Version.parse(ver)
            if op == "~=" and len(v.release) < 2:
                raise InvalidVersion(f"~= needs at least two release segments ({part.strip()!r})")
            if v.local and op not in ("==", "!="):
                raise InvalidVersion(f"local version labels are only valid with == or != ({part.strip()!r})")
        specs.append(Specifier(op, ver))
    return specs


def _base(v: Version) -> tuple:
    """(epoch, release without trailing zeros): PEP 440's 'base version' for comparisons."""
    return v.key()[:2]


def _prefix_match(candidate: Version, prefix: str) -> bool:
    """`==X.Y.*` semantics; prefixes are release-only (enforced in parse_specifier_set)."""
    p = Version.parse(prefix)
    if p.epoch != candidate.epoch:
        return False
    n = len(p.release)
    padded = tuple(candidate.release) + (0,) * max(0, n - len(candidate.release))
    return padded[:n] == p.release


def specifier_contains(spec: Specifier, candidate: Version) -> bool:
    op, raw = spec.op, spec.version
    if raw.endswith(".*"):
        hit = _prefix_match(candidate, raw[:-2])
        return hit if op == "==" else not hit
    v = Version.parse(raw)
    # Local labels on the candidate are ignored unless the specifier names one.
    c = candidate if (v.local and op in ("==", "!=")) else candidate.public
    if op == "==":
        return c == v
    if op == "!=":
        return c != v
    if op == ">=":
        return c >= v
    if op == "<=":
        return c <= v
    if op == ">":
        # PEP 440: >V never matches a local version of V, and excludes post-releases of V unless V
        # is itself a post-release. "Of V" means V exactly (packaging >= 26.1 reads it this way;
        # older packaging compared base versions, so it also rejected 1.0+cpu for >1.0rc1 and
        # 1.0.post1 for >1.0.dev0). Exclusive bounds compare the full candidate, local included.
        if not candidate > v:
            return False
        if candidate.public == v:  # V+local
            return False
        if v.dev is not None or v.post is not None:
            return True
        same_family = (candidate.epoch, _base(candidate)[1], candidate.pre) == (v.epoch, _base(v)[1], v.pre)
        return not (same_family and candidate.post is not None)  # V.postN[.devM][+local]
    if op == "<":
        # PEP 440: <V excludes pre-releases of V unless V itself is a pre-release. A final or
        # post-release V therefore behaves like <V.dev0 (packaging >= 26.1: <1.0.post1 admits 1.0rc1).
        bound = v if v.is_prerelease else Version(v.epoch, v.release, v.pre, v.post, 0, None, f"{v.text}.dev0")
        return candidate < bound
    if op == "~=":
        # Keep the epoch: _prefix_match compares it, and a bare "1" would mean epoch 0.
        prefix = (f"{v.epoch}!" if v.epoch else "") + ".".join(str(p) for p in v.release[:-1])
        return c >= v and _prefix_match(c, prefix)
    raise InvalidVersion(f"unsupported operator {op!r}")


def contains(specs: Iterable[Specifier], candidate: str | Version) -> bool:
    """True when `candidate` satisfies every clause (pre-releases allowed, like `pip check`)."""
    c = candidate if isinstance(candidate, Version) else Version.parse(candidate)
    return all(specifier_contains(s, c) for s in specs)


# ---------------------------------------------------------------------------
# version_constraints entries:  "<dist><specifier>: <API relied on> (<note>)"
# ---------------------------------------------------------------------------
_CONSTRAINT_RE = re.compile(
    r"^\s*(?P<name>[A-Za-z0-9](?:[A-Za-z0-9._-]*[A-Za-z0-9])?)"
    r"(?:\[[A-Za-z0-9._,\s-]*\])?"           # optional extras, ignored
    r"\s*(?P<spec>(?:~=|==|!=|<=|>=|<|>)[^:]*?)?\s*:\s*(?P<rest>.*)$",
    re.DOTALL,
)


@dataclass
class Constraint:
    raw: str
    status: str  # "parsed" | "free-text" | "invalid-specifier"
    written_name: str | None = None
    dist: str | None = None
    specifiers: list[Specifier] = field(default_factory=list)
    api: str = ""
    error: str | None = None

    @property
    def spec_text(self) -> str:
        return ",".join(str(s) for s in self.specifiers)


def parse_constraint(entry: str) -> Constraint:
    if not isinstance(entry, str):
        return Constraint(raw=str(entry), status="free-text", error="entry is not a string")
    m = _CONSTRAINT_RE.match(entry)
    if not m:
        return Constraint(raw=entry, status="free-text",
                          error="no leading '<dist><specifier>:' requirement")
    name, spec = m["name"], (m["spec"] or "")
    try:
        specs = parse_specifier_set(spec)
    except InvalidVersion as e:
        return Constraint(raw=entry, status="invalid-specifier", written_name=name, dist=normalize_dist(name),
                          api=m["rest"].strip(), error=str(e))
    return Constraint(raw=entry, status="parsed", written_name=name, dist=normalize_dist(name),
                      specifiers=specs, api=m["rest"].strip())


# ---------------------------------------------------------------------------
# Policy
# ---------------------------------------------------------------------------
class PolicyError(ValueError):
    pass


_POLICY_KEYS = {"$comment", "policy_version", "default_max_age_days", "domain_max_age_days",
                "capability_max_age_days", "skill_max_age_days", "ignore_imports", "known_stale_patterns",
                "acknowledged_inconclusive"}
_PATTERN_KEYS = {"id", "pattern", "level", "message", "claim"}
_ACK_REQUIRED = {"http_status", "reason", "acknowledged_on"}


@dataclass
class StalePattern:
    id: str
    regex: re.Pattern
    level: str
    message: str
    claim: str | None


@dataclass
class AckInconclusive:
    """A URL (or host) that answers automated checks with a fixed non-2xx status (e.g. a 403 bot wall)
    and that a maintainer has accepted as such (docs/EVIDENCE.md, "Bot walls"). verify_online.py
    reports a match as 'acknowledged' instead of 'inconclusive' up to and including `expires_on`
    (the entry's own date, at most default_max_age_days after acknowledged_on; that is also the
    default), so it can't silence a URL forever."""
    url: str | None
    host: str | None
    http_status: frozenset[int]
    reason: str
    acknowledged_on: dt.date
    expires_on: dt.date

    def lapsed(self, today: dt.date) -> bool:
        """True once `today` is after expires_on. The caller supplies today (an injectable clock)."""
        return today > self.expires_on

    def matches(self, url: str, status: int | None) -> bool:
        if status not in self.http_status:
            return False
        if self.url is not None:
            return url == self.url
        return urllib.parse.urlsplit(url).hostname == self.host


@dataclass
class Policy:
    default_max_age_days: int
    domain: dict[str, int]
    capability: dict[str, int]
    skill: dict[str, int]
    ignore_imports: set[str]
    patterns: list[StalePattern]
    path: str = "<inline>"
    acknowledged: list[AckInconclusive] = field(default_factory=list)

    def max_age_for(self, slug: str | None, domain: str | None = None,
                    capabilities: Iterable[str] = ()) -> tuple[int, str]:
        """Most specific rule wins: skill > strictest matching capability > domain > default."""
        if slug and slug in self.skill:
            return self.skill[slug], f"skill override '{slug}'"
        caps = [(self.capability[c], c) for c in capabilities or () if c in self.capability]
        if caps:
            days, cap = min(caps)
            return days, f"capability override '{cap}'"
        if domain and domain in self.domain:
            return self.domain[domain], f"domain override '{domain}'"
        return self.default_max_age_days, "default"


def _positive_int(v) -> bool:
    return isinstance(v, int) and not isinstance(v, bool) and v > 0


def validate_policy(data, skill_slugs: Iterable[str] | None = None,
                    capabilities: Iterable[str] | None = None) -> list[str]:
    """Return every problem in a policy document (empty == valid).

    Unknown skill/capability keys are only checked when the caller supplies the
    known sets: a typo there would silently disable an override.
    """
    errs: list[str] = []
    if not isinstance(data, dict):
        return ["policy must be a JSON object"]
    for k in sorted(set(data) - _POLICY_KEYS):
        errs.append(f"unknown top-level key {k!r}")
    if data.get("policy_version") != 1:
        errs.append("policy_version must be 1")
    if not _positive_int(data.get("default_max_age_days")):
        errs.append("default_max_age_days must be a positive integer")

    known = {"domain_max_age_days": set(av.DOMAIN),
             "skill_max_age_days": set(skill_slugs) if skill_slugs is not None else None,
             "capability_max_age_days": set(capabilities) if capabilities is not None else None}
    for section, allowed in known.items():
        block = data.get(section, {})
        if not isinstance(block, dict):
            errs.append(f"{section} must be an object")
            continue
        for key, entry in block.items():
            where = f"{section}.{key}"
            if allowed is not None and key not in allowed:
                errs.append(f"{where}: unknown {section.split('_')[0]} {key!r}")
            if not isinstance(entry, dict) or set(entry) != {"max_age_days", "reason"}:
                errs.append(f"{where}: must be an object with exactly 'max_age_days' and 'reason'")
                continue
            if not _positive_int(entry["max_age_days"]):
                errs.append(f"{where}.max_age_days must be a positive integer")
            if not isinstance(entry["reason"], str) or not entry["reason"].strip():
                errs.append(f"{where}.reason must be a non-empty string (overrides need a justification)")

    ign = data.get("ignore_imports", {})
    if not isinstance(ign, dict) or not all(isinstance(k, str) and isinstance(v, str) and v.strip()
                                            for k, v in ign.items()):
        errs.append("ignore_imports must map module name -> non-empty reason")

    pats = data.get("known_stale_patterns", [])
    if not isinstance(pats, list):
        errs.append("known_stale_patterns must be a list")
        pats = []
    seen: set[str] = set()
    for i, p in enumerate(pats):
        where = f"known_stale_patterns[{i}]"
        if not isinstance(p, dict):
            errs.append(f"{where}: must be an object")
            continue
        missing = _PATTERN_KEYS - set(p)
        extra = set(p) - _PATTERN_KEYS
        if missing:
            errs.append(f"{where}: missing key(s) {sorted(missing)}")
        if extra:
            errs.append(f"{where}: unknown key(s) {sorted(extra)}")
        pid = p.get("id")
        if not isinstance(pid, str) or not re.match(av.SLUG_PATTERN, pid):
            errs.append(f"{where}.id must be a kebab-case string")
        elif pid in seen:
            errs.append(f"{where}.id {pid!r} is duplicated")
        else:
            seen.add(pid)
        try:
            re.compile(p.get("pattern"))
        except (re.error, TypeError) as e:
            errs.append(f"{where}.pattern does not compile: {e}")
        if p.get("level") not in ("STALE", "REVIEW_DUE"):
            errs.append(f"{where}.level must be 'STALE' or 'REVIEW_DUE'")
        if not isinstance(p.get("message"), str) or not p.get("message", "").strip():
            errs.append(f"{where}.message must be a non-empty string")
        if p.get("claim") is not None and not isinstance(p.get("claim"), str):
            errs.append(f"{where}.claim must be a claims.json id or null")

    acks = data.get("acknowledged_inconclusive", [])
    if not isinstance(acks, list):
        errs.append("acknowledged_inconclusive must be a list")
        acks = []
    for i, a in enumerate(acks):
        where = f"acknowledged_inconclusive[{i}]"
        if not isinstance(a, dict):
            errs.append(f"{where}: must be an object")
            continue
        missing = _ACK_REQUIRED - set(a)
        extra = set(a) - _ACK_REQUIRED - {"url", "host", "expires_on"}
        if missing:
            errs.append(f"{where}: missing key(s) {sorted(missing)}")
        if extra:
            errs.append(f"{where}: unknown key(s) {sorted(extra)}")
        if ("url" in a) == ("host" in a):
            errs.append(f"{where}: give exactly one of 'url' or 'host'")
        elif "url" in a and not (isinstance(a["url"], str) and re.match(r"^https?://\S+$", a["url"])):
            errs.append(f"{where}.url must be an http(s) URL")
        elif "host" in a and not (isinstance(a["host"], str) and re.match(r"^[a-z0-9.-]+$", a["host"])):
            errs.append(f"{where}.host must be a lower-case hostname")
        st = a.get("http_status")
        # Only fixed HTTP answers can be acknowledged: a timeout or DNS failure has no status and
        # must keep surfacing, and 404/410 is 'broken', which is never downgraded.
        if not (isinstance(st, list) and st and all(isinstance(x, int) and not isinstance(x, bool)
                                                    and 400 <= x <= 599 and x not in (404, 410) for x in st)):
            errs.append(f"{where}.http_status must be a non-empty list of 4xx/5xx codes other than 404/410")
        if not isinstance(a.get("reason"), str) or not a.get("reason", "").strip():
            errs.append(f"{where}.reason must be a non-empty string")
        try:
            acked = dt.date.fromisoformat(a.get("acknowledged_on"))
        except (TypeError, ValueError):
            errs.append(f"{where}.acknowledged_on must be a YYYY-MM-DD date")
            acked = None
        if "expires_on" in a:
            try:
                expires = dt.date.fromisoformat(a["expires_on"])
            except (TypeError, ValueError):
                errs.append(f"{where}.expires_on must be a YYYY-MM-DD date")
                expires = None
            max_age = data.get("default_max_age_days")
            if acked and expires and expires < acked:
                errs.append(f"{where}.expires_on is before acknowledged_on")
            elif acked and expires and _positive_int(max_age) and (expires - acked).days > max_age:
                errs.append(f"{where}.expires_on is more than default_max_age_days ({max_age}) after "
                            "acknowledged_on; an acknowledgement must lapse and be re-confirmed")
    return errs


def policy_from_dict(data: dict, path: str = "<inline>", skill_slugs=None, capabilities=None) -> Policy:
    errs = validate_policy(data, skill_slugs, capabilities)
    if errs:
        raise PolicyError(f"{path}: invalid freshness policy:\n  - " + "\n  - ".join(errs))

    def days(section: str) -> dict[str, int]:
        return {k: v["max_age_days"] for k, v in data.get(section, {}).items()}

    return Policy(
        default_max_age_days=data["default_max_age_days"],
        domain=days("domain_max_age_days"),
        capability=days("capability_max_age_days"),
        skill=days("skill_max_age_days"),
        ignore_imports=set(data.get("ignore_imports", {})),
        patterns=[StalePattern(p["id"], re.compile(p["pattern"]), p["level"], p["message"], p["claim"])
                  for p in data.get("known_stale_patterns", [])],
        path=path,
        acknowledged=[_ack_from_dict(a, data["default_max_age_days"]) for a in data.get("acknowledged_inconclusive", [])],
    )


def _ack_from_dict(a: dict, default_max_age_days: int) -> AckInconclusive:
    acked = dt.date.fromisoformat(a["acknowledged_on"])
    expires = (dt.date.fromisoformat(a["expires_on"]) if "expires_on" in a
               else acked + dt.timedelta(days=default_max_age_days))
    return AckInconclusive(a.get("url"), a.get("host"), frozenset(a["http_status"]), a["reason"], acked, expires)


def load_policy(path: Path = DEFAULT_POLICY_PATH, skills: list[sl.Skill] | None = None) -> Policy:
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except FileNotFoundError as e:
        raise PolicyError(f"{path}: policy file not found") from e
    except json.JSONDecodeError as e:
        raise PolicyError(f"{path}: not valid JSON ({e})") from e
    slugs = caps = None
    if skills is not None:
        slugs = {s.slug for s in skills}
        caps = {c for s in skills for c in (s.frontmatter.get("capabilities") or []) if isinstance(c, str)}
    return policy_from_dict(data, _display_path(Path(path)), slugs, caps)


def _display_path(path: Path) -> str:
    """Repo-relative POSIX path when possible, so reports are identical across machines."""
    try:
        return path.resolve().relative_to(sl.REPO_ROOT).as_posix()
    except ValueError:
        return str(path)


# ---------------------------------------------------------------------------
# Checks
# ---------------------------------------------------------------------------
IMPORT_RE = re.compile(r"^\s*(?:import|from)\s+([a-zA-Z0-9_]+)", re.MULTILINE)
_STDLIB = set(getattr(sys, "stdlib_module_names", ())) | {"__future__"}


def _finding(skill: str, level: str, kind: str, code: str, message: str, **extra) -> dict:
    return {"skill": skill, "level": level, "kind": kind, "code": code, "message": message, **extra}


def installed_version(dist: str) -> str | None:
    try:
        return importlib.metadata.version(dist)
    except importlib.metadata.PackageNotFoundError:
        return None


def local_version_findings(slug: str, constraints: list[Constraint],
                           lookup: Callable[[str], str | None] = installed_version) -> list[dict]:
    out = []
    for c in constraints:
        if c.status != "parsed":
            continue
        got = lookup(c.dist)
        base = {"dist": c.dist, "specifier": c.spec_text, "installed": got}
        if got is None:
            out.append(_finding(slug, "SKIPPED", "local-version-check", "not-installed",
                                f"{c.dist} not installed in this environment; nothing compared", **base))
            continue
        try:
            ver = Version.parse(got)
        except InvalidVersion:
            out.append(_finding(slug, "SKIPPED", "local-version-check", "installed-version-unparseable",
                                f"{c.dist} installed as {got!r}, which the PEP 440 subset cannot parse", **base))
            continue
        if not c.specifiers:
            out.append(_finding(slug, "INFO", "local-version-check", "no-specifier",
                                f"{c.dist} {got} installed; entry has no specifier to compare", **base))
        elif contains(c.specifiers, ver):
            out.append(_finding(slug, "OK", "local-version-check", "satisfied",
                                f"{c.dist} {got} satisfies {c.spec_text} (this environment only)", **base))
        else:
            out.append(_finding(slug, "REVIEW_DUE", "local-version-check", "not-satisfied",
                                f"{c.dist} {got} installed here does NOT satisfy {c.spec_text}; either the "
                                f"environment or the skill's constraint is out of date", **base))
    return out


# ---------------------------------------------------------------------------
# Backing claims. A STALE pattern fails CI for every skill it matches, so that verdict is
# only issued when the claim behind it (docs/claims.json) was actually checked. Otherwise a
# match is still reported, as REVIEW_DUE with the reason: flagged, but not presented as proof.
# ---------------------------------------------------------------------------
# Claim statuses that may carry a STALE verdict. 'review-due' is a verified claim whose review
# clock ran out: it keeps failing CI (and check_claims.py asks for a re-check) so that an expired
# clock cannot quietly switch a verified rule off.
# A STALE verdict says an API is gone, so its backing claim must rest on evidence about the API
# itself: a test that ran it, or a person who read the source. local-version-check and
# automated-doc-check are recorded kinds, but by VERIFICATION_KINDS neither says anything about API
# behaviour or page content, so they cannot back a CI failure.
STALE_BACKING_VERIFICATION = ("runtime-test", "human-review")
STALE_BACKING_STATUS = ("current", "review-due")


class ClaimsRegistryError(ValueError):
    pass


def load_claims_index(path: Path = DEFAULT_CLAIMS_PATH) -> dict[str, dict]:
    """id -> claim record. Full validation is check_claims.py's job; this needs only ids and states."""
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except FileNotFoundError as e:
        raise ClaimsRegistryError(f"{path}: claims registry not found") from e
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as e:
        raise ClaimsRegistryError(f"{path}: cannot read claims registry ({e})") from e
    claims = data.get("claims") if isinstance(data, dict) else None
    if not isinstance(claims, list):
        raise ClaimsRegistryError(f"{path}: claims registry has no 'claims' list")
    return {c["id"]: c for c in claims if isinstance(c, dict) and isinstance(c.get("id"), str)}


def stale_backing_problem(pattern: StalePattern, claims: dict[str, dict]) -> str | None:
    """Why a STALE pattern may not fail CI with this registry; None when its backing claim was verified."""
    if pattern.claim is None:
        return "the pattern cites no claim in docs/claims.json"
    claim = claims.get(pattern.claim)
    if claim is None:
        return f"its backing claim {pattern.claim!r} is not in the claims registry"
    if (claim.get("verification") not in STALE_BACKING_VERIFICATION
            or claim.get("status") not in STALE_BACKING_STATUS):
        return (f"its backing claim {pattern.claim!r} is not verified by a runtime test or human review, "
                f"or is disputed (verification {claim.get('verification')!r}, status {claim.get('status')!r})")
    return None


def effective_level(pattern: StalePattern, claims: dict[str, dict] | None) -> tuple[str, str | None]:
    """(level to report, why it differs from the policy level). claims=None means no gating (library use)."""
    if pattern.level != "STALE" or claims is None:
        return pattern.level, None
    problem = stale_backing_problem(pattern, claims)
    return ("STALE", None) if problem is None else ("REVIEW_DUE", problem)


def stale_patterns_not_applied(policy: Policy, claims: dict[str, dict] | None) -> list[dict]:
    """Every STALE pattern currently reported as REVIEW_DUE, whether or not any skill matches it."""
    out = []
    for p in policy.patterns:
        level, reason = effective_level(p, claims)
        if reason is not None:
            out.append({"pattern": p.id, "claim": p.claim, "reported_as": level, "reason": reason})
    return out


def check_skill(skill: sl.Skill, today: dt.date, policy: Policy | None = None,
                version_lookup: Callable[[str], str | None] | None = None,
                claims: dict[str, dict] | None = None) -> list[dict]:
    """All offline findings for one skill. `version_lookup` enables local-version-check; `claims`
    (from load_claims_index) gates STALE patterns on their backing claim. The CLI always passes it."""
    policy = policy or load_policy()
    slug = skill.slug
    fm = skill.frontmatter
    findings = []

    for p in policy.patterns:
        if p.regex.search(skill.raw):
            level, reason = effective_level(p, claims)
            if reason is None:
                findings.append(_finding(slug, level, "static-pattern", p.id, p.message, claim=p.claim))
            else:
                findings.append(_finding(slug, level, "static-pattern", p.id,
                                         f"{p.message} [policy level STALE not applied: {reason}; verify the "
                                         "claim (docs/EVIDENCE.md) before treating this as a removed API]",
                                         claim=p.claim, policy_level=p.level, level_reason=reason))

    max_age, rule = policy.max_age_for(slug, fm.get("domain"), fm.get("capabilities") or [])
    lv = fm.get("last_verified")
    if not lv:
        findings.append(_finding(slug, "REVIEW_DUE", "review-clock", "no-last-verified", "no last_verified date set"))
    elif not isinstance(lv, str) or not sl.is_calendar_date(lv):
        findings.append(_finding(slug, "STALE", "review-clock", "invalid-last-verified",
                                 f"last_verified {lv!r} is not a valid YYYY-MM-DD date"))
    else:
        age = (today - dt.date.fromisoformat(lv)).days
        if age < 0:
            findings.append(_finding(slug, "REVIEW_DUE", "review-clock", "last-verified-in-future",
                                     f"last_verified {lv} is after today ({today.isoformat()})"))
        elif age > max_age:
            findings.append(_finding(slug, "REVIEW_DUE", "review-clock", "review-overdue",
                                     f"last_verified is {age} days old (limit {max_age}, {rule})",
                                     age_days=age, max_age_days=max_age, rule=rule))

    raw_constraints = fm.get("version_constraints") or []
    if not isinstance(raw_constraints, list):
        raw_constraints = [raw_constraints]
    constraints = [parse_constraint(e) for e in raw_constraints]
    for c in constraints:
        if c.status == "free-text":
            findings.append(_finding(slug, "REVIEW_DUE", "metadata", "constraint-free-text",
                                     f"unparseable (free text) - review: {c.raw[:90]!r}", entry=c.raw))
        elif c.status == "invalid-specifier":
            findings.append(_finding(slug, "REVIEW_DUE", "metadata", "constraint-invalid-specifier",
                                     f"{c.written_name}: {c.error}", entry=c.raw))
        elif not c.specifiers:
            findings.append(_finding(slug, "INFO", "metadata", "constraint-no-specifier",
                                     f"{c.written_name}: names a package but no version specifier; nothing to "
                                     "compare", entry=c.raw, dist=c.dist))
        if c.status != "free-text" and c.written_name and normalize_dist(c.written_name) != \
                re.sub(r"[-_.]+", "-", c.written_name).lower():
            findings.append(_finding(slug, "INFO", "metadata", "constraint-name-aliased",
                                     f"{c.written_name!r} read as PyPI dist {c.dist!r}; prefer the dist name",
                                     entry=c.raw, dist=c.dist))

    modules: set[str] = set()
    for _, code in skill.code_blocks(lang="python"):
        modules.update(m for m in IMPORT_RE.findall(code) if m not in _STDLIB and m not in policy.ignore_imports)
    if modules and not raw_constraints:
        findings.append(_finding(slug, "REVIEW_DUE", "metadata", "no-version-constraints",
                                 f"imports {sorted(modules)} but declares no version_constraints"))
    elif modules:
        covered = {c.dist for c in constraints if c.dist}
        missing = sorted(m for m in modules if normalize_dist(m) not in covered)
        if missing:
            findings.append(_finding(slug, "INFO", "metadata", "import-without-constraint",
                                     f"imports {missing} with no matching version_constraints entry "
                                     f"(dists: {[normalize_dist(m) for m in missing]})"))

    if version_lookup is not None:
        findings.extend(local_version_findings(slug, constraints, version_lookup))
    return findings


def build_report(skills: list[sl.Skill], today: dt.date, policy: Policy,
                 version_lookup: Callable[[str], str | None] | None = None,
                 claims: dict[str, dict] | None = None, claims_path: str | None = None) -> dict:
    findings = []
    for s in skills:
        findings.extend(check_skill(s, today, policy, version_lookup, claims))
    counts = {lvl: sum(f["level"] == lvl for f in findings) for lvl in LEVELS}
    kinds_run = ["static-pattern", "review-clock", "metadata"] + (["local-version-check"] if version_lookup else [])
    return {
        "tool": "check_freshness",
        "report_version": 2,
        "today": today.isoformat(),
        "policy": policy.path,
        "claims": claims_path if claims is not None else None,
        "stale_patterns_not_applied": stale_patterns_not_applied(policy, claims),
        "kinds_run": kinds_run,
        "kinds": {k: FINDING_KINDS[k] for k in kinds_run},
        "levels": LEVELS,
        "skills_scanned": len(skills),
        "counts": counts,
        "findings": findings,
        "note": "Flags for human review. No finding means 'nothing matched', NOT 'verified current'. Network "
                "checks (automated-doc-check) run only in .github/workflows/freshness-scheduled.yml.",
    }


def print_human(report: dict, verbose: bool = False) -> None:
    order = ["STALE", "REVIEW_DUE", "SKIPPED", "INFO", "OK"]
    shown = order if verbose else ["STALE", "REVIEW_DUE", "SKIPPED"]
    for lvl in shown:
        for f in report["findings"]:
            if f["level"] == lvl:
                print(f"{lvl:10s} [{f['kind']}] {f['skill']:24s} {f['message']}")
    for sp in report.get("stale_patterns_not_applied", []):
        print(f"NOTE       known_stale_patterns '{sp['pattern']}' is level STALE in the policy but is reported as "
              f"{sp['reported_as']}: {sp['reason']}")
    c = report["counts"]
    print(f"\n{c['STALE']} STALE, {c['REVIEW_DUE']} REVIEW_DUE, {c['SKIPPED']} SKIPPED, {c['INFO']} INFO, "
          f"{c['OK']} OK across {report['skills_scanned']} skills (policy: {report['policy']}, "
          f"today: {report['today']})" + ("" if verbose else "; --verbose shows INFO/OK"))
    print("\nWhat each kind means:")
    for k, meaning in report["kinds"].items():
        print(f"  {k:20s} {meaning}")
    print(f"\nNote: {report['note']}")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    ap.add_argument("--json", action="store_true", help="print the JSON report")
    ap.add_argument("--policy", type=Path, default=DEFAULT_POLICY_PATH, help="freshness policy JSON")
    ap.add_argument("--claims", type=Path, default=DEFAULT_CLAIMS_PATH,
                    help="claims registry; a STALE pattern fails CI only when its backing claim here is verified")
    ap.add_argument("--validate-policy", action="store_true", help="only validate the policy file")
    ap.add_argument("--local-versions", action="store_true",
                    help="also compare specifiers with packages installed in this environment (offline)")
    ap.add_argument("--today", type=dt.date.fromisoformat, default=None, help="override today's date (YYYY-MM-DD)")
    ap.add_argument("--root", type=Path, default=sl.REPO_ROOT, help="directory containing the skill folders")
    ap.add_argument("--verbose", action="store_true", help="human output: also list INFO and OK findings")
    args = ap.parse_args(argv)

    skills = sl.load_all_skills(args.root)
    try:
        policy = load_policy(args.policy, skills)
    except PolicyError as e:
        print(f"ERROR {e}", file=sys.stderr)
        return 2
    if args.validate_policy:
        print(f"OK {args.policy}: valid freshness policy ({len(policy.patterns)} known-stale patterns)")
        return 0

    try:
        claims = load_claims_index(args.claims)
    except ClaimsRegistryError as e:
        print(f"ERROR {e}", file=sys.stderr)
        return 2
    report = build_report(skills, args.today or dt.date.today(), policy,
                          installed_version if args.local_versions else None, claims, _display_path(args.claims))
    if args.json:
        print(json.dumps(report, indent=2))
    else:
        print_human(report, args.verbose)
    # Only STALE (known-removed API / unparseable date) fails CI; the rest is a review backlog.
    return 1 if report["counts"]["STALE"] else 0


if __name__ == "__main__":
    sys.exit(main())
