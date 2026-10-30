"""Doc-consistency regression tests.

Docs that enumerate code-defined sets must name every member: the release file
list (build_release.TOP_FILES) in docs/RELEASE.md, and the generator table
(check_generated.GENERATORS) in CONTRIBUTING.md and CLAUDE.md. Also guards the
eval_router --router fix that docs/ROUTING.md documents, and the maintainer-owned
security-contact line in SECURITY.md.
"""
from __future__ import annotations

import re
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

from testutil import REPO_ROOT as ROOT  # noqa: E402

sys.path.insert(0, str(ROOT / "scripts"))
import build_release  # noqa: E402
import check_generated  # noqa: E402
import eval_router  # noqa: E402


def section(text: str, heading: str) -> str:
    m = re.search(rf"^## {re.escape(heading)}\n(.*?)(?=^## |\Z)", text, re.M | re.S)
    if m is None:
        raise AssertionError(f"no '## {heading}' section")
    return m.group(1)


class ReleaseDocListsShippedFiles(unittest.TestCase):
    def test_every_top_file_named_in_what_ships(self) -> None:
        ships = section((ROOT / "docs/RELEASE.md").read_text(encoding="utf-8"), "What ships")
        missing = sorted(f for f in build_release.TOP_FILES if f"`{f}`" not in ships)
        self.assertEqual(missing, [], "docs/RELEASE.md 'What ships' omits shipped files")


class RegenerationDocsNameEveryGenerator(unittest.TestCase):
    def test_contributing_and_claude_md_name_every_generator(self) -> None:
        for doc in ("CONTRIBUTING.md", "CLAUDE.md"):
            text = (ROOT / doc).read_text(encoding="utf-8")
            with self.subTest(doc=doc):
                missing = [g.script for g in check_generated.GENERATORS
                           if f"python {g.script}" not in text]
                self.assertEqual(missing, [], f"{doc} omits generators")


SECURITY_CONTACT_RE = re.compile(r"^Security contact: (?P<value>.*)$", re.M)


class SecurityContactConfigPoint(unittest.TestCase):
    """SECURITY.md has one maintainer-owned contact line. Until the maintainer fills it, it must
    say PENDING (never a guessed address), and the gap must stay visible in KNOWN-ISSUES and in
    the release docs as a pre-release external action."""

    def setUp(self) -> None:
        self.security = (ROOT / "SECURITY.md").read_text(encoding="utf-8")

    def contact(self) -> str:
        matches = SECURITY_CONTACT_RE.findall(self.security)
        self.assertEqual(len(matches), 1, "SECURITY.md needs exactly one 'Security contact: ...' line")
        return matches[0].strip()

    def test_config_point_is_marked_and_never_a_placeholder_address(self) -> None:
        self.assertIn("MAINTAINER-CONFIG: security-contact", self.security)
        value = self.contact()
        self.assertTrue(value)
        for bad in ("TODO", "example.com", "example.org", "<", ">"):
            self.assertNotIn(bad, value, "the contact line holds a placeholder dressed as a contact")
        if value.startswith("PENDING"):
            self.assertIn("maintainer action required", value)
        else:  # filled in: an address, a URL, or an explicit decision to rely on GitHub reporting
            self.assertTrue(re.fullmatch(r"\S+@\S+\.\S+|https://\S+|none: use private vulnerability reporting",
                                         value), value)

    def test_pending_contact_documents_the_interim_channel_and_maintainer_steps(self) -> None:
        if not self.contact().startswith("PENDING"):
            self.skipTest("security contact is configured; nothing is pending")
        flat = " ".join(self.security.split())  # phrases may wrap across lines
        for phrase in ("Report a vulnerability", "Private vulnerability reporting", "Advanced Security",
                       "asks the maintainer for a private security contact"):
            self.assertIn(phrase, flat)
        known = (ROOT / "docs/KNOWN-ISSUES.md").read_text(encoding="utf-8")
        self.assertRegex(known, r"(?m)^23\. \*\*", "KNOWN-ISSUES #23 must stay open while the contact is PENDING")

    def test_release_docs_list_the_contact_as_a_pre_release_external_action(self) -> None:
        if not self.contact().startswith("PENDING"):
            self.skipTest("security contact is configured; nothing is pending")
        actions = section((ROOT / "docs/RELEASE.md").read_text(encoding="utf-8"), "Pre-release external actions")
        self.assertIn("`SECURITY.md`", actions)
        self.assertIn("Security contact: PENDING", actions)
        self.assertIn("private vulnerability reporting", actions.lower())

    def test_release_docs_do_not_restate_the_settings_steps(self) -> None:
        # GitHub renames its settings pages; one copy of the click path (SECURITY.md,
        # "Maintainer configuration") cannot drift from another. RELEASE.md points to it.
        actions = section((ROOT / "docs/RELEASE.md").read_text(encoding="utf-8"), "Pre-release external actions")
        self.assertIn("Maintainer configuration", actions)
        security = (ROOT / "SECURITY.md").read_text(encoding="utf-8")
        for path in re.findall(r"Settings\s*(?:>|, then)\s*\**([A-Za-z][A-Za-z ]+)", actions):
            self.assertIn(path.strip(), security, "RELEASE.md names a settings page SECURITY.md does not")


class LoadRouteFnHandlesDataclassModules(unittest.TestCase):
    def test_dataclass_under_future_annotations_loads(self) -> None:
        src = textwrap.dedent("""
            from __future__ import annotations
            from dataclasses import dataclass

            @dataclass
            class Hit:
                slug: str

            def route(task, top_k=5):
                return [Hit("x")]
        """)
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "candidate_router.py"
            p.write_text(src, encoding="utf-8", newline="\n")
            fn = eval_router.load_route_fn(p)
            self.assertEqual(fn("anything")[0].slug, "x")


if __name__ == "__main__":
    unittest.main()
