"""Per-file integrity of managed skill folders (manifest format 3).

install.sh records the sha256 of every file in a skill folder, not only
SKILL.md, so an edit to any file (changed, deleted, renamed or added) keeps
the skill on update, prune and uninstall unless --force. Also covers the
upgrade from format-1/2 manifests, corrupt or hostile manifest file lines,
unusual file names, pre-existing user content and rollback. See
docs/INSTALLER.md, guarantees 2 and 5, and "Manifest format".

Uses the small fake checkout from test_install_security.Base. Requires bash.
"""
from __future__ import annotations

import os
import shutil
import unittest

from test_install_security import LOCK_NAME, MANIFEST_NAME, SLUGS, Base, sha256, skill_text, snapshot

EXTRA = {"ref/guide.md": "guide v1\n", ".hidden": "dot\n", "scripts/run me.py": "print('x')\n"}


class MultiFileBase(Base):
    def setUp(self):
        super().setUp()
        self.add_extra_files()

    def add_extra_files(self, version: str = "1", slugs=SLUGS) -> None:
        for slug in slugs:
            for rel, body in EXTRA.items():
                p = self.src / slug / rel
                p.parent.mkdir(parents=True, exist_ok=True)
                p.write_text(body.replace("v1", f"v{version}"), encoding="utf-8", newline="\n")

    def bump_all(self, version: str) -> None:
        self.bump_source(version)
        self.add_extra_files(version)

    def keep_line(self, r, slug: str) -> str:
        lines = [ln for ln in r.stderr.splitlines() if f"keep    {slug} " in ln]
        self.assertEqual(len(lines), 1, r.stderr)
        return lines[0]

    def to_format_2(self) -> None:
        """Rewrite the manifest as the previous install.sh would have written it."""
        out = []
        for line in self.manifest.read_text(encoding="utf-8").splitlines():
            if line == "# format: 3":
                out.append("# format: 2")
            elif line.count("\t") < 2:
                out.append(line)
        self.manifest.write_text("\n".join(out) + "\n", encoding="utf-8", newline="\n")


