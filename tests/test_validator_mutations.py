"""Mutation tests for scripts/validate_skills.py.

Each test copies the real skill tree into a temp dir (with a generated
INDEX.md), applies ONE representative defect to a known-good skill, runs the
validator against that tree via `--root`/`run(root)`, and asserts the
expected rule fires on the expected file/field. The unmutated copy must
produce zero errors, so the suite also guards against false positives on
every real skill.

Mutations are anchored on whole frontmatter keys / section headers (not on
prose) so concurrent content edits to the copied skills don't break them; a
mutation that fails to apply raises instead of silently testing nothing.
"""
from __future__ import annotations

import datetime as dt
import json
import re
import shutil
import tempfile
import unittest
from pathlib import Path

import testutil as tu
import validate_skills as vs

WF = "data-preprocessing"  # workflow gold standard (CLAUDE.md forbids casual edits)
REF = "attention-mechanisms"  # reference gold standard
OTHER = "feature-engineering"
THIRD = "model-evaluation"
TODAY = dt.date.today()


def _snapshot(root: Path) -> dict[Path, bytes]:
    return {p: p.read_bytes() for p in root.rglob("*") if p.is_file()}


class FixtureTree(unittest.TestCase):
    """One copied tree per test class, restored to its snapshot after every test.

    Restoring a handful of files is far cheaper than re-copying ~40 folders
    per test on Windows, and the restore is verified, so tests stay isolated.
    """

    root: Path

    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.mkdtemp(prefix="skills-mut-")
        cls.root = tu.copy_skill_tree(Path(cls._tmp) / "tree")
        cls._pristine = _snapshot(cls.root)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls._tmp, ignore_errors=True)

    def setUp(self):
        self._report = None
        self._touched: set[Path] = set()

    def tearDown(self):
        self.restore()

    def restore(self) -> None:
        """Undo every change made through the helpers below, then verify it."""
        for p in sorted(self._touched, key=lambda q: len(q.parts), reverse=True):
            if p in self._pristine:
                p.write_bytes(self._pristine[p])
            elif p.is_dir():
                shutil.rmtree(p)
            elif p.exists():
                p.unlink()
        for p in self._touched:
            if p in self._pristine:
                assert p.read_bytes() == self._pristine[p], f"restore failed for {p}"
            else:
                assert not p.exists(), f"restore failed to remove {p}"
        self._touched.clear()
        self._report = None

    # -- helpers (every mutation goes through these so restore() can undo it) --
    def write(self, path: Path, text: str) -> None:
        self._touched.add(path)
        path.write_text(text, encoding="utf-8", newline="\n")
        self._report = None

    def edit(self, slug: str, fn) -> None:
        self.write(self.root / slug / "SKILL.md", fn(tu.read_skill(self.root, slug)))

    def clone(self, src: str, dest: str, name: str | None = None) -> None:
        self._touched.add(self.root / dest)
        shutil.copytree(self.root / src, self.root / dest)
        if name is not None:
            self.edit(dest, lambda t: tu.set_key(t, "name", f" {name}"))
        self.reindex()

    def reindex(self) -> None:
        self._touched.add(self.root / "INDEX.md")
        tu.write_minimal_index(self.root)
        self._report = None

    def report(self) -> vs.Report:
        if self._report is None:  # cached until the tree is edited again
            self._report = vs.run(self.root, today=TODAY)
        return self._report

    def assertFinding(self, report_items, code, file=None, field=None, **extra):
        matches = [e for e in report_items if e["code"] == code
                   and (file is None or e["file"] == file)
                   and (field is None or e.get("field") == field)
                   and all(e.get(k) == v for k, v in extra.items())]
        if not matches:
            got = [(e["code"], e["file"], e.get("field")) for e in report_items]
            self.fail(f"expected {code} on {file} field={field} {extra or ''}; got {got}")
        for m in matches:  # every finding is actionable
            self.assertTrue(m.get("fix"), m)
            self.assertIn(m["code"], vs.RULES)
        return matches[0]

    def assertError(self, code, slug=WF, field=None, **extra):
        return self.assertFinding(self.report().errors, code, f"{slug}/SKILL.md" if slug else None, field, **extra)

    def errors_for(self, slug=WF):
        return [e for e in self.report().errors if e["file"] == f"{slug}/SKILL.md"]


