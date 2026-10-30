"""Tests for the native Claude Code plugin/marketplace packaging
(scripts/generate_plugins.py) — covers marketplace JSON validity, plugin
manifest validity, every marketplace entry resolving, every plugin containing
valid/discoverable skills, no duplicate skill content (byte-identical to the
canonical source), and reproducibility (generated output has zero drift).

The `claude`-CLI-backed tests additionally run the real `claude plugin
validate` / `plugin marketplace add` / `plugin install` / `plugin update`
commands against an isolated CLAUDE_CONFIG_DIR and HOME (never the developer's
real ~/.claude config) — skipped if the `claude` CLI isn't on PATH. They assert
exit codes and `plugin list` text only: none of them shows that Claude Code loads
or selects a skill at runtime.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import functools
import unittest
import unittest.mock
from pathlib import Path

from testutil import REPO_ROOT, make_skill
sys.path.insert(0, str(REPO_ROOT / "scripts"))
sys.path.insert(0, str(REPO_ROOT / "schema"))
import export_skills as ex  # noqa: E402
import generate_plugins as gp  # noqa: E402
import skills_lib as sl  # noqa: E402
import allowed_values as av  # noqa: E402

BASH = shutil.which("bash")
CLI_TIMEOUT = 120  # seconds per `claude` call; a local-folder marketplace needs no network


def _claude_path() -> tuple[str | None, str]:
    """Resolve `claude` once, through `bash -lc` with the real environment: on Windows,
    `claude` commonly resolves to a shim (e.g. an ASBX Toolbox wrapper) that only runs
    correctly when exec'd by a POSIX shell, the same reason test_install.py shells out to
    bash rather than invoking install.sh directly. Resolving it before HOME is redirected
    keeps PATH entries a login profile adds. ML_AI_SKILLS_CLAUDE_BIN selects a specific
    binary (e.g. an unpacked public @anthropic-ai/claude-code build).

    Returns (path or None, reason it was not found). An override that is not a file is a
    misconfiguration, not an absent CLI: TestClaudeBinOverride fails on it."""
    if not BASH:
        return None, "bash not found on PATH (the claude CLI is run through bash)"
    override = os.environ.get("ML_AI_SKILLS_CLAUDE_BIN")
    if override:
        if Path(override).is_file():
            return override, ""
        return None, f"ML_AI_SKILLS_CLAUDE_BIN={override!r} is not a file"
    # A plain `bash -c` sees the inherited PATH and is fast; a login shell can take over 10 s
    # on a loaded Windows machine (measured 13 s on 2026-10-29), which used to time out and
    # silently skip every CLI test, so it is only the fallback and gets a longer timeout.
    for flag, timeout in (("-c", 30), ("-lc", 60)):
        try:
            probe = subprocess.run(
                [BASH, flag, "command -v claude"], capture_output=True, text=True, timeout=timeout,
            )
        except (OSError, subprocess.TimeoutExpired):
            continue
        path = probe.stdout.strip().splitlines()
        if probe.returncode == 0 and path:
            return path[0], ""
    return None, "claude CLI not found on PATH (via bash -c / bash -lc)"


@functools.lru_cache(maxsize=1)
def claude_probe() -> tuple[str | None, str]:
    """_claude_path(), run on first use and cached for the process. Importing this module
    must not start a subprocess: tools such as scripts/generate_readme_stats.py import every
    test module to count tests, and an import-time probe (up to 90 s on a loaded machine)
    made those imports slow or, on a timeout, aborted the whole unittest run."""
    return _claude_path()


def require_claude() -> None:
    """Skip the calling test class when the claude CLI is not available (from setUpClass)."""
    path, reason = claude_probe()
    if path is None:
        raise unittest.SkipTest(reason)


# Module attributes kept for importers (tests/test_claude_cli_integration.py reads
# CLAUDE_AVAILABLE, CLAUDE_MISSING_REASON and CLAUDE_PATH); resolved lazily via PEP 562.
_LAZY_PROBE_ATTRS = {
    "CLAUDE_PATH": lambda: claude_probe()[0],
    "CLAUDE_MISSING_REASON": lambda: claude_probe()[1],
    "CLAUDE_AVAILABLE": lambda: claude_probe()[0] is not None,
}


def __getattr__(name: str):
    if name in _LAZY_PROBE_ATTRS:
        return _LAZY_PROBE_ATTRS[name]()
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


ALL_SKILLS = sl.load_all_skills()
ALL_SLUGS = {s.slug for s in ALL_SKILLS}

# Variables that could point a CLI at a model provider with the developer's credentials.
# No test here sends a prompt. Stripping these reduces provider configuration, but does not
# remove it: wrapper builds (the ASBX one) write their own provider and credential-export
# settings into the isolated CLAUDE_CONFIG_DIR. The guarantee is only that no command used
# here sends a prompt.
_CREDENTIAL_ENV_PREFIXES = ("ANTHROPIC_", "CLAUDE_CODE_USE_", "AWS_BEARER_TOKEN_BEDROCK")


def isolated_cli_env(tmp: Path) -> dict:
    """Environment for `claude` that cannot read or write the developer's real config:
    CLAUDE_CONFIG_DIR, HOME/USERPROFILE, APPDATA and the XDG dirs all point inside `tmp`."""
    env = {k: v for k, v in os.environ.items() if not k.startswith(_CREDENTIAL_ENV_PREFIXES)}
    dirs = {"CLAUDE_CONFIG_DIR": "claude-config", "HOME": "home", "USERPROFILE": "home",
            "APPDATA": "appdata", "XDG_CONFIG_HOME": "xdg-config", "XDG_DATA_HOME": "xdg-data",
            "XDG_CACHE_HOME": "xdg-cache", "XDG_STATE_HOME": "xdg-state"}
    for var, sub in dirs.items():
        (tmp / sub).mkdir(parents=True, exist_ok=True)
        env[var] = str(tmp / sub)
    # Documented Claude Code switch for auto-update, telemetry and error reporting; reduces
    # background traffic (whether a build then makes none was not measured).
    env["CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC"] = "1"
    return env


def run_claude(*args: str, env: dict | None = None) -> subprocess.CompletedProcess:
    """Run the resolved `claude` with `args` as separate argv entries (no shell parsing).
    Windows backslash paths are normalized to forward slashes, which `claude` (Node or
    native) accepts on Windows and MSYS leaves alone."""
    norm = [str(a).replace("\\", "/") for a in args]
    return subprocess.run(
        [BASH, "-c", 'exec "$0" "$@"', claude_probe()[0], *norm], capture_output=True, env=env,
        encoding="utf-8", errors="replace",  # CLI output can include unicode glyphs (❯, ✔) that
        input="", timeout=CLI_TIMEOUT,       # aren't representable in the Windows console codepage.
    )  # input="": closed stdin so an unexpected confirmation prompt fails fast instead of hanging CI.


def unshipped_files(folder: Path) -> list[str]:
    """Files under a canonical skill folder that plugin packaging (SKILL.md only) would not
    ship, ignoring OS/build litter (the set scripts/export_skills.py leaves out)."""
    def litter(rel: Path) -> bool:
        return (any(part in ex.EXCLUDED_DIRS for part in rel.parts[:-1])
                or rel.name in ex.EXCLUDED_FILES or rel.name.endswith(ex.EXCLUDED_SUFFIXES))

    return sorted(p.relative_to(folder).as_posix() for p in folder.rglob("*")
                  if p.is_file() and p.relative_to(folder).as_posix() != "SKILL.md"
                  and not litter(p.relative_to(folder)))


class TestBuildPlan(unittest.TestCase):
    """Pure logic, synthetic fixtures — domain partitioning correctness."""

    def setUp(self):
        self.skills = [
            make_skill("alpha", domain="foundations"),
            make_skill("beta", domain="classical-ml"),
            make_skill("gamma", domain="classical-ml"),
        ]

    def test_all_in_one_contains_every_skill(self):
        plan = gp.build_plan(self.skills)
        self.assertEqual(set(plan[gp.ALL_PLUGIN_NAME]["slugs"]), {"alpha", "beta", "gamma"})

    def test_domain_plugins_partition_with_no_overlap(self):
        plan = gp.build_plan(self.skills)
        domain_slugs = [set(plan[d]["slugs"]) for d in av.DOMAIN]
        union = set().union(*domain_slugs)
        self.assertEqual(union, {"alpha", "beta", "gamma"})
        for i, a in enumerate(domain_slugs):
            for b in domain_slugs[i + 1:]:
                self.assertEqual(a & b, set(), "domain plugins must not overlap")

    def test_domain_plugin_matches_frontmatter_domain(self):
        plan = gp.build_plan(self.skills)
        self.assertEqual(plan["foundations"]["slugs"], ["alpha"])
        self.assertEqual(sorted(plan["classical-ml"]["slugs"]), ["beta", "gamma"])
        self.assertEqual(plan["llm"]["slugs"], [])


class TestRenderDeterminism(unittest.TestCase):
    def test_render_files_is_deterministic(self):
        first = gp.render_files(ALL_SKILLS)
        second = gp.render_files(ALL_SKILLS)
        self.assertEqual(first, second)

    def test_no_check_issues_against_current_repo_state(self):
        """The tree committed to git must already match the generator's
        output — this is the drift gate CI runs."""
        issues = gp.check(ALL_SKILLS)
        self.assertEqual(issues, [], "run `python scripts/generate_plugins.py --write`")


class TestMarketplaceJson(unittest.TestCase):
    def setUp(self):
        self.marketplace = json.loads(gp.MARKETPLACE_PATH.read_text(encoding="utf-8"))

    def test_has_required_top_level_keys(self):
        for key in ("name", "owner", "plugins"):
            self.assertIn(key, self.marketplace)

    def test_every_entry_resolves_to_a_plugin_directory_with_manifest(self):
        for entry in self.marketplace["plugins"]:
            self.assertTrue(entry["source"].startswith("./"), "source must be a same-repo relative path")
            plugin_dir = REPO_ROOT / entry["source"][2:]
            self.assertTrue(plugin_dir.is_dir(), f"{entry['source']} does not resolve to a directory")
            manifest = plugin_dir / ".claude-plugin" / "plugin.json"
            self.assertTrue(manifest.is_file(), f"{entry['source']} has no .claude-plugin/plugin.json")
            self.assertEqual(json.loads(manifest.read_text(encoding="utf-8"))["name"], entry["name"])

    def test_covers_all_in_one_plus_every_domain(self):
        names = {e["name"] for e in self.marketplace["plugins"]}
        self.assertEqual(names, {gp.ALL_PLUGIN_NAME, *av.DOMAIN})


class TestPluginManifests(unittest.TestCase):
    def test_every_plugin_json_is_valid_and_has_required_fields(self):
        for plugin_dir in sorted((REPO_ROOT / "plugins").iterdir()):
            manifest_path = plugin_dir / ".claude-plugin" / "plugin.json"
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            for key in ("name", "version", "description"):
                self.assertIn(key, manifest, f"{plugin_dir.name}: missing '{key}'")
            self.assertEqual(manifest["name"], plugin_dir.name)

    def test_every_plugin_skills_dir_contains_only_real_skills(self):
        for plugin_dir in sorted((REPO_ROOT / "plugins").iterdir()):
            skills_dir = plugin_dir / "skills"
            if not skills_dir.exists():
                continue
            for skill_dir in skills_dir.iterdir():
                self.assertIn(skill_dir.name, ALL_SLUGS, f"{plugin_dir.name} packages unknown slug {skill_dir.name}")
                self.assertTrue((skill_dir / "SKILL.md").is_file())

    def test_all_in_one_plugin_packages_every_published_skill(self):
        packaged = {p.name for p in (REPO_ROOT / "plugins" / gp.ALL_PLUGIN_NAME / "skills").iterdir()}
        self.assertEqual(packaged, ALL_SLUGS)


# "<N> ... Agent Skill(s)" / "<N> self-contained ML/AI Agent Skills": a number followed, within a
# few words, by "Skill"/"Skills"/"Skill(s)". Used to find every count claim in generated text.
COUNT_CLAIM_RE = re.compile(r"\b(\d+)(?:\s+[^\s\d]+){0,5}?\s+Skill(?:s|\(s\))?\b")


def count_claims(text: str) -> list[int]:
    return [int(m.group(1)) for m in COUNT_CLAIM_RE.finditer(text)]


class TestNoHardcodedCounts(unittest.TestCase):
    """Every skill count stated in generated text must equal the real count for that
    plugin's scope - a hardcoded "38" goes stale the moment a skill is added or removed."""

    def test_regex_finds_the_claims_it_is_meant_to_find(self):
        self.assertEqual(count_claims("38 self-contained ML/AI Agent Skills, packaged as one plugin"), [38])
        self.assertEqual(count_claims("All 38 ML/AI Agent Skills from the library"), [38])
        self.assertEqual(count_claims("4 Responsible AI Agent Skill(s) from the library"), [4])
        self.assertEqual(count_claims("covering 8 domains"), [])

    def test_generated_manifests_state_the_real_counts(self):
        plan = gp.build_plan(ALL_SKILLS)
        expected = {name: len(entry["slugs"]) for name, entry in plan.items()}
        checked = 0
        for rel, content in gp.render_files(ALL_SKILLS).items():
            if not rel.endswith(".json"):
                continue
            doc = json.loads(content)
            if rel == ".claude-plugin/marketplace.json":
                scoped = [(gp.ALL_PLUGIN_NAME, doc["metadata"]["description"])]
                scoped += [(e["name"], e["description"]) for e in doc["plugins"]]
            else:
                scoped = [(doc["name"], doc["description"])]
            for name, text in scoped:
                claims = count_claims(text)
                self.assertTrue(claims, f"{rel}: no skill count found in {text!r} (did the wording change?)")
                for n in claims:
                    self.assertEqual(n, expected[name], f"{rel}: {text!r} states {n} skills; '{name}' has "
                                                        f"{expected[name]}")
                checked += 1
        # marketplace metadata + one marketplace entry and one plugin.json per plugin
        self.assertEqual(checked, 1 + 2 * len(plan))

    def test_marketplace_description_tracks_a_synthetic_skill_count(self):
        skills = [make_skill("alpha", domain="foundations"), make_skill("beta", domain="classical-ml"),
                  make_skill("gamma", domain="classical-ml")]
        doc = gp.render_marketplace_json(gp.build_plan(skills), "0.0.0")
        self.assertEqual(count_claims(doc["metadata"]["description"]), [3])
        for entry in doc["plugins"]:
            want = {"ml-ai-skills": 3, "classical-ml": 2, "foundations": 1}.get(entry["name"], 0)
            self.assertEqual(count_claims(entry["description"]), [want], entry["name"])