class TestPerFileRecord(MultiFileBase):
    def test_manifest_records_every_file_including_hidden_ones(self):
        self.ok(self.install())
        text = self.manifest.read_text(encoding="utf-8")
        self.assertIn("# format: 3\n", text)
        files = self.manifest_files()
        self.assertEqual(set(files), set(SLUGS))
        for slug in SLUGS:
            self.assertEqual(set(files[slug]), {"SKILL.md", *EXTRA}, slug)
        self.assert_manifest_matches_disk()
        # Grouped by slug, skill line first, then files by path.
        body = [ln for ln in text.splitlines() if not ln.startswith("#")]
        self.assertEqual(body[:5], [f"alpha\t{sha256(self.target / 'alpha' / 'SKILL.md')}",
                                    f"alpha\t{sha256(self.target / 'alpha' / '.hidden')}\t.hidden",
                                    f"alpha\t{sha256(self.target / 'alpha' / 'SKILL.md')}\tSKILL.md",
                                    f"alpha\t{sha256(self.target / 'alpha' / 'ref' / 'guide.md')}\tref/guide.md",
                                    f"alpha\t{sha256(self.target / 'alpha' / 'scripts' / 'run me.py')}"
                                    "\tscripts/run me.py"])

    def test_any_file_edit_keeps_the_skill_on_update(self):
        slugs = ("alpha", "beta", "gamma", "delta", "epsilon")
        self.write_source({s: skill_text(s) for s in slugs})
        self.add_extra_files(slugs=slugs)
        self.ok(self.install())
        t = self.target
        (t / "alpha" / "ref" / "guide.md").write_text("my notes", encoding="utf-8")   # modified
        (t / "beta" / ".hidden").unlink()                                               # deleted
        (t / "gamma" / "ref" / "guide.md").rename(t / "gamma" / "ref" / "guide2.md")   # renamed
        (t / "delta" / "ref" / "mine.md").write_text("mine", encoding="utf-8")          # added
        self.write_source({s: skill_text(s, "2") for s in slugs})
        self.add_extra_files("2", slugs=slugs)
        r = self.ok(self.install())
        self.assertIn("updated=1 skipped=4", r.stdout)
        self.assertIn("(changed: ref/guide.md)", self.keep_line(r, "alpha"))
        self.assertIn("(removed: .hidden)", self.keep_line(r, "beta"))
        self.assertIn("(added: ref/guide2.md; removed: ref/guide.md)", self.keep_line(r, "gamma"))
        self.assertIn("(added: ref/mine.md)", self.keep_line(r, "delta"))
        for slug in ("alpha", "beta", "gamma", "delta"):
            self.assertIn(f"--force --skill {slug}", self.keep_line(r, slug))
            self.assertEqual(self.content(slug), skill_text(slug), "kept, not overwritten")
        self.assertEqual((t / "alpha" / "ref" / "guide.md").read_text(encoding="utf-8"), "my notes")
        self.assertEqual((t / "delta" / "ref" / "mine.md").read_text(encoding="utf-8"), "mine")
        self.assertEqual(self.content("epsilon"), skill_text("epsilon", "2"))
        # The kept skills keep their install-time record, so the edit stays detectable.
        r = self.ok(self.install())
        self.assertIn("skipped=4", r.stdout)
        self.ok(self.install("--force", "--skill", "delta"))
        self.assertFalse((t / "delta" / "ref" / "mine.md").exists())
        self.assertEqual(self.content("delta"), skill_text("delta", "2"))
        self.assertEqual((t / "alpha" / "ref" / "guide.md").read_text(encoding="utf-8"), "my notes")

    def test_any_file_edit_keeps_the_skill_on_uninstall_and_prune(self):
        self.ok(self.install())
        (self.target / "alpha" / "scripts" / "run me.py").write_text("edited", encoding="utf-8")
        (self.target / "gamma" / "extra dir").mkdir()
        (self.target / "gamma" / "extra dir" / "x.md").write_text("mine", encoding="utf-8")
        r = self.ok(self.uninstall())
        self.assertIn("(changed: scripts/run me.py)", self.keep_line(r, "alpha"))
        self.assertIn("(added: extra dir/x.md)", self.keep_line(r, "gamma"))
        self.assertFalse((self.target / "beta").exists())
        self.assertEqual(set(self.manifest_entries()), {"alpha", "gamma"})
        self.assertEqual(set(self.manifest_files()["alpha"]), {"SKILL.md", *EXTRA},
                         "the install-time record is kept, so the edit stays detectable")
        shutil.rmtree(self.src / "gamma")
        r = self.ok(self.install("--prune"))
        self.assertIn("added: extra dir/x.md", self.keep_line(r, "gamma"))
        self.assertTrue((self.target / "gamma" / "extra dir" / "x.md").exists())
        self.ok(self.install("--prune", "--force", "--skill", "gamma"))
        self.assertFalse((self.target / "gamma").exists())
        self.ok(self.uninstall("--force"))
        self.assertFalse(self.manifest.exists())
        self.assert_no_lock()

    def test_skill_md_replaced_by_a_directory_counts_as_modified(self):
        self.ok(self.install())
        (self.target / "beta" / "SKILL.md").unlink()
        (self.target / "beta" / "SKILL.md").mkdir()
        (self.target / "beta" / "SKILL.md" / "inner.md").write_text("x", encoding="utf-8")
        r = self.ok(self.uninstall())
        self.assertIn("removed: SKILL.md", self.keep_line(r, "beta"))
        self.assertTrue((self.target / "beta" / "SKILL.md" / "inner.md").is_file())

    def test_dry_run_reports_the_same_decisions_and_writes_nothing(self):
        self.ok(self.install())
        (self.target / "alpha" / "ref" / "new.md").write_text("mine", encoding="utf-8")
        self.bump_all("2")
        before = snapshot(self.root)
        dry = self.ok(self.install("--dry-run"))
        self.assertEqual(snapshot(self.root), before)
        self.assertIn("(added: ref/new.md)", self.keep_line(dry, "alpha"))
        real = self.ok(self.install())
        self.assertEqual(self.keep_line(dry, "alpha"), self.keep_line(real, "alpha"))

    def test_two_installs_give_identical_tree_and_manifest(self):
        def normalized():
            snap = snapshot(self.target)
            kind, data = snap[MANIFEST_NAME]
            snap[MANIFEST_NAME] = (kind, b"\n".join(ln for ln in data.split(b"\n")
                                                     if not ln.startswith(b"# updated_at:")))
            return snap
        self.ok(self.install())
        first = normalized()
        self.ok(self.install())
        self.assertEqual(normalized(), first)


