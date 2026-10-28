"""Doc-consistency regression tests.

Docs that enumerate code-defined sets must name every member: the release file
list (build_release.TOP_FILES) in docs/RELEASE.md, and the generator table
(check_generated.GENERATORS) in CONTRIBUTING.md and CLAUDE.md. Also guards the
eval_router --router fix that docs/ROUTING.md documents.
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
