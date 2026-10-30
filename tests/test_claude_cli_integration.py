"""Opt-in end-to-end check of the plugin marketplace with a real `claude` CLI.

Skipped unless ML_AI_SKILLS_CLAUDE_CLI=1. Set ML_AI_SKILLS_CLAUDE_BIN to test a specific
binary (for example an unpacked public @anthropic-ai/claude-code build) instead of the
`claude` on PATH.

What it does, all inside one temporary directory:
  * `git clone` of this repository's committed HEAD (uncommitted edits are not tested;
    that is what a user adding the GitHub marketplace gets);
  * runs the clone's own `scripts/generate_plugins.py --check`;
  * runs `claude` with CLAUDE_CONFIG_DIR, HOME, USERPROFILE, APPDATA and the XDG dirs
    redirected into the temp dir and model-provider variables removed, for:
    `--version`, `plugin validate --strict` (marketplace and every plugin),
    `plugin marketplace add/list/update/remove`, `plugin install`, `plugin list`
    (text and `--json`), `plugin details` (the skill inventory, where the CLI build has
    it), `plugin update`, `plugin uninstall`, and two commands that must fail;
  * compares every installed SKILL.md copy the CLI reports with the clone's canonical
    bytes, and checks that the developer's real config (plugin registry, settings.json,
    .claude.json) never mentions the temp clone, after a positive control on the
    isolated config shows the check reads the files the CLI writes.

What it does not show: no command here sends a prompt to a model, so nothing shows that
Claude Code loads a skill into a session, selects the right one, or answers better with
it. Exit codes and listings only show that the CLI accepted, installed and inventoried
the packaging. Every command has a timeout. A local-folder marketplace needs no network,
but whether a CLI build makes background requests (telemetry, the ASBX wrapper's MCP
registration) was not checked.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import test_generate_plugins as tgp
from testutil import REPO_ROOT

ENABLED = os.environ.get("ML_AI_SKILLS_CLAUDE_CLI") == "1"
GIT_TIMEOUT = 120
SKIP_REASON = ("opt-in: set ML_AI_SKILLS_CLAUDE_CLI=1 to run the claude CLI marketplace lifecycle "
               "(needs git and a claude CLI; no model calls)")


def registry_files(config_dir: str | None, home: Path) -> list[Path]:
    """Every config file where the CLI was seen to record a marketplace or plugin. A
    local-folder `marketplace add` is written to settings.json (`extraKnownMarketplaces`,
    "declared in user settings"); the plugin registry lives under plugins/; global state is
    in .claude.json (inside CLAUDE_CONFIG_DIR when set, else ~/.claude.json)."""
    base = Path(config_dir) if config_dir else home / ".claude"
    files = [base / "plugins" / "known_marketplaces.json", base / "plugins" / "installed_plugins.json",
             base / "settings.json", base / "settings.local.json", base / ".claude.json"]
    if not config_dir:
        files.append(home / ".claude.json")
    return files


def real_plugin_registry_files() -> list[Path]:
    return registry_files(os.environ.get("CLAUDE_CONFIG_DIR"), Path.home())


def mentions(paths: list[Path], needles: set[str]) -> list[Path]:
    hits = []
    for path in paths:
        text = path.read_text(encoding="utf-8", errors="replace") if path.is_file() else ""
        if any(needle in text for needle in needles):
            hits.append(path)
    return hits


@unittest.skipUnless(ENABLED, SKIP_REASON)
class TestClaudeCliMarketplaceLifecycle(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        if not tgp.CLAUDE_AVAILABLE:
            if os.environ.get("ML_AI_SKILLS_CLAUDE_BIN"):  # a misconfiguration, not an absent CLI
                raise AssertionError(f"ML_AI_SKILLS_CLAUDE_CLI=1: {tgp.CLAUDE_MISSING_REASON}")
            raise unittest.SkipTest(f"ML_AI_SKILLS_CLAUDE_CLI=1 but {tgp.CLAUDE_MISSING_REASON}")
        cls._tmp = tempfile.TemporaryDirectory(prefix="mlai-cli-")
        cls.tmp = Path(cls._tmp.name)
        cls.clone = cls.tmp / "repo"
        subprocess.run(["git", "clone", "--quiet", "--no-hardlinks", str(REPO_ROOT), str(cls.clone)],
                       check=True, capture_output=True, timeout=GIT_TIMEOUT)
        cls.env = tgp.isolated_cli_env(cls.tmp / "isolated")
        cls.skills = {}
        for d in sorted(cls.clone.iterdir()):
            if (d / "SKILL.md").is_file() and d.name not in ("_TEMPLATE",) and not d.name.startswith("."):
                cls.skills[d.name] = (d / "SKILL.md").read_bytes()
        cls.plan = {}
        for p in sorted((cls.clone / "plugins").iterdir()):
            cls.plan[p.name] = sorted(s.name for s in (p / "skills").iterdir()) if (p / "skills").exists() else []

    @classmethod
    def tearDownClass(cls):
        cls._tmp.cleanup()

    def claude(self, *args: str, expect: int | None = 0) -> subprocess.CompletedProcess:
        result = tgp.run_claude(*args, env=self.env)
        if expect is not None:
            self.assertEqual(result.returncode, expect, f"claude {' '.join(args)}\n{result.stdout}{result.stderr}")
        return result

    def test_lifecycle(self):
        mp = tgp.gp.MARKETPLACE_NAME
        all_id = f"{tgp.gp.ALL_PLUGIN_NAME}@{mp}"
        domain_id = f"classical-ml@{mp}"
        version = self.claude("--version")
        print(f"\n[claude-cli] {version.stdout.strip()} at {tgp.CLAUDE_PATH}", file=sys.stderr)

        gen = subprocess.run([sys.executable, str(self.clone / "scripts" / "generate_plugins.py"), "--check"],
                             capture_output=True, text=True, timeout=GIT_TIMEOUT, cwd=self.clone)
        self.assertEqual(gen.returncode, 0, "committed plugins/ drift from the committed skills:\n" + gen.stdout)
        self.assertEqual(sorted(self.plan[tgp.gp.ALL_PLUGIN_NAME]), sorted(self.skills))

        with self.subTest("validate"):
            self.claude("plugin", "validate", str(self.clone), "--strict")
            for name in self.plan:
                self.claude("plugin", "validate", str(self.clone / "plugins" / name), "--strict")

        needles = {str(self.clone), json.dumps(str(self.clone))[1:-1], self.clone.as_posix()}
        self.claude("plugin", "marketplace", "add", str(self.clone))
        listing = self.claude("plugin", "marketplace", "list")
        self.assertIn(mp, listing.stdout)
        with self.subTest("positive control: the leak check reads the files the CLI writes"):
            isolated = registry_files(self.env["CLAUDE_CONFIG_DIR"], Path(self.env["HOME"]))
            self.assertTrue(mentions(isolated, needles),
                            f"the isolated config records the marketplace in none of {isolated}; "
                            "the leak check below would not see a leak")

        self.claude("plugin", "install", all_id)
        self.claude("plugin", "install", domain_id)
        listed = self.claude("plugin", "list")
        self.assertIn(all_id, listed.stdout)
        self.assertIn(domain_id, listed.stdout)

        with self.subTest("installed copies are byte-identical to the clone's canonical SKILL.md"):
            self.check_installed_copies({all_id, domain_id})

        with self.subTest("skill inventory reported by `plugin details`"):
            self.check_details(domain_id, "classical-ml")
            self.check_details(all_id, tgp.gp.ALL_PLUGIN_NAME)

        self.claude("plugin", "update", all_id)
        self.claude("plugin", "marketplace", "update", mp)

        self.claude("plugin", "uninstall", domain_id)
        after = self.claude("plugin", "list")
        self.assertNotIn(domain_id, after.stdout)
        self.assertIn(all_id, after.stdout)

        with self.subTest("commands that must fail do fail"):
            again = self.claude("plugin", "uninstall", domain_id, expect=None)
            self.assertNotEqual(again.returncode, 0, again.stdout + again.stderr)
            missing = self.claude("plugin", "install", f"no-such-plugin@{mp}", expect=None)
            self.assertNotEqual(missing.returncode, 0, missing.stdout + missing.stderr)

        self.claude("plugin", "marketplace", "remove", mp)
        final = self.claude("plugin", "list")
        self.assertNotIn(f"@{mp}", final.stdout)
        self.assertNotIn(mp, self.claude("plugin", "marketplace", "list").stdout)

        with self.subTest("the developer's real plugin registry never saw the temporary marketplace"):
            # A content comparison would race with a Claude Code session the developer has
            # open; the temp clone's path can only get there through a leak.
            self.assertEqual(mentions(real_plugin_registry_files(), needles), [],
                             "the developer's real config mentions the test's temporary clone")

    def check_installed_copies(self, ids: set[str]) -> None:
        raw = self.claude("plugin", "list", "--json", expect=None)
        try:
            entries = json.loads(raw.stdout)
        except ValueError:
            self.skipTest(f"this CLI build prints no JSON for `plugin list --json` (rc={raw.returncode})")
        by_id = {e.get("id"): e for e in entries if isinstance(e, dict)}
        self.assertTrue(ids <= set(by_id), f"`plugin list --json` lacks {ids - set(by_id)}")
        for plugin_id in ids:
            name = plugin_id.split("@", 1)[0]
            roots = [Path(by_id[plugin_id][k]) for k in ("installPath", "readFromFolder") if by_id[plugin_id].get(k)]
            self.assertTrue(roots, f"{plugin_id}: no installPath/readFromFolder in `plugin list --json`")
            for root in roots:
                shipped = sorted(p.name for p in (root / "skills").iterdir())
                self.assertEqual(shipped, self.plan[name], f"{root}")
                for slug in shipped:
                    self.assertEqual((root / "skills" / slug / "SKILL.md").read_bytes(), self.skills[slug],
                                     f"{root}/skills/{slug}/SKILL.md differs from the canonical file")

    def check_details(self, plugin_id: str, name: str) -> None:
        result = self.claude("plugin", "details", plugin_id, expect=None)
        if result.returncode != 0 and re.search(r"unknown command|did you mean", result.stdout + result.stderr, re.I):
            self.skipTest("this CLI build has no `plugin details`")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        m = re.search(r"Skills \((\d+)\)", result.stdout)
        self.assertIsNotNone(m, f"no skill inventory in `plugin details` output:\n{result.stdout}")
        self.assertEqual(int(m.group(1)), len(self.plan[name]), result.stdout)
        for slug in self.plan[name]:
            self.assertRegex(result.stdout, rf"(?m)^\s+{re.escape(slug)}\s", f"{slug} missing from details")


if __name__ == "__main__":
    unittest.main()