class TestUpgradeFromOlderManifests(MultiFileBase):
    def test_format_2_entries_are_upgraded_when_their_files_match_the_source(self):
        self.ok(self.install())
        self.to_format_2()
        r = self.ok(self.install())
        self.assertIn("upgrading manifest from format 2 to 3", r.stdout)
        self.assertIn("updated=3 skipped=0", r.stdout)
        self.assertEqual(set(self.manifest_files()), set(SLUGS))
        self.assert_manifest_matches_disk()

    def test_format_2_entry_with_a_file_the_source_lacks_is_kept(self):
        self.ok(self.install())
        self.to_format_2()
        (self.target / "alpha" / "ref" / "mine.md").write_text("mine", encoding="utf-8")
        (self.target / "beta" / "ref" / "guide.md").write_text("edited", encoding="utf-8")
        r = self.ok(self.install())
        for slug, name in (("alpha", "ref/mine.md"), ("beta", "ref/guide.md")):
            line = self.keep_line(r, slug)
            self.assertIn("recorded only its SKILL.md", line)
            self.assertIn(name, line)
            self.assertIn(f"--force --skill {slug}", line)
        self.assertIn("updated=1 skipped=2", r.stdout)
        files = self.manifest_files()
        self.assertEqual(set(files), {"gamma"}, "no per-file record is invented for a kept copy")
        self.assertIn("# format: 3", self.manifest.read_text(encoding="utf-8"))
        # uninstall.sh sees the same: the unrecorded file may be the user's.
        r = self.ok(self.uninstall())
        self.assertIn("Run ./install.sh once", self.keep_line(r, "alpha"))
        self.assertTrue((self.target / "alpha" / "ref" / "mine.md").exists())
        self.assertFalse((self.target / "gamma").exists())
        self.ok(self.uninstall("--force", "--skill", "alpha"))
        self.assertFalse((self.target / "alpha").exists())

    def test_format_2_entry_with_edited_skill_md_is_still_modified(self):
        self.ok(self.install())
        self.to_format_2()
        (self.target / "alpha" / "SKILL.md").write_text("edit", encoding="utf-8")
        r = self.ok(self.install())
        self.assertIn("modified locally since it was installed (changed: SKILL.md)", self.keep_line(r, "alpha"))

    def test_uninstall_keeps_a_format_2_manifest_in_format_2(self):
        self.ok(self.install())
        self.to_format_2()
        before = self.manifest.read_bytes()
        r = self.ok(self.uninstall("--skill", "alpha"))
        # A format-2 entry records SKILL.md only, and alpha holds other files too.
        self.assertIn("recorded only its SKILL.md", self.keep_line(r, "alpha"))
        self.assertEqual(self.manifest.read_bytes(), before, "nothing removed, so nothing rewritten")
        self.ok(self.uninstall("--force", "--skill", "alpha"))
        text = self.manifest.read_text(encoding="utf-8")
        self.assertIn("# format: 2\n", text)
        self.assertEqual(self.manifest_files(), {}, "uninstall.sh never invents per-file lines")
        self.assertEqual(set(self.manifest_entries()), {"beta", "gamma"})

    def test_format_1_entries_with_extra_files_are_proven_against_the_source(self):
        self.ok(self.install())
        self.manifest.write_text("# ml-ai-skills manifest\n" + "".join(s + "\n" for s in SLUGS), encoding="utf-8")
        (self.target / "beta" / ".hidden").write_text("mine", encoding="utf-8")
        r = self.ok(self.install())
        self.assertIn("updated=2 skipped=1", r.stdout)
        self.assertIn("recorded only its SKILL.md", self.keep_line(r, "beta"))
        self.assertEqual((self.target / "beta" / ".hidden").read_text(encoding="utf-8"), "mine")
        self.assertEqual(set(self.manifest_files()), {"alpha", "gamma"})
        self.assert_manifest_matches_disk()

    def test_newer_format_is_refused_by_both_scripts(self):
        self.ok(self.install())
        text = self.manifest.read_text(encoding="utf-8").replace("# format: 3", "# format: 4")
        self.manifest.write_text(text, encoding="utf-8")
        before = snapshot(self.root)
        for r in (self.install(), self.uninstall("--force"), self.install("--dry-run")):
            self.assertEqual(r.returncode, 1, r.stderr)
            self.assertIn("understands formats 1 to 3", r.stderr)
        self.assertEqual(snapshot(self.root), before)