class TestNoDuplicateContent(unittest.TestCase):
    def test_packaged_skill_md_is_byte_identical_to_canonical_source(self):
        by_slug = sl.by_slug(ALL_SKILLS)
        for plugin_dir in sorted((REPO_ROOT / "plugins").iterdir()):
            skills_dir = plugin_dir / "skills"
            if not skills_dir.exists():
                continue
            for skill_dir in skills_dir.iterdir():
                packaged = (skill_dir / "SKILL.md").read_text(encoding="utf-8")
                canonical = by_slug[skill_dir.name].raw
                self.assertEqual(
                    packaged, canonical,
                    f"{plugin_dir.name}/skills/{skill_dir.name}/SKILL.md diverged from canonical source "
                    "— edit the canonical <slug>/SKILL.md and regenerate, never the copy under plugins/",
                )

    def test_packaged_skill_md_bytes_equal_canonical_file_bytes(self):
        """Bytes, not decoded text: read_text() would hide a CRLF/LF difference."""
        canonical = {s.slug: s.path.read_bytes() for s in ALL_SKILLS}
        checked = 0
        for path in sorted((REPO_ROOT / "plugins").glob("*/skills/*/SKILL.md")):
            slug = path.parent.name
            self.assertIn(slug, canonical, f"{path} packages unknown slug")
            self.assertEqual(path.read_bytes(), canonical[slug],
                             f"{path.relative_to(REPO_ROOT)} is not a byte copy of {slug}/SKILL.md")
            checked += 1
        plan = gp.build_plan(ALL_SKILLS)
        self.assertEqual(checked, sum(len(e["slugs"]) for e in plan.values()))

    def test_render_copies_skill_md_bytes_including_crlf(self):
        with tempfile.TemporaryDirectory() as tmp:
            skill_dir = Path(tmp) / "crlf-skill"
            skill_dir.mkdir()
            text = make_skill("crlf-skill", domain="foundations").raw
            data = text.replace("\n", "\r\n").encode("utf-8")
            (skill_dir / "SKILL.md").write_bytes(data)
            skill = sl.load_skill(skill_dir)
            self.assertNotEqual(skill.raw.encode("utf-8"), data, "fixture must exercise the CRLF case")
            files = gp.render_files([skill])
            self.assertEqual(files["plugins/ml-ai-skills/skills/crlf-skill/SKILL.md"], data)
            self.assertEqual(files["plugins/foundations/skills/crlf-skill/SKILL.md"], data)

    def test_canonical_skill_folders_hold_only_what_packaging_ships(self):
        """generate_plugins.py (like install.sh) ships SKILL.md only. A reference file
        added next to it would be missing from every plugin, breaking its relative links.
        Only files count (an empty directory ships nothing), and OS/build litter that no
        packaging ships (.DS_Store, __pycache__/, node_modules/, *.pyc; the same set
        scripts/export_skills.py leaves out) is ignored."""
        for skill in ALL_SKILLS:
            extra = unshipped_files(skill.path.parent)
            self.assertEqual(extra, [], f"{skill.slug}/ has files the plugins would not ship: {extra}")

    def test_unshipped_files_ignores_litter_but_not_content(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp) / "alpha"
            for rel in ("SKILL.md", ".DS_Store", "__pycache__/x.cpython-313.pyc", "node_modules/m/index.js",
                        "helper.pyc", "references/guide.md", "evals/case.yaml"):
                (folder / rel).parent.mkdir(parents=True, exist_ok=True)
                (folder / rel).write_bytes(b"x")
            (folder / "empty-dir").mkdir()
            self.assertEqual(unshipped_files(folder), ["evals/case.yaml", "references/guide.md"])