class TestBaseline(FixtureTree):
    def test_unmutated_copy_has_no_errors(self):
        errors = self.report().errors
        self.assertEqual(errors, [], "validator reports errors on an unmodified copy of the real skills")

    def test_cli_root_json_clean_and_dirty(self):
        ok = tu.run_validator_cli(self.root, "--json")
        self.assertEqual(ok.returncode, 0, ok.stdout + ok.stderr)
        self.assertEqual(json.loads(ok.stdout)["errors"], [])

        self.edit(WF, lambda t: tu.set_key(t, "domain", " not-a-domain"))
        bad = tu.run_validator_cli(self.root, "--json")
        self.assertEqual(bad.returncode, 1)
        errors = json.loads(bad.stdout)["errors"]
        self.assertFinding(errors, "invalid-enum", f"{WF}/SKILL.md", "domain")

    def test_cli_human_output_has_location_and_fix(self):
        self.edit(WF, lambda t: tu.set_key(t, "level", " expert"))
        out = tu.run_validator_cli(self.root).stdout
        self.assertIn(f"ERROR [invalid-enum] {WF}/SKILL.md:", out)
        self.assertIn("fix: ", out)


class TestFrontmatterMutations(FixtureTree):
    def test_each_required_key_removed(self):
        # One validator run: remove a different required key from a different
        # skill each, then check every skill reports exactly its own key.
        slugs = [d.name for d in vs.sl.iter_skill_dirs(self.root)]
        self.assertGreaterEqual(len(slugs), len(vs.av.REQUIRED_KEYS))
        plan = dict(zip(vs.av.REQUIRED_KEYS, slugs))
        for key, slug in plan.items():
            self.edit(slug, lambda t, k=key: tu.remove_key(t, k))
        errors = self.report().errors
        for key, slug in plan.items():
            with self.subTest(key=key, skill=slug):
                self.assertFinding(errors, "missing-required-key", f"{slug}/SKILL.md", key)
                missing = {e["field"] for e in errors
                           if e["file"] == f"{slug}/SKILL.md" and e["code"] == "missing-required-key"}
                self.assertEqual(missing, {key})

    def test_empty_required_scalar(self):
        self.edit(WF, lambda t: tu.set_key(t, "inputs", ""))
        self.assertError("missing-required-key", field="inputs")

    def test_bad_enum(self):
        self.edit(WF, lambda t: tu.set_key(t, "domain", " not-a-domain"))
        e = self.assertError("invalid-enum", field="domain")
        self.assertIn("classical-ml", e["allowed"])

    def test_malformed_date(self):
        self.edit(WF, lambda t: tu.set_key(t, "last_verified", " 2026-13-45"))
        self.assertError("invalid-date", field="last_verified")

    def test_non_iso_date(self):
        self.edit(WF, lambda t: tu.set_key(t, "last_verified", " 21/09/2026"))
        self.assertError("invalid-date", field="last_verified")

    def test_future_date(self):
        future = (TODAY + dt.timedelta(days=30)).isoformat()
        self.edit(WF, lambda t: tu.set_key(t, "last_verified", f" {future}"))
        self.assertError("future-date", field="last_verified")

    def test_flow_list_is_supported(self):
        self.edit(WF, lambda t: tu.set_key(t, "capabilities", " [tabular-imputation, tabular-scaling]"))
        self.assertEqual(self.errors_for(), [])
        skill = vs.sl.load_skill(self.root / WF)
        self.assertEqual(skill.frontmatter["capabilities"], ["tabular-imputation", "tabular-scaling"])

    def test_flow_list_bad_item(self):
        self.edit(WF, lambda t: tu.set_key(t, "capabilities", " [Tabular_Imputation, ok-tag]"))
        self.assertError("invalid-list-item", field="capabilities", value="Tabular_Imputation")

    def test_hyphenated_key(self):
        self.edit(WF, lambda t: tu.set_key(t, "risk_level", " low").replace("risk_level: low", "risk-level: low"))
        e = self.assertError("unknown-key", field="risk-level")
        self.assertEqual(e["suggestion"], "risk_level")
        self.assertError("missing-required-key", field="risk_level")

    def test_duplicate_key(self):
        self.edit(WF, lambda t: tu.add_key(t, "level: beginner"))
        e = self.assertError("yaml-syntax", field="level", problem="duplicate-key")
        text = tu.read_skill(self.root, WF)
        expected_line = [i for i, ln in enumerate(text.split("\n"), 1) if ln == "level: beginner"][0]
        self.assertEqual(e["line"], expected_line)

    def test_unknown_key(self):
        self.edit(WF, lambda t: tu.add_key(t, "foo: bar"))
        self.assertError("unknown-key", field="foo")

    def test_unparseable_line(self):
        self.edit(WF, lambda t: tu.add_key(t, "this line is not yaml"))
        self.assertError("yaml-syntax", problem="unparseable-line")

    def test_tab_indentation(self):
        self.edit(WF, lambda t: tu.set_key(t, "related", f"\n\t- {OTHER}"))
        self.assertError("yaml-syntax", problem="tab-indentation")

    def test_unquoted_colon_value(self):
        self.edit(WF, lambda t: tu.set_key(t, "inputs", " A table: rows and columns"))
        self.assertError("yaml-syntax", field="inputs", problem="mapping-in-scalar")

    def test_inline_comment_is_stripped(self):
        self.edit(WF, lambda t: tu.set_key(t, "risk_level", " low  # reviewed"))
        self.assertEqual(self.errors_for(), [])
        self.assertEqual(vs.sl.load_skill(self.root / WF).frontmatter["risk_level"], "low")

    def test_duplicate_list_item(self):
        self.edit(WF, lambda t: tu.set_key(t, "related", f"\n  - {OTHER}\n  - {OTHER}"))
        self.assertError("duplicate-list-item", field="related")

    def test_list_key_given_scalar(self):
        self.edit(WF, lambda t: tu.set_key(t, "related", f" {OTHER}"))
        self.assertError("invalid-type", field="related")

    def test_duplicate_nested_lists_do_not_crash(self):
        # Nested lists are unhashable; duplicate detection once crashed the whole run here.
        self.edit(WF, lambda t: tu.set_key(t, "capabilities", "\n  -\n    - a\n  -\n    - a"))
        self.assertError("yaml-syntax", field="capabilities")
        self.assertError("invalid-type", field="capabilities")  # items are lists, not strings
        self.assertError("duplicate-list-item", field="capabilities")
        self.assertFalse([e for e in self.report().errors if e["file"] != f"{WF}/SKILL.md"])

    def test_scalar_version_constraints_is_one_finding(self):
        self.edit(WF, lambda t: tu.set_key(t, "version_constraints",
                                           ' "numpy>=1.0: np.array (model-knowledge estimate)x"'))
        self.assertError("invalid-type", field="version_constraints")
        vc_findings = [w for w in self.report().warnings + self.report().errors
                       if w["code"] == "version-constraint-format"]
        self.assertEqual(vc_findings, [])  # not one warning per character of the string

    def test_slug_item_with_trailing_newline(self):
        # Python's `$` matches before a final "\n"; JSON Schema's (ECMA-262) does not.
        self.edit(WF, lambda t: tu.set_key(t, "capabilities", "\n  - |\n    tabular-zz"))
        self.assertError("invalid-list-item", field="capabilities")

    def test_value_starting_with_flow_indicator(self):
        # YAML rejects the whole document, so Claude Code would load no fields at all.
        for value in (" ,leading comma", " }x", " ]x"):
            with self.subTest(value=value):
                self.edit(WF, lambda t, value=value: tu.set_key(t, "inputs", value))
                self.assertError("yaml-syntax", field="inputs")
                self.restore()

    def test_overlong_description(self):
        long_desc = ('Use when X. Trigger phrases: "a", "b", "c". NOT for Y. ' + "padding " * 140).strip()
        self.edit(WF, lambda t: tu.set_key(t, "description", f" >\n  {long_desc}"))
        self.assertError("description-too-long", field="description")

    def test_xml_tag_in_description(self):
        desc = 'Use when <task> happens. Trigger phrases: "a", "b", "c". NOT for Y (see z).'
        self.edit(WF, lambda t: tu.set_key(t, "description", f" >\n  {desc}"))
        self.assertError("description-xml-tag", field="description")

    def test_missing_not_for_clause(self):
        self.edit(WF, lambda t: tu.set_key(t, "description", ' >\n  Use when X. Trigger phrases: "a", "b", "c".'))
        self.assertError("description-not-for", field="description")

    def test_malformed_fence(self):
        def drop_closing_fence(t):
            _start, end = tu.frontmatter_bounds(t)
            return t[:end] + "-- not a fence" + t[end + 3:]
        self.edit(WF, drop_closing_fence)
        self.assertError("frontmatter-malformed")