class TestHostileManifestFileLines(MultiFileBase):
    def append(self, *lines: str) -> None:
        with self.manifest.open("a", encoding="utf-8", newline="\n") as f:
            for line in lines:
                f.write(line + "\n")

    def test_traversal_paths_in_file_lines_are_never_used_as_paths(self):
        self.ok(self.install())
        victim_hash = sha256(self.victim / "keep.txt")
        hostile = (f"alpha\t{victim_hash}\t../../victim/keep.txt",
                   f"alpha\t{victim_hash}\t/abs/keep.txt",
                   f"beta\t{victim_hash}\t../victim",
                   f"../victim\t{victim_hash}\tkeep.txt")
        for run in (lambda: self.install("--force", "--prune"), lambda: self.uninstall("--force")):
            self.append(*hostile)  # install.sh drops them when it rewrites the manifest
            r = self.ok(run())
            self.assertEqual(r.stderr.count("not a valid 'slug<TAB>sha256<TAB>file' entry"), 4, r.stderr)
            self.assert_victim_intact()
        self.assertFalse(self.manifest.exists())
        self.assertEqual(list(self.target.iterdir()), [])

    def test_wrong_missing_or_extra_file_lines_only_keep_the_skill(self):
        self.ok(self.install())
        h = sha256(self.target / "alpha" / "SKILL.md")
        text = self.manifest.read_text(encoding="utf-8")
        lines = [ln for ln in text.splitlines()
                 if not (ln.startswith("beta\t") and ln.endswith("\t.hidden"))]  # a missing file line
        lines.append(f"gamma\t{'0' * 64}\tref/guide.md")   # duplicate: the first (correct) one wins
        lines.append(f"alpha\t{h}\tghost.md")              # extra: recorded but not on disk
        lines.append(f"nosuch\t{h}\tSKILL.md")             # no skill line
        self.manifest.write_text("\n".join(lines) + "\n", encoding="utf-8")
        r = self.ok(self.uninstall())
        self.assertIn("(removed: ghost.md)", self.keep_line(r, "alpha"))
        self.assertIn("(added: .hidden)", self.keep_line(r, "beta"))
        self.assertIn("no skill line for 'nosuch'", r.stderr)
        self.assertIn("a second entry for a file of 'gamma'", r.stderr)
        self.assertFalse((self.target / "gamma").exists(), "first entry wins, so gamma was unmodified")
        self.assertTrue((self.target / "alpha" / "SKILL.md").is_file())
        self.assertTrue((self.target / "beta" / ".hidden").is_file())

    def test_duplicate_skill_lines_first_one_wins(self):
        self.ok(self.install())
        self.append(f"alpha\t{'0' * 64}")
        r = self.ok(self.uninstall("--skill", "alpha"))
        self.assertIn("remove  alpha", r.stdout)

    def test_manifest_that_is_a_directory_is_refused(self):
        self.ok(self.install())
        self.manifest.unlink()
        self.manifest.mkdir()
        before = snapshot(self.root)
        r = self.install()
        self.assertEqual(r.returncode, 1, r.stderr)
        self.assertIn("not a regular file", r.stderr)
        r = self.uninstall()
        self.assertEqual(r.returncode, 1, r.stderr)
        self.assertIn("no ml-ai-skills manifest", r.stderr)
        self.assertEqual(snapshot(self.root), before)
        self.assert_no_lock()


class TestUnusualFileNames(Base):
    NAMES = ["sp ace.md", "uni-é-日本.md", "hash#x.md", "eq=x.md", "-lead.md", "brack[1].md",
             "dollar$HOME.md", "quote'x.md", " lead-space.md"]
    POSIX_ONLY = ["star*q?.md", 'dq"x.md', "trail-space.md ", "back\\slash.md"]  # not valid on Windows

    def setUp(self):
        super().setUp()
        self.names = names = self.NAMES + ([] if os.name == "nt" else self.POSIX_ONLY)
        for n in names:
            (self.src / "alpha" / "sub dir").mkdir(exist_ok=True)
            (self.src / "alpha" / n).write_text(n, encoding="utf-8")
            (self.src / "alpha" / "sub dir" / n).write_text(n, encoding="utf-8")

    def test_full_cycle_with_unusual_names_inside_a_skill(self):
        self.ok(self.install())
        files = self.manifest_files()["alpha"]
        for n in self.names:
            self.assertIn(n, files)
            self.assertIn(f"sub dir/{n}", files)
        self.assert_manifest_matches_disk()
        r = self.ok(self.install())
        self.assertIn("skipped=0", r.stdout, "an unchanged copy with odd names is recognised as ours")
        (self.target / "alpha" / self.names[1]).write_text("edit", encoding="utf-8")
        r = self.ok(self.uninstall())
        keep = [ln for ln in r.stderr.splitlines() if "keep    alpha " in ln]
        self.assertEqual(len(keep), 1, r.stderr)
        self.assertIn(f"(changed: {self.names[1]})", keep[0])
        self.ok(self.uninstall("--force"))
        self.assertEqual(list(self.target.iterdir()), [])

    def test_control_character_in_a_file_name_is_refused(self):
        if os.name == "nt":
            self.skipTest("Windows file names cannot contain control characters")
        (self.src / "beta" / "new\nline.md").write_text("x", encoding="utf-8")
        r = self.install()
        self.assertEqual(r.returncode, 1, r.stderr)
        self.assertIn("control character", r.stderr)
        self.assertFalse((self.target / "beta").exists())
        self.assertTrue((self.target / "alpha").is_dir())
        self.assert_manifest_matches_disk()