class TestMarketplaceReferences(unittest.TestCase):
    """Every cross-reference between marketplace.json, plugin.json, VERSION and the
    plugins/ folder layout agrees with the generator's plan."""

    @classmethod
    def setUpClass(cls):
        cls.marketplace = json.loads(gp.MARKETPLACE_PATH.read_text(encoding="utf-8"))
        cls.plan = gp.build_plan(ALL_SKILLS)
        cls.version = gp.read_version()

    def _manifest(self, name: str) -> dict:
        return json.loads((REPO_ROOT / "plugins" / name / ".claude-plugin" / "plugin.json").read_text(encoding="utf-8"))

    def test_entry_names_are_unique_and_ordered_all_in_one_first(self):
        names = [e["name"] for e in self.marketplace["plugins"]]
        self.assertEqual(len(names), len(set(names)))
        self.assertEqual(names, [gp.ALL_PLUGIN_NAME, *gp.DOMAIN_ORDER])
        self.assertEqual(self.marketplace["name"], gp.MARKETPLACE_NAME)

    def test_sources_stay_inside_plugins_dir(self):
        plugins_root = (REPO_ROOT / "plugins").resolve()
        for entry in self.marketplace["plugins"]:
            src = entry["source"]
            self.assertNotIn("..", Path(src).parts, src)
            self.assertEqual(src, f"./plugins/{entry['name']}")
            self.assertEqual((REPO_ROOT / src).resolve().parent, plugins_root, src)

    def test_no_plugin_dir_is_missing_from_the_marketplace(self):
        on_disk = {p.name for p in (REPO_ROOT / "plugins").iterdir()}
        self.assertEqual(on_disk, {e["name"] for e in self.marketplace["plugins"]})

    def test_versions_agree_with_version_file(self):
        self.assertEqual(self.marketplace["metadata"]["version"], self.version)
        for entry in self.marketplace["plugins"]:
            self.assertEqual(entry["version"], self.version, entry["name"])
            self.assertEqual(self._manifest(entry["name"])["version"], self.version, entry["name"])

    def test_entry_descriptions_match_plugin_manifests(self):
        for entry in self.marketplace["plugins"]:
            manifest = self._manifest(entry["name"])
            self.assertEqual(entry["description"], manifest["description"], entry["name"])
            self.assertEqual(entry["description"], self.plan[entry["name"]]["description"], entry["name"])
            self.assertEqual(manifest["license"], gp.LICENSE)

    def test_each_plugin_ships_exactly_its_planned_skills(self):
        for name, entry in self.plan.items():
            skills_dir = REPO_ROOT / "plugins" / name / "skills"
            on_disk = sorted(p.name for p in skills_dir.iterdir()) if skills_dir.exists() else []
            self.assertEqual(on_disk, entry["slugs"], name)

    def test_domain_plugins_match_frontmatter_domain_on_disk(self):
        domain_of = {s.slug: s.frontmatter.get("domain") for s in ALL_SKILLS}
        for domain in av.DOMAIN:
            skills_dir = REPO_ROOT / "plugins" / domain / "skills"
            for p in (skills_dir.iterdir() if skills_dir.exists() else []):
                self.assertEqual(domain_of.get(p.name), domain, f"plugins/{domain} ships {p.name}")
        self.assertEqual(len(self.plan[gp.ALL_PLUGIN_NAME]["slugs"]), len(sl.iter_skill_dirs()))

    def test_plugin_folders_contain_only_generated_layout(self):
        for name in self.plan:
            plugin_dir = REPO_ROOT / "plugins" / name
            files = sorted(p.relative_to(plugin_dir).as_posix() for p in plugin_dir.rglob("*") if p.is_file())
            want = sorted([".claude-plugin/plugin.json", *(f"skills/{s}/SKILL.md" for s in self.plan[name]["slugs"])])
            self.assertEqual(files, want, name)


