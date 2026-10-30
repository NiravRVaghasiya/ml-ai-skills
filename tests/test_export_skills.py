"""Tests for scripts/export_skills.py (upload-compatible export of the canonical skills).

Stdlib only. The PyYAML cross-check skips when PyYAML is not installed. Two opt-in tests
run the upstream validators themselves on the export:
  ML_AI_SKILLS_QUICK_VALIDATE=/path/to/quick_validate.py   (anthropics/skills; needs PyYAML
      in the interpreter named by ML_AI_SKILLS_UPSTREAM_PYTHON, default: this one)
  ML_AI_SKILLS_SKILLS_REF_PYTHON=/path/to/python            (an interpreter with skills-ref)
Passing them shows the files meet those validators, not that claude.ai or the Skills API
accept an upload: no upload is made.
"""
from __future__ import annotations

import hashlib
import io
import json
import os
import stat
import subprocess
import sys
import tempfile
import unittest
import zipfile
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from testutil import REPO_ROOT, copy_skill_tree
sys.path.insert(0, str(REPO_ROOT / "scripts"))
sys.path.insert(0, str(REPO_ROOT / "schema"))
import allowed_values as av  # noqa: E402
import export_skills as ex  # noqa: E402
import skills_lib as sl  # noqa: E402

try:
    import yaml
except ImportError:  # optional cross-check only
    yaml = None

SUBPROCESS_TIMEOUT = 120


def tree_hashes(root: Path) -> dict[str, str]:
    return {p.relative_to(root).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in sorted(root.rglob("*")) if p.is_file()}


def write_skill(root: Path, slug: str, raw: str | bytes) -> Path:
    d = root / slug
    d.mkdir(parents=True, exist_ok=True)
    (d / "SKILL.md").write_bytes(raw if isinstance(raw, bytes) else raw.encode("utf-8"))
    return d


DEFAULT_DESCRIPTION = 'Use when testing. Trigger phrases: "a", "b", "c". NOT for anything else.'


def skill_text(slug: str, description: str = DEFAULT_DESCRIPTION) -> str:
    """A canonical-shaped SKILL.md (every required key, list keys in both block and flow
    form). The description is a `>` block scalar, so its parsed value ends in one newline."""
    return (
        "---\n"
        f"name: {slug}\n"
        "display_name: Fixture Skill\n"
        f"description: >\n  {description}\n"
        "type: workflow\ndomain: specialized\nlevel: intermediate\nlifecycle: stable\n"
        "risk_level: low\nevidence_level: established-practice\nlast_verified: '2026-01-01'\n"
        "capabilities: [fixture-tag]\nrequires:\nconflicts:\nrelated:\n  - other-skill\n"
        "inputs: n/a\noutputs: n/a\n"
        "---\n\n## Overview\nBody text.\n"
    )


DASH_DESCRIPTION = 'Use when testing --- a dash case. Trigger phrases: "a", "b", "c". NOT for ----- rules.'


def dash_skill_text(slug: str) -> str:
    """A skill with `---` runs in the description and in a value folded into `metadata`.
    skills-ref splits SKILL.md on the first two literal `---`, so the export must not write one."""
    return skill_text(slug, DASH_DESCRIPTION).replace("inputs: n/a\n", 'inputs: "csv --- or ---- tsv"\n', 1)


def quiet_main(argv: list[str]) -> tuple[int, str, str]:
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = ex.main(argv)
    return code, out.getvalue(), err.getvalue()


def frontmatter_of(skill_md: bytes) -> str:
    return skill_md.split(b"\n---\n", 1)[0].decode("utf-8") + "\n---\n"


class _TmpCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()