class TestNamingMutations(FixtureTree):
    def test_folder_name_mismatch(self):
        self.edit(WF, lambda t: tu.set_key(t, "name", f" {WF}-x"))
        self.assertError("name-mismatch", field="name")

    def test_duplicate_names_across_folders(self):
        self.clone(WF, f"{WF}-copy")  # keeps name: data-preprocessing
        self.assertError("duplicate-name", slug=f"{WF}-copy", field="name")
        self.assertError("name-mismatch", slug=f"{WF}-copy", field="name")

    def test_uppercase_slug(self):
        self.clone(WF, "Data-Prep", name="Data-Prep")
        self.assertError("invalid-folder-name", slug="Data-Prep")
        self.assertError("invalid-name", slug="Data-Prep", field="name")

    def test_reserved_word_in_name(self):
        self.clone(WF, "claude-data-prep", name="claude-data-prep")
        self.assertError("name-reserved-word", slug="claude-data-prep", field="name")

    def test_name_too_long(self):
        slug = "x" * 70
        self.clone(WF, slug, name=slug)
        self.assertError("name-too-long", slug=slug, field="name")


class TestRelationshipMutations(FixtureTree):
    def test_unknown_related_slug(self):
        self.edit(WF, lambda t: tu.set_key(t, "related", "\n  - no-such-skill"))
        self.assertError("unknown-skill-reference", field="related", target="no-such-skill")

    def test_self_requires(self):
        self.edit(WF, lambda t: tu.set_key(t, "requires", f"\n  - {WF}"))
        self.assertError("self-reference", field="requires")

    def test_self_related(self):
        self.edit(WF, lambda t: tu.set_key(t, "related", f"\n  - {WF}"))
        self.assertError("self-reference", field="related")

    def test_requires_cycle_of_two(self):
        self.edit(WF, lambda t: tu.set_key(t, "requires", f"\n  - {OTHER}"))
        self.edit(OTHER, lambda t: tu.set_key(t, "requires", f"\n  - {WF}"))
        cyc = [e for e in self.report().errors if e["code"] == "requires-cycle"]
        self.assertTrue(cyc, "2-cycle not detected")
        self.assertEqual(cyc[0]["field"], "requires")
        self.assertTrue({WF, OTHER} <= set(cyc[0]["cycle"].split(" -> ")))
        self.assertIn(cyc[0]["file"], {f"{WF}/SKILL.md", f"{OTHER}/SKILL.md"})

    def test_requires_cycle_of_three(self):
        self.edit(WF, lambda t: tu.set_key(t, "requires", f"\n  - {OTHER}"))
        self.edit(OTHER, lambda t: tu.set_key(t, "requires", f"\n  - {THIRD}"))
        self.edit(THIRD, lambda t: tu.set_key(t, "requires", f"\n  - {WF}"))
        cyc = [e for e in self.report().errors if e["code"] == "requires-cycle"]
        self.assertTrue(cyc, "3-cycle not detected")
        self.assertTrue({WF, OTHER, THIRD} <= set(cyc[0]["cycle"].split(" -> ")))

    def test_requires_and_conflicts_overlap(self):
        self.edit(WF, lambda t: tu.set_key(tu.set_key(t, "requires", f"\n  - {OTHER}"),
                                           "conflicts", f"\n  - {OTHER}"))
        self.assertError("requires-conflicts-overlap", field="conflicts", target=OTHER)

    def test_asymmetric_conflicts_is_a_warning(self):
        self.edit(WF, lambda t: tu.set_key(t, "conflicts", f"\n  - {OTHER}"))
        report = self.report()
        self.assertFinding(report.warnings, "conflicts-asymmetric", f"{WF}/SKILL.md", "conflicts", target=OTHER)
        self.assertEqual([e for e in report.errors if e["file"] == f"{WF}/SKILL.md"], [])
        self.edit(OTHER, lambda t: tu.set_key(t, "conflicts", f"\n  - {WF}"))
        self.assertFalse([w for w in self.report().warnings if w["code"] == "conflicts-asymmetric"])

    def test_shared_capability_is_a_warning(self):
        tag = vs.sl.load_skill(self.root / WF).frontmatter["capabilities"][0]
        self.edit(REF, lambda t: tu.set_key(t, "capabilities", f"\n  - {tag}"))
        report = self.report()
        w = self.assertFinding(report.warnings, "capability-collision", f"{REF}/SKILL.md", "capabilities", value=tag)
        self.assertIn(WF, w["shared_with"])

    def test_near_duplicate_capability_is_a_warning(self):
        tag = vs.sl.load_skill(self.root / WF).frontmatter["capabilities"][0]
        self.edit(REF, lambda t: tu.set_key(t, "capabilities", f"\n  - ml-{tag}"))
        report = self.report()
        w = self.assertFinding(report.warnings, "capability-near-duplicate", f"{REF}/SKILL.md", "capabilities",
                               value=f"ml-{tag}", near=tag)
        self.assertEqual((w["reason"], w["shared_with"]), ("generic-prefix", [WF]))
        self.assertFinding(report.warnings, "capability-near-duplicate", f"{WF}/SKILL.md", "capabilities",
                           value=tag, near=f"ml-{tag}")
        self.assertEqual([e for e in report.errors if e["file"] == f"{REF}/SKILL.md"], [])

    def test_index_out_of_sync(self):
        index = self.root / "INDEX.md"
        self.write(index, "\n".join(ln for ln in index.read_text(encoding="utf-8").split("\n")
                                   if f"`{WF}`" not in ln))
        self.assertFinding(self.report().errors, "index-mismatch", "INDEX.md")