class TestCheckDetectsDrift(unittest.TestCase):
    """check() against a scratch tree written by write(): clean, then each kind of drift."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        gp.write(ALL_SKILLS, root=self.root)

    def tearDown(self):
        self._tmp.cleanup()

    def test_written_tree_checks_clean(self):
        self.assertEqual(gp.check(ALL_SKILLS, root=self.root), [])

    def test_stale_missing_and_unexpected_files_are_reported(self):
        rel = f"plugins/{gp.ALL_PLUGIN_NAME}/skills/{ALL_SKILLS[0].slug}/SKILL.md"
        target = self.root / rel
        target.write_bytes(target.read_bytes() + b"\n")
        self.assertEqual(gp.check(ALL_SKILLS, root=self.root), [f"stale generated file (content drift): {rel}"])
        target.unlink()
        self.assertEqual(gp.check(ALL_SKILLS, root=self.root), [f"missing generated file: {rel}"])
        gp.write(ALL_SKILLS, root=self.root)
        (self.root / "plugins" / "stray.txt").write_text("x", encoding="utf-8")
        issues = gp.check(ALL_SKILLS, root=self.root)
        self.assertEqual(len(issues), 1)
        self.assertIn("unexpected file", issues[0])
        self.assertIn("plugins/stray.txt", issues[0])

    def test_write_removes_files_it_no_longer_generates(self):
        (self.root / "plugins" / "old-plugin").mkdir()
        (self.root / "plugins" / "old-plugin" / "x.md").write_text("x", encoding="utf-8")
        gp.write(ALL_SKILLS, root=self.root)
        self.assertFalse((self.root / "plugins" / "old-plugin").exists())
        self.assertEqual(gp.check(ALL_SKILLS, root=self.root), [])


class TestImportRunsNoSubprocess(unittest.TestCase):
    """Importing this module (as scripts/generate_readme_stats.py does to count tests) must
    not probe for the claude CLI: the probe is lazy (claude_probe)."""

    def test_import_starts_no_subprocess(self):
        child = (
            # A Popen subclass, not a function: stdlib modules subclass subprocess.Popen at
            # import time (asyncio.windows_utils), and subprocess.run goes through Popen.
            "import subprocess, sys\n"
            "class _Blocked(subprocess.Popen):\n"
            "    def __init__(self, *a, **k):\n"
            "        raise AssertionError('subprocess started at import time: %r' % (a[:1],))\n"
            "subprocess.Popen = _Blocked\n"
            f"sys.path.insert(0, {str(Path(__file__).resolve().parent)!r})\n"
            "import test_generate_plugins\n"
            "print('imported')\n"
        )
        proc = subprocess.run([sys.executable, "-c", child], capture_output=True, text=True,
                              timeout=120, cwd=REPO_ROOT)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("imported", proc.stdout)


class TestClaudeBinOverride(unittest.TestCase):
    """A wrong ML_AI_SKILLS_CLAUDE_BIN must fail here, not turn every CLI test into a skip."""

    def test_override_names_a_file_when_set(self):
        override = os.environ.get("ML_AI_SKILLS_CLAUDE_BIN")
        if not override:
            self.skipTest("ML_AI_SKILLS_CLAUDE_BIN is not set")
        path, reason = claude_probe()
        self.assertTrue(Path(override).is_file(), reason)
        self.assertEqual(path, override)

    @unittest.skipIf(BASH is None, "bash not found (checked before the override)")
    def test_bad_override_reports_itself(self):
        with tempfile.TemporaryDirectory() as tmp:
            missing = str(Path(tmp) / "no-such-claude")
            with unittest.mock.patch.dict(os.environ, {"ML_AI_SKILLS_CLAUDE_BIN": missing}):
                path, reason = _claude_path()
        self.assertIsNone(path)
        self.assertIn("ML_AI_SKILLS_CLAUDE_BIN", reason)
        self.assertIn("is not a file", reason)


class TestClaudeCliValidation(unittest.TestCase):
    """Asserts on returncode only, not --json output: different `claude` CLI
    builds have different flag surfaces (the ASBX Toolbox build used during
    authoring accepted `--json` on `plugin validate`; the public
    @anthropic-ai/claude-code build installed in CI printed nothing to
    stdout for it, breaking JSON parsing). `--strict` alone is exactly what
    the CI workflow's own validate steps run, so returncode==0 there is the
    portable, version-independent signal."""

    @classmethod
    def setUpClass(cls):
        require_claude()

    def _validate(self, path: Path) -> subprocess.CompletedProcess:
        return run_claude("plugin", "validate", str(path), "--strict")

    def test_marketplace_validates_clean_under_strict(self):
        result = self._validate(REPO_ROOT)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_every_plugin_validates_clean_under_strict(self):
        for plugin_dir in sorted((REPO_ROOT / "plugins").iterdir()):
            result = self._validate(plugin_dir)
            self.assertEqual(result.returncode, 0, f"{plugin_dir.name}: {result.stdout + result.stderr}")


class TestLocalMarketplaceInstall(unittest.TestCase):
    """End-to-end install against an isolated CLAUDE_CONFIG_DIR and HOME — never
    touches the real ~/.claude config. The fuller lifecycle (temporary clone, uninstall,
    marketplace remove, installed-copy bytes) is the opt-in
    tests/test_claude_cli_integration.py. A zero exit code shows the CLI accepted the
    packaging; it does not show that a skill is loaded, selected, or useful."""

    @classmethod
    def setUpClass(cls):
        require_claude()

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.env = isolated_cli_env(Path(self._tmp.name))

    def tearDown(self):
        self._tmp.cleanup()

    def _run(self, *args: str) -> subprocess.CompletedProcess:
        return run_claude("plugin", *args, env=self.env)

    def test_marketplace_add_install_update_reinstall_cycle(self):
        # No `-y`/`--yes`: that flag only matters for a marketplace-declared
        # *install command* (a script/headersHelper a plugin runs to fetch
        # itself) needing confirmation. Our plugins are plain skills/
        # folders with no such command, so no confirmation prompt applies —
        # and CLI builds disagree on whether `-y` is even a recognized flag.
        add = self._run("marketplace", "add", str(REPO_ROOT))
        self.assertEqual(add.returncode, 0, add.stdout + add.stderr)

        install = self._run("install", f"{gp.ALL_PLUGIN_NAME}@{gp.MARKETPLACE_NAME}")
        self.assertEqual(install.returncode, 0, install.stdout + install.stderr)

        domain_install = self._run("install", f"classical-ml@{gp.MARKETPLACE_NAME}")
        self.assertEqual(domain_install.returncode, 0, domain_install.stdout + domain_install.stderr)

        listing = self._run("list")
        self.assertIn("ml-ai-skills@ml-ai-skills", listing.stdout)
        self.assertIn("classical-ml@ml-ai-skills", listing.stdout)

        update = self._run("update", f"{gp.ALL_PLUGIN_NAME}@{gp.MARKETPLACE_NAME}")
        self.assertEqual(update.returncode, 0, update.stdout + update.stderr)

        reinstall = self._run("install", f"{gp.ALL_PLUGIN_NAME}@{gp.MARKETPLACE_NAME}")
        self.assertEqual(reinstall.returncode, 0, reinstall.stdout + reinstall.stderr)


if __name__ == "__main__":
    unittest.main()