class TestRealSkillsExport(unittest.TestCase):
    """Every real skill (copied to a temp root first, so concurrent edits cannot race)."""

    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory()
        cls.root = copy_skill_tree(Path(cls._tmp.name) / "src")
        cls.before = tree_hashes(cls.root)
        cls.canonical = {s.slug: s for s in sl.load_all_skills(cls.root)}
        cls.results = {}
        for target in ex.TARGETS:
            out = Path(cls._tmp.name) / f"out-{target}"
            code, report = ex.export(cls.root, [], target, out, license_text="MIT", make_zip=True)
            cls.results[target] = (code, report, out)

    @classmethod
    def tearDownClass(cls):
        cls._tmp.cleanup()

    def test_every_target_exports_every_skill(self):
        for target, (code, report, out) in self.results.items():
            problems = {e["slug"]: e["problems"] for e in report["skills"] if e["problems"]}
            self.assertEqual((code, problems), (0, {}), target)
            self.assertEqual(sorted(p.name for p in out.iterdir() if p.is_dir()), sorted(self.canonical))

    def test_frontmatter_has_only_allowed_keys_and_string_metadata(self):
        for target, (_, _, out) in self.results.items():
            for slug in self.canonical:
                fm = ex.parse_exported_frontmatter(frontmatter_of((out / slug / "SKILL.md").read_bytes()))
                self.assertLessEqual(set(fm), set(ex.TARGETS[target].allowed_keys), f"{target}/{slug}")
                self.assertEqual(set(fm), {"name", "description", "license", "metadata"})
                self.assertTrue(all(isinstance(v, str) for v in fm["metadata"].values()))
                self.assertEqual(fm["name"], slug)

    def test_body_is_byte_identical(self):
        for target, (_, report, out) in self.results.items():
            for slug in self.canonical:
                _, body = ex.split_skill_md((self.root / slug / "SKILL.md").read_bytes())
                exported = (out / slug / "SKILL.md").read_bytes()
                self.assertTrue(exported.endswith(b"\n---\n" + body), f"{target}/{slug}")
                entry = next(e for e in report["skills"] if e["slug"] == slug)
                self.assertEqual(entry["body_sha256"], hashlib.sha256(body).hexdigest())

    def test_round_trip_restores_canonical_values(self):
        """Values round-trip; formatting does not (documented). An empty list key
        (`requires:` with no items, parsed as null) comes back as []."""
        for target, (_, _, out) in self.results.items():
            for slug, skill in self.canonical.items():
                fm = ex.parse_exported_frontmatter(frontmatter_of((out / slug / "SKILL.md").read_bytes()))
                restored = ex.canonical_values(fm)
                self.assertEqual(restored.pop("license"), "MIT")
                want = {k: ([] if v is None and k in av.LIST_KEYS else v) for k, v in skill.frontmatter.items()}
                self.assertEqual(restored, want, f"{target}/{slug}")

    def test_report_lists_every_moved_key_and_drops_none(self):
        for _, report, out in self.results.values():
            on_disk = json.loads((out / ex.REPORT_NAME).read_text(encoding="utf-8"))
            self.assertEqual(on_disk, report)
            for entry in report["skills"]:
                canonical_keys = set(self.canonical[entry["slug"]].frontmatter) - {"name", "description"}
                self.assertEqual({m["key"] for m in entry["moved_keys"]}, canonical_keys)
                self.assertEqual(entry["dropped_keys"], [])
                self.assertEqual(entry["added_keys"], [{"key": "license", "action": "added", "value": "MIT"}])

    def test_canonical_tree_is_unchanged(self):
        self.assertEqual(tree_hashes(self.root), self.before)

    def test_zip_layout_matches_folder_and_is_deterministic(self):
        _, _, out = self.results["skills-api"]
        out2 = Path(self._tmp.name) / "out-skills-api-again"
        code, _ = ex.export(self.root, [], "skills-api", out2, license_text="MIT", make_zip=True)
        self.assertEqual(code, 0)
        for slug in self.canonical:
            data = (out / f"{slug}.zip").read_bytes()
            self.assertEqual(data, (out2 / f"{slug}.zip").read_bytes(), slug)
            with zipfile.ZipFile(io.BytesIO(data)) as zf:
                self.assertEqual(zf.namelist(), [f"{slug}/SKILL.md"])
                self.assertEqual(zf.read(f"{slug}/SKILL.md"), (out / slug / "SKILL.md").read_bytes())

    @unittest.skipIf(yaml is None, "PyYAML not installed (optional cross-check)")
    def test_pyyaml_reads_the_same_frontmatter(self):
        _, _, out = self.results["claude-ai-upload"]
        for slug in self.canonical:
            text = frontmatter_of((out / slug / "SKILL.md").read_bytes())
            self.assertEqual(yaml.safe_load(text.strip("-\n")), ex.parse_exported_frontmatter(text), slug)

    def test_list_targets_and_dry_run(self):
        code, stdout, _ = quiet_main(["--list-targets"])
        self.assertEqual(code, 0)
        for name in ex.TARGETS:
            self.assertIn(name, stdout)
        code, stdout, _ = quiet_main(["--root", str(self.root), "--target", "agentskills-spec"])
        self.assertEqual(code, 0)
        self.assertIn("dry run, nothing written", stdout)