class TestPreExistingContent(MultiFileBase):
    def plant_user_content(self) -> dict[str, tuple]:
        self.target.mkdir(parents=True, exist_ok=True)
        (self.target / "my-skill").mkdir()
        (self.target / "my-skill" / "SKILL.md").write_text("mine", encoding="utf-8")
        (self.target / "README.txt").write_text("user file at the target root", encoding="utf-8")
        (self.target / "gamma").mkdir()   # same name as a source skill, not installed by us
        (self.target / "gamma" / "SKILL.md").write_text("their gamma", encoding="utf-8")
        return {k: v for k, v in snapshot(self.target).items()
                if k.split("/")[0] in ("my-skill", "README.txt", "gamma")}

    def assert_user_content(self, planted: dict[str, tuple]) -> None:
        now = snapshot(self.target)
        self.assertEqual({k: now.get(k) for k in planted}, planted)

    def test_rollback_after_a_halfway_failure_preserves_pre_existing_files(self):
        planted = self.plant_user_content()
        self.ok(self.install())  # alpha, beta installed; gamma skipped (not ours)
        self.assert_user_content(planted)
        (self.target / "alpha" / "ref" / "mine.md").write_text("mine", encoding="utf-8")
        planted = {k: v for k, v in snapshot(self.target).items() if k != MANIFEST_NAME}
        self.bump_all("2")
        r = self.install("--force", env=self.env(ML_AI_SKILLS_FAULT_INJECT="commit:beta"))
        self.assertEqual(r.returncode, 1, r.stderr)
        self.assertIn("restored previous copy of beta", r.stdout)
        now = snapshot(self.target)
        for k, v in planted.items():
            if k.split("/")[0] in ("beta", "my-skill", "README.txt"):
                self.assertEqual(now.get(k), v, k)
        self.assertEqual(self.content("alpha"), skill_text("alpha", "2"), "--force replaced alpha")
        self.assertEqual(self.content("gamma"), skill_text("gamma", "2"), "--force adopted gamma")
        self.assert_manifest_matches_disk()
        self.assert_no_lock()

    def test_unsafe_names_in_a_folder_we_did_not_install_are_not_examined(self):
        # Guarantee 3: a same-named folder the installer did not create is
        # skipped before anything below it is walked, so a link or an odd name
        # in it neither fails the run nor gets reported as "installed".
        self.target.mkdir(parents=True)
        (self.target / "gamma").mkdir()
        (self.target / "gamma" / "SKILL.md").write_text("their gamma", encoding="utf-8")
        self.make_dir_link(self.target / "gamma" / "linked", self.victim)
        if os.name != "nt":  # Windows file names cannot contain control characters
            (self.target / "gamma" / "odd\tname").write_text("x", encoding="utf-8")
        planted = {k: v for k, v in snapshot(self.target).items() if k.split("/")[0] == "gamma"}
        r = self.ok(self.install())
        self.assertIn("skip    gamma — already exists", r.stderr)
        self.assertIn("failed=0", r.stdout)
        self.assertNotIn("installed folder", r.stderr)
        r = self.install("--force")  # replacing it would copy a link: refused, not followed
        self.assertEqual(r.returncode, 1, r.stderr)
        self.assertIn("refuse  gamma — existing folder (not installed by ml-ai-skills)", r.stderr)
        now = snapshot(self.target)
        self.assertEqual({k: now.get(k) for k in planted}, planted)
        self.assertNotIn("gamma", self.manifest_entries())
        self.assert_victim_intact()
        self.assert_manifest_matches_disk()
        self.assert_no_lock()

    def test_failed_staging_preserves_everything(self):
        planted = self.plant_user_content()
        self.ok(self.install())
        before = snapshot(self.target)
        self.bump_all("2")
        r = self.install("--force", env=self.env(ML_AI_SKILLS_FAULT_INJECT="stage"))
        self.assertEqual(r.returncode, 1)
        self.assertEqual(snapshot(self.target), before)
        self.assert_user_content(planted)

    def test_lock_directory_left_by_a_crash_is_reported_not_reused(self):
        self.ok(self.install())
        (self.target / LOCK_NAME / "old" / "beta").mkdir(parents=True)
        before = snapshot(self.root)
        r = self.install()
        self.assertEqual(r.returncode, 3, r.stderr)
        self.assertIn("old/", r.stderr)
        self.assertEqual(snapshot(self.root), before)


if __name__ == "__main__":
    unittest.main()