class TestBodyMutations(FixtureTree):
    def test_missing_section(self):
        self.edit(WF, lambda t: tu.sub_once(t, r"^## Gotchas[ \t]*$", "### Gotchas"))
        self.assertError("missing-section", section="Gotchas")

    def test_wrong_section_for_type(self):
        self.edit(REF, lambda t: tu.sub_once(t, r"^## Gotchas[ \t]*$",
                                             "## Workflow\n1. **Step** - do it.\n\n## Gotchas"))
        self.assertError("wrong-section-for-type", slug=REF, section="Workflow")

    def test_type_flip_requires_workflow(self):
        self.edit(REF, lambda t: tu.set_key(t, "type", " workflow"))
        self.assertError("missing-section", slug=REF, section="Workflow")
        self.assertError("wrong-section-for-type", slug=REF, section="Key Concepts")

    def test_too_few_gotchas(self):
        self.edit(WF, lambda t: re.sub(r"(?ms)^## Gotchas[ \t]*\n.*?(?=^## )", "## Gotchas\n- only one\n\n", t))
        self.assertError("too-few-gotchas", found=1)

    def test_too_few_references(self):
        self.edit(WF, lambda t: re.sub(r"(?ms)^## References[ \t]*\n.*", "## References\n- [a](https://a.example) - r\n", t))
        self.assertError("too-few-references", found=1)

    def test_unbalanced_fence(self):
        self.edit(WF, lambda t: t.rstrip("\n") + "\n\n```python\nprint('dangling')\n")
        self.assertError("unbalanced-fence")

    def test_unclosed_tilde_fence(self):
        # Old check counted ``` only, so an unclosed ~~~ fence (which swallows the rest of the
        # file when rendered) passed.
        self.edit(WF, lambda t: tu.sub_once(t, r"^## Overview[ \t]*$", "## Overview\n~~~text\nnever closed\n"))
        e = self.assertError("unbalanced-fence", fence="~~~")
        text = tu.read_skill(self.root, WF)
        self.assertEqual(e["line"], text.split("\n").index("~~~text") + 1)

    def test_section_heading_hidden_in_a_fence(self):
        self.edit(WF, lambda t: tu.sub_once(t, r"^## Gotchas[ \t]*$", "```markdown\n## Gotchas\n```"))
        self.assertError("missing-section", section="Gotchas")

    def test_setext_heading(self):
        self.edit(WF, lambda t: tu.sub_once(t, r"^## Gotchas[ \t]*$", "Gotchas\n-------"))
        e = self.assertError("setext-heading", heading="Gotchas", level=2)
        self.assertEqual(e["line"], tu.read_skill(self.root, WF).split("\n").index("-------") + 1)
        self.assertError("missing-section", section="Gotchas")

    def test_setext_heading_inside_a_numbered_step(self):
        # CommonMark renders a `---` indented to a list item's content, directly under
        # its paragraph, as an h2 inside the item: the step text becomes a heading.
        self.edit(WF, lambda t: tu.sub_once(t, r"^   target balance before touching anything\.$",
                                            "   target balance before touching anything.\n   ---"))
        e = self.assertError("setext-heading", level=2)
        self.assertTrue(e["heading"].startswith("**Profile the data first.**"), e["heading"])
        self.assertEqual(e["line"], tu.read_skill(self.root, WF).split("\n").index("   ---") + 1)

    def test_closing_hashes_still_name_the_section(self):
        self.edit(WF, lambda t: tu.sub_once(t, r"^## Gotchas[ \t]*$", "## Gotchas ##"))
        self.assertEqual(self.errors_for(), [])

    def test_unfilled_template_copy(self):
        """A skill folder created by copying _TEMPLATE/SKILL.md and not filling it in fails on
        its placeholders (with a fix that names the template), not on description-xml-tag."""
        dest = self.root / "new-skill"
        self._touched.add(dest)
        shutil.copytree(tu.REPO_ROOT / "_TEMPLATE", dest)
        self.reindex()
        self.assertError("placeholder-text", slug="new-skill", field="description")
        errors = self.errors_for("new-skill")
        self.assertNotIn("description-xml-tag", [e["code"] for e in errors])
        [placeholder] = [e for e in errors if e["code"] == "placeholder-text" and e.get("field") == "description"]
        self.assertIn("_TEMPLATE/SKILL.md", placeholder["fix"])
        self.assertError("name-mismatch", slug="new-skill", field="name")
        self.assertEqual([e for e in self.report().errors if e["file"] != "new-skill/SKILL.md"], [])

    def _append_to_overview(self, slug: str, text: str) -> None:
        self.edit(slug, lambda t: tu.sub_once(t, r"^## Overview[ \t]*$", f"## Overview\n{text}\n"))

    def test_broken_internal_link(self):
        self._append_to_overview(WF, "See [notes](notes/missing.md).")
        e = self.assertError("broken-link", url="notes/missing.md")
        self.assertIn("line", e)

    def test_relative_link_escaping_folder(self):
        self._append_to_overview(WF, f"See [sibling](../{REF}/SKILL.md).")
        self.assertError("link-escapes-skill", url=f"../{REF}/SKILL.md")
        self.assertFalse([e for e in self.report().errors if e["code"] == "broken-link"])

    def test_relative_link_inside_folder_is_fine(self):
        self.write(self.root / WF / "notes.md", "notes\n")
        self._append_to_overview(WF, "See [notes](notes.md#section).")
        self.assertEqual(self.errors_for(), [])

    def test_link_escape_in_every_link_syntax(self):
        sib = f"../{REF}/SKILL.md"
        forms = {
            "title": (f'See [x]({sib} "title").', sib),
            "angle": (f"See [x](<{sib}>).", sib),
            "backslash": (f"See [x](..\\{REF}\\SKILL.md).", f"..\\{REF}\\SKILL.md"),
            "file-url": ("See [x](file:///etc/passwd).", "file:///etc/passwd"),
            "drive-slash": ("See [x](C:/Windows/win.ini).", "C:/Windows/win.ini"),
            "drive-backslash": ("See [x](C:\\Windows\\win.ini).", "C:\\Windows\\win.ini"),
            "reference": (f"See [x][1].\n\n[1]: {sib}", sib),
            "reference-title": (f"See [x][s].\n\n[s]: <{sib}> \"t\"", sib),
            "html-href": (f'See <a href="{sib}">x</a>.', sib),
            "html-src": (f"<img src='{sib}' alt='x'>", sib),
        }
        for label, (text, url) in forms.items():
            with self.subTest(form=label):
                self._append_to_overview(WF, text)
                self.assertError("link-escapes-skill", url=url)
                self.restore()

    def test_link_forms_inside_folder_are_fine(self):
        self.write(self.root / WF / "notes.md", "notes\n")
        self._append_to_overview(
            WF, 'See [n](notes.md "t"), [r][n], <a href="notes.md">n</a>, [mail](mailto:a@b.c) '
                "and a footnote.[^1]\n\n[n]: notes.md\n[^1]: Footnote text, not a link.")
        self.assertEqual(self.errors_for(), [])

    def test_unresolved_wiki_link(self):
        self._append_to_overview(WF, "See [[no-such-skill]].")
        self.assertError("unresolved-wiki-link", target="no-such-skill")

    def test_wiki_link_resolves_and_code_is_ignored(self):
        self._append_to_overview(WF, f"See [[{OTHER}]] and `df[[\"a\"]]`, and `x[0](y)`.")
        self.assertEqual(self.errors_for(), [])


if __name__ == "__main__":
    unittest.main()