class TestUsageErrors(_TmpCase):
    def setUp(self):
        super().setUp()
        self.root = self.tmp / "src"
        write_skill(self.root, "alpha", skill_text("alpha"))

    def test_unknown_target_is_rejected(self):
        with self.assertRaises(ex.UsageError) as cm:
            ex.export(self.root, [], "claude-desktop", self.tmp / "out")
        for name in ex.TARGETS:
            self.assertIn(name, str(cm.exception))
        code, _, err = quiet_main(["--root", str(self.root), "--target", "bogus", "--out", str(self.tmp / "o")])
        self.assertEqual(code, 2)
        self.assertIn("unknown target 'bogus'", err)
        self.assertFalse((self.tmp / "o").exists())
        self.assertEqual(quiet_main(["--root", str(self.root)])[0], 2)

    def test_out_must_be_new_or_empty(self):
        out = self.tmp / "out"
        out.mkdir()
        (out / "keep.txt").write_text("x", encoding="utf-8")
        code, _, err = quiet_main(["--root", str(self.root), "--target", "skills-api", "--out", str(out)])
        self.assertEqual(code, 2)
        self.assertIn("not empty", err)
        self.assertEqual(sorted(p.name for p in out.iterdir()), ["keep.txt"])
        empty = self.tmp / "empty"
        empty.mkdir()
        self.assertEqual(quiet_main(["--root", str(self.root), "--target", "skills-api", "--out", str(empty)])[0], 0)

    def test_never_in_place(self):
        before = tree_hashes(self.root)
        for out in (self.root, self.root / "alpha", self.root / "alpha" / "nested"):
            with self.assertRaises(ex.UsageError, msg=str(out)):
                ex.export(self.root, [], "skills-api", out)
        self.assertEqual(tree_hashes(self.root), before)

    def test_slugs_must_be_slugs_not_paths(self):
        outside = self.tmp / "outside"
        write_skill(self.tmp, "outside", skill_text("outside"))
        for bad in ("../outside", str(outside), "alpha/../outside", "Alpha", "missing-skill"):
            with self.assertRaises(ex.UsageError, msg=bad):
                ex.export(self.root, [bad], "skills-api", None)

    def test_zip_needs_out(self):
        self.assertEqual(quiet_main(["--root", str(self.root), "--target", "skills-api", "--zip"])[0], 2)


