"""Tests for the native Claude Code plugin/marketplace packaging
(scripts/generate_plugins.py) — covers marketplace JSON validity, plugin
manifest validity, every marketplace entry resolving, every plugin containing
valid/discoverable skills, no duplicate skill content (byte-identical to the
canonical source), and reproducibility (generated output has zero drift).

The `claude`-CLI-backed tests additionally run the real `claude plugin
validate` / `plugin marketplace add` / `plugin install` / `plugin update`
commands against an isolated CLAUDE_CONFIG_DIR (never the developer's real
~/.claude config) — skipped if the `claude` CLI isn't on PATH.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from testutil import REPO_ROOT, make_skill
sys.path.insert(0, str(REPO_ROOT / "scripts"))
sys.path.insert(0, str(REPO_ROOT / "schema"))
import generate_plugins as gp  # noqa: E402
import skills_lib as sl  # noqa: E402
import allowed_values as av  # noqa: E402

BASH = shutil.which("bash")


def _claude_available() -> bool:
    """Probe through `bash -lc` rather than shutil.which("claude") directly:
    on Windows, `claude` commonly resolves to a shim (e.g. an ASBX Toolbox
    script) that only runs correctly when exec'd by a POSIX shell, the same
    reason test_install.py shells out to bash rather than invoking install.sh
    directly."""
    if not BASH:
        return False
    try:
        return subprocess.run(
            [BASH, "-lc", "command -v claude"], capture_output=True, text=True, timeout=10,
        ).returncode == 0
    except OSError:
        return False


CLAUDE_AVAILABLE = _claude_available()
ALL_SKILLS = sl.load_all_skills()
ALL_SLUGS = {s.slug for s in ALL_SKILLS}


def run_claude(*args: str, env: dict | None = None) -> subprocess.CompletedProcess:
    """Shell out to `claude` via bash -lc (see _claude_available). Windows
    backslash paths are normalized to forward slashes first — MSYS/git-bash's
    argv path-mangling otherwise corrupts a `C:\\...` string embedded in a
    -lc command line; `claude` (Node) accepts forward slashes on Windows."""
    norm = [str(a).replace("\\", "/") for a in args]
    quoted = " ".join(f'"{a}"' if (" " in a or a == "") else a for a in norm)
    return subprocess.run(
        [BASH, "-lc", f"claude {quoted}"], capture_output=True, env=env,
        encoding="utf-8", errors="replace",  # CLI output can include unicode glyphs (❯, ✔) that
    )                                        # aren't representable in the Windows console codepage


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


@unittest.skipUnless(CLAUDE_AVAILABLE, "claude CLI not found on PATH (via bash)")
class TestClaudeCliValidation(unittest.TestCase):
    def _validate(self, path: Path) -> dict:
        result = run_claude("plugin", "validate", str(path), "--json", "--strict")
        return json.loads(result.stdout)

    def test_marketplace_validates_clean_under_strict(self):
        report = self._validate(REPO_ROOT)
        self.assertTrue(report["success"], report["manifest"])
        self.assertEqual(report["manifest"]["errors"], [])
        self.assertEqual(report["manifest"]["warnings"], [])

    def test_every_plugin_validates_clean_under_strict(self):
        for plugin_dir in sorted((REPO_ROOT / "plugins").iterdir()):
            report = self._validate(plugin_dir)
            self.assertTrue(report["success"], f"{plugin_dir.name}: {report['manifest']}")


@unittest.skipUnless(CLAUDE_AVAILABLE, "claude CLI not found on PATH (via bash)")
class TestLocalMarketplaceInstall(unittest.TestCase):
    """End-to-end install against an isolated CLAUDE_CONFIG_DIR — never
    touches the real ~/.claude config."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.env = dict(os.environ, CLAUDE_CONFIG_DIR=self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def _run(self, *args: str) -> subprocess.CompletedProcess:
        return run_claude("plugin", *args, env=self.env)

    def test_marketplace_add_install_update_reinstall_cycle(self):
        add = self._run("marketplace", "add", str(REPO_ROOT))
        self.assertEqual(add.returncode, 0, add.stderr)

        install = self._run("install", f"{gp.ALL_PLUGIN_NAME}@{gp.MARKETPLACE_NAME}", "-y")
        self.assertEqual(install.returncode, 0, install.stderr)

        domain_install = self._run("install", f"classical-ml@{gp.MARKETPLACE_NAME}", "-y")
        self.assertEqual(domain_install.returncode, 0, domain_install.stderr)

        listing = self._run("list")
        self.assertIn("ml-ai-skills@ml-ai-skills", listing.stdout)
        self.assertIn("classical-ml@ml-ai-skills", listing.stdout)

        update = self._run("update", f"{gp.ALL_PLUGIN_NAME}@{gp.MARKETPLACE_NAME}")
        self.assertEqual(update.returncode, 0, update.stderr)

        reinstall = self._run("install", f"{gp.ALL_PLUGIN_NAME}@{gp.MARKETPLACE_NAME}", "-y")
        self.assertEqual(reinstall.returncode, 0, reinstall.stderr)


if __name__ == "__main__":
    unittest.main()