class TestTargetRules(_TmpCase):
    """Each documented limit, on synthetic skills; a failing skill means nothing is written."""

    def verdicts(self, slug: str, raw: str | bytes) -> dict[str, list[str]]:
        root = self.tmp / f"src-{slug}"
        write_skill(root, slug, raw)
        out = {}
        for target in ex.TARGETS:
            dest = self.tmp / f"out-{slug}-{target}"
            code, report = ex.export(root, [slug], target, dest)
            problems = report["skills"][0]["problems"]
            self.assertEqual(code, 1 if problems else 0)
            self.assertEqual(dest.exists(), not problems, f"{target}: output must exist only on success")
            out[target] = problems
        return out

    def assertOnlyFails(self, verdicts: dict, failing: set[str], needle: str):
        for target, problems in verdicts.items():
            if target in failing:
                self.assertTrue(any(needle in p for p in problems), f"{target}: {problems}")
            else:
                self.assertEqual(problems, [], target)

    def test_valid_skill_passes_every_target(self):
        self.assertOnlyFails(self.verdicts("alpha", skill_text("alpha")), set(), "")

    def test_description_over_1024_characters_fails_everywhere(self):
        desc = 'Use when x. Trigger phrases: "a", "b", "c". NOT for y. ' + "z" * 1024
        v = self.verdicts("long-desc", skill_text("long-desc", desc))
        self.assertOnlyFails(v, set(ex.TARGETS), "description is")

    def test_description_at_exactly_1024_characters_passes(self):
        head = 'Use when x. Trigger phrases: "a", "b", "c". NOT for y. '
        desc = head + "z" * (av.DESCRIPTION_MAX_LENGTH - len(head) - 1)  # + the block scalar's newline
        self.assertOnlyFails(self.verdicts("max-desc", skill_text("max-desc", desc)), set(), "")
        over = self.verdicts("over-desc", skill_text("over-desc", desc + "z"))
        self.assertOnlyFails(over, set(ex.TARGETS), "description is 1025")

    def test_name_over_64_characters_fails_everywhere(self):
        slug = "a" * 65
        self.assertOnlyFails(self.verdicts(slug, skill_text(slug)), set(ex.TARGETS), "name is 65")

    def test_reserved_word_fails_claude_targets_only(self):
        v = self.verdicts("claude-helper", skill_text("claude-helper"))
        self.assertOnlyFails(v, {"skills-api", "claude-ai-upload"}, "reserved word")

    def test_xml_tag_in_description_fails_claude_targets_only(self):
        desc = 'Use when <b>x</b>. Trigger phrases: "a", "b", "c". NOT for y.'
        v = self.verdicts("xml-desc", skill_text("xml-desc", desc))
        self.assertOnlyFails(v, {"skills-api", "claude-ai-upload"}, "XML tag")

    def test_bare_angle_bracket_fails_claude_ai_upload_only(self):
        desc = 'Use when n > 1000 rows. Trigger phrases: "a", "b", "c". NOT for y.'
        v = self.verdicts("angle-desc", skill_text("angle-desc", desc))
        self.assertOnlyFails(v, {"claude-ai-upload"}, "`<` or `>`")

    def test_name_must_equal_folder(self):
        raw = skill_text("alpha").replace("name: alpha", "name: beta", 1)
        self.assertOnlyFails(self.verdicts("alpha", raw), set(ex.TARGETS), "must equal its folder name")

    def test_name_with_trailing_newline_is_rejected(self):
        """A `|` block scalar gives "alpha\\n"; Python's `$` would let it match the slug regex."""
        raw = skill_text("alpha").replace("name: alpha\n", "name: |\n  alpha\n", 1)
        self.assertOnlyFails(self.verdicts("alpha", raw), set(ex.TARGETS), "must be lowercase letters")

    def test_malformed_frontmatter_is_refused_not_guessed(self):
        raw = skill_text("alpha").replace("level: intermediate", "level: yes", 1)
        self.assertOnlyFails(self.verdicts("alpha", raw), set(ex.TARGETS), "frontmatter line")

    def test_byte_order_mark_is_refused(self):
        raw = b"\xef\xbb\xbf" + skill_text("alpha").encode("utf-8")
        self.assertOnlyFails(self.verdicts("alpha", raw), set(ex.TARGETS), "byte-order mark")

    def test_triple_hyphen_in_values_survives_skills_ref_style_split(self):
        """skills-ref reads the frontmatter as `content.split("---", 2)[1]`; a literal `---`
        in a value would end it early (found by review: exit 0, then skills-ref rejected it)."""
        self.assertOnlyFails(self.verdicts("dashes", dash_skill_text("dashes")), set(), "")
        source = sl.parse_frontmatter_with_diagnostics(
            ex.split_skill_md(dash_skill_text("dashes").encode("utf-8"))[0]).data
        for target in ex.TARGETS:
            content = (self.tmp / f"out-dashes-{target}" / "dashes" / "SKILL.md").read_text(encoding="utf-8")
            head, fm_text, body = content.split("---", 2)  # exactly what skills-ref does
            self.assertEqual(head, "")
            self.assertEqual(body, "\n\n## Overview\nBody text.\n", target)
            values = ex.canonical_values(ex.parse_exported_frontmatter("---" + fm_text + "---\n"))
            self.assertEqual(values["description"], source["description"], target)
            self.assertEqual(values["inputs"], "csv --- or ---- tsv", target)
            if yaml is not None:
                self.assertEqual(yaml.safe_load(fm_text)["description"], source["description"], target)

    def test_literal_triple_hyphen_before_the_fence_is_rejected(self):
        """The guard behind the emitter: hand-written bytes with a literal `---` in a value."""
        fm = {"name": "alpha", "description": "a --- b"}
        bad = b'---\nname: "alpha"\ndescription: "a --- b"\n---\nbody\n'
        for target in ex.TARGETS.values():
            problems = ex.validate_export(fm, "alpha", {"SKILL.md": bad}, bad, target)
            self.assertEqual(len(problems), 1, problems)
            self.assertIn("`---` before its closing fence", problems[0])
        good = ex.render_frontmatter(fm).encode("utf-8") + b"body\n"
        self.assertNotIn(b"---", good[3:good.index(b"\n---\n")])
        for target in ex.TARGETS.values():
            self.assertEqual(ex.validate_export(fm, "alpha", {"SKILL.md": good}, good, target), [])

    def test_upload_size_limit(self):
        root = self.tmp / "src"
        d = write_skill(root, "alpha", skill_text("alpha"))
        (d / "big.bin").write_bytes(b"\0" * ex.UPLOAD_MAX_BYTES)
        _, report = ex.export(root, ["alpha"], "skills-api", None)
        self.assertTrue(any("uncompressed" in p for p in report["skills"][0]["problems"]))
        _, report = ex.export(root, ["alpha"], "agentskills-spec", None)
        self.assertEqual(report["skills"][0]["problems"], [])


class TestFilesAndLinks(_TmpCase):
    def test_extra_files_copied_and_build_artifacts_excluded_and_reported(self):
        root = self.tmp / "src"
        d = write_skill(root, "alpha", skill_text("alpha"))
        (d / "references").mkdir()
        (d / "references" / "guide.md").write_bytes(b"line\r\nwith crlf\r\n")
        (d / "__pycache__").mkdir()
        (d / "__pycache__" / "x.cpython-313.pyc").write_bytes(b"\0")
        (d / "evals").mkdir()
        (d / "evals" / "case.yaml").write_text("x: 1\n", encoding="utf-8")
        (d / "notes.pyc").write_bytes(b"\0")
        (d / ".DS_Store").write_bytes(b"\0")
        out = self.tmp / "out"
        code, report = ex.export(root, [], "claude-ai-upload", out, make_zip=True)
        self.assertEqual(code, 0, report)
        self.assertEqual((out / "alpha" / "references" / "guide.md").read_bytes(), b"line\r\nwith crlf\r\n")
        entry = report["skills"][0]
        self.assertEqual(entry["files"], ["SKILL.md", "references/guide.md"])
        self.assertEqual(sorted(entry["excluded_files"]), [".DS_Store", "__pycache__/", "evals/", "notes.pyc"])
        self.assertEqual(sorted(p.relative_to(out / "alpha").as_posix() for p in (out / "alpha").rglob("*")
                                if p.is_file()), ["SKILL.md", "references/guide.md"])
        with zipfile.ZipFile(out / "alpha.zip") as zf:
            self.assertEqual(zf.namelist(), ["alpha/SKILL.md", "alpha/references/guide.md"])

    def test_crlf_body_kept_byte_for_byte_and_frontmatter_written_lf(self):
        root = self.tmp / "src"
        raw = skill_text("alpha").replace("\n", "\r\n").encode("utf-8")
        write_skill(root, "alpha", raw)
        out = self.tmp / "out"
        code, report = ex.export(root, [], "claude-ai-upload", out)
        self.assertEqual(code, 0, report)
        _, body = ex.split_skill_md(raw)
        self.assertIn(b"\r\n", body)
        exported = (out / "alpha" / "SKILL.md").read_bytes()
        self.assertTrue(exported.endswith(b"\n---\n" + body))
        self.assertNotIn(b"\r", exported[: len(exported) - len(body)])

    def _symlink(self, link: Path, target: Path, is_dir: bool) -> None:
        try:
            os.symlink(target, link, target_is_directory=is_dir)
        except (OSError, NotImplementedError) as e:
            self.skipTest(f"cannot create symlinks here ({e}); Windows needs Developer Mode or admin")

    def test_symlinked_file_inside_skill_is_refused(self):
        root = self.tmp / "src"
        d = write_skill(root, "alpha", skill_text("alpha"))
        secret = self.tmp / "secret.txt"
        secret.write_text("do not ship", encoding="utf-8")
        self._symlink(d / "leak.txt", secret, False)
        out = self.tmp / "out"
        code, report = ex.export(root, [], "skills-api", out)
        self.assertEqual(code, 1)
        self.assertTrue(any("leak.txt" in p and "symlink" in p for p in report["skills"][0]["problems"]))
        self.assertFalse(out.exists())

    def test_symlinked_skill_folder_is_refused(self):
        root = self.tmp / "src"
        real = write_skill(self.tmp / "elsewhere", "alpha", skill_text("alpha"))
        root.mkdir()
        self._symlink(root / "alpha", real, True)
        code, report = ex.export(root, ["alpha"], "skills-api", self.tmp / "out")
        self.assertEqual(code, 1)
        self.assertIn("symlink/junction", report["skills"][0]["problems"][0])

    @unittest.skipUnless(sys.platform == "win32", "directory junctions are Windows-only")
    def test_junction_inside_skill_is_refused(self):
        root = self.tmp / "src"
        d = write_skill(root, "alpha", skill_text("alpha"))
        target = self.tmp / "outside-dir"
        target.mkdir()
        (target / "x.md").write_text("outside", encoding="utf-8")
        made = subprocess.run(["cmd", "/c", "mklink", "/J", str(d / "linked"), str(target)],
                              capture_output=True, text=True, timeout=SUBPROCESS_TIMEOUT)
        if made.returncode != 0:
            self.skipTest(f"mklink /J failed: {made.stdout}{made.stderr}")
        code, report = ex.export(root, [], "skills-api", self.tmp / "out")
        self.assertEqual(code, 1)
        self.assertTrue(any("linked" in p for p in report["skills"][0]["problems"]), report)

    def test_reparse_point_classification(self):
        """Link-like reparse points are refused; data-holding ones (OneDrive placeholders,
        dedup) are not mistaken for links. Simulated stat results: those files cannot be
        created here."""
        def fake(tag=None, attrs=0x400 | 0x20):
            st = SimpleNamespace(st_mode=stat.S_IFREG | 0o644, st_file_attributes=attrs)
            if tag is not None:
                st.st_reparse_tag = tag
            return st
        cases = {"symlink": (fake(0xA000000C), True), "junction": (fake(0xA0000003), True),
                 "app execution alias": (fake(0x8000001B), True),
                 "WSL symlink (name surrogate)": (fake(0xA000001D), True),
                 "tag unreadable": (fake(None), True),
                 "OneDrive placeholder": (fake(0x9000001A), False), "dedup": (fake(0x80000013), False),
                 "plain file": (fake(None, attrs=0x20), False)}
        for label, (st, expected) in cases.items():
            with self.subTest(label), mock.patch.object(ex.os, "lstat", return_value=st):
                self.assertIs(ex.is_link(Path("x")), expected)


class TestYamlEmission(unittest.TestCase):
    NASTY = ["plain", 'with "quotes" and \\backslash', "line\nbreak\ttab\rcr",
             "nel\x85 ls\u2028 ps\u2029 ", "nul\x00 bel\x07 del\x7f bom\ufeff c1\x9b",
             "emoji \U0001F600 and CJK \u6f22\u5b57", "", " leading/trailing ",
             "- looks: like yaml # not a comment", "'single'", "{flow: [x]}", "&anchor *alias !tag",
             "a --- b", "---", "--", "-------", "x---y----z", "\\---", '"---"']

    def test_quote_round_trips_through_json(self):
        for s in self.NASTY:
            q = ex.yaml_quote(s)
            self.assertNotIn("\n", q)
            self.assertNotIn("---", q, "skills-ref splits SKILL.md on literal `---`")
            self.assertEqual(json.loads(q), s, repr(s))

    @unittest.skipIf(yaml is None, "PyYAML not installed (optional cross-check)")
    def test_quote_round_trips_through_pyyaml(self):
        for s in self.NASTY:
            self.assertEqual(yaml.safe_load("k: " + ex.yaml_quote(s))["k"], s, repr(s))

    def test_reader_rejects_anything_outside_the_emitted_grammar(self):
        for bad in ("---\nname: plain\n---\n", "---\nname: \"a\"\nname: \"b\"\n---\n",
                    "---\n  orphan: \"x\"\n---\n", "---\nname: \"a\"\n", "name: \"a\"\n---\n",
                    "---\nmetadata:\n  k: \"a\"\n  k: \"b\"\n---\n"):
            with self.assertRaises(ValueError, msg=bad):
                ex.parse_exported_frontmatter(bad)

    def test_render_then_parse_is_identity(self):
        fm = {"name": "x", "description": self.NASTY[3], "license": "MIT",
              "metadata": {"a_b": self.NASTY[4], "c": ""}}
        self.assertEqual(ex.parse_exported_frontmatter(ex.render_frontmatter(fm)), fm)


@unittest.skipUnless(os.environ.get("ML_AI_SKILLS_QUICK_VALIDATE"),
                     "opt-in: set ML_AI_SKILLS_QUICK_VALIDATE to a downloaded anthropics/skills quick_validate.py")
class TestUpstreamQuickValidate(_TmpCase):
    def test_every_exported_skill_passes_quick_validate(self):
        script = os.environ["ML_AI_SKILLS_QUICK_VALIDATE"]
        python = os.environ.get("ML_AI_SKILLS_UPSTREAM_PYTHON", sys.executable)
        root = copy_skill_tree(self.tmp / "src")
        write_skill(root, "dash-fixture", dash_skill_text("dash-fixture"))
        out = self.tmp / "out"
        self.assertEqual(ex.export(root, [], "claude-ai-upload", out)[0], 0)
        self.assertTrue((out / "dash-fixture" / "SKILL.md").is_file())
        for d in sorted(p for p in out.iterdir() if p.is_dir()):
            r = subprocess.run([python, "-I", script, str(d)], capture_output=True, text=True,
                               timeout=SUBPROCESS_TIMEOUT, env=dict(os.environ, PYTHONUTF8="1"))
            self.assertEqual(r.returncode, 0, f"{d.name}: {r.stdout}{r.stderr}")
        canonical = root / "data-preprocessing"
        r = subprocess.run([python, "-I", script, str(canonical)], capture_output=True, text=True,
                           timeout=SUBPROCESS_TIMEOUT, env=dict(os.environ, PYTHONUTF8="1"))
        self.assertNotEqual(r.returncode, 0, "the canonical folder was expected to be rejected")
        self.assertIn("Unexpected key(s)", r.stdout)


@unittest.skipUnless(os.environ.get("ML_AI_SKILLS_SKILLS_REF_PYTHON"),
                     "opt-in: set ML_AI_SKILLS_SKILLS_REF_PYTHON to an interpreter with skills-ref installed")
class TestUpstreamSkillsRef(_TmpCase):
    def test_every_exported_skill_passes_skills_ref(self):
        python = os.environ["ML_AI_SKILLS_SKILLS_REF_PYTHON"]
        root = copy_skill_tree(self.tmp / "src")
        # `---` inside values: skills-ref's parser splits on it (the export escapes it).
        write_skill(root, "dash-fixture", dash_skill_text("dash-fixture"))
        out = self.tmp / "out"
        self.assertEqual(ex.export(root, [], "agentskills-spec", out)[0], 0)
        self.assertTrue((out / "dash-fixture" / "SKILL.md").is_file())
        code = ("import json, sys, pathlib\nfrom skills_ref.validator import validate\n"
                "print(json.dumps({d.name: validate(d) for d in sorted(pathlib.Path(sys.argv[1]).iterdir())"
                " if d.is_dir()}))")
        for folder, expect_clean in ((out, True), (root, False)):
            r = subprocess.run([python, "-I", "-c", code, str(folder)], capture_output=True, text=True,
                               timeout=SUBPROCESS_TIMEOUT, env=dict(os.environ, PYTHONUTF8="1"))
            self.assertEqual(r.returncode, 0, r.stderr)
            errors = {k: v for k, v in json.loads(r.stdout).items() if v}
            if expect_clean:
                self.assertEqual(errors, {})
            else:
                self.assertTrue(errors, "the canonical folders were expected to be rejected")
        read = ("import json, sys, pathlib\nfrom skills_ref.parser import read_properties\n"
                "p = read_properties(pathlib.Path(sys.argv[1]))\n"
                "print(json.dumps({'description': p.description, 'metadata': p.metadata}))")
        r = subprocess.run([python, "-I", "-c", read, str(out / "dash-fixture")], capture_output=True, text=True,
                           timeout=SUBPROCESS_TIMEOUT, env=dict(os.environ, PYTHONUTF8="1"))
        self.assertEqual(r.returncode, 0, r.stderr)
        props = json.loads(r.stdout)
        self.assertEqual(props["description"].strip(), DASH_DESCRIPTION)
        self.assertEqual(props["metadata"]["inputs"], "csv --- or ---- tsv")


if __name__ == "__main__":
    unittest.main()
