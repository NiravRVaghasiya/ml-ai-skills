"""The router's synonym map must stay backed by skill text (KNOWN-ISSUES #9).

evals/routing/synonym-audit.json records, for every key of scripts/router.py's
SYNONYMS, the skill text that backs it, and the keys removed because nothing
did. The evidence rule (also stated in the JSON's "method"):

* "same-passage": some skill uses the key and the canonical concept together -
  one prose passage (a paragraph, list item or Workflow step; fenced code, HTML
  comments, link targets and the References section left out), or the skill's
  metadata, contains the key phrase and at least one canonical stem that is not
  part of the key. A one-word key must also be used in that sense in at least
  half of the library's passages that contain it (SENSE_MIN): "nan" is kept out
  because the library mostly means not-a-number (a diverged loss, an undefined
  metric), not a missing value.
* "variant": a spelling of a kept key or of a metadata word ("chat bot",
  "warm up").

These tests recompute the rule against the current SKILL.md files, so a key
added after reading a benchmark query - or a skill edit that removes the
backing text - fails here.
"""
from __future__ import annotations

import json
import re
import unittest

import testutil  # noqa: F401  (inserts scripts/ and schema/ onto sys.path as a side effect)
import router
import skills_lib as sl

AUDIT_PATH = testutil.REPO_ROOT / "evals" / "routing" / "synonym-audit.json"
BASES = ("same-passage", "variant")
#: A one-word key is kept only if at least this share of the passages that contain it use it in the canonical sense.
SENSE_MIN = 0.5
_PASSAGE_RE = re.compile(r"\n\s*\n|\n(?=\s*(?:[-*]|\d+\.)\s)")
_LINK_RE = re.compile(r"\]\([^)]*\)|https?://\S+")
_REFERENCES_RE = re.compile(r"^## References\s*\n.*?(?=^## |\Z)", flags=re.M | re.S)
_NOT_RE = re.compile(r"\bNOT\b")


def stems_all(text: str) -> list[str]:
    return [router.stem(w) for w in router._words(text) if w not in router.CLAUSE_BREAKS]


def contains(seq: list[str], key: tuple[str, ...]) -> bool:
    n = len(key)
    return any(tuple(seq[i:i + n]) == key for i in range(len(seq) - n + 1))


def metadata_text(skill: sl.Skill) -> str:
    """Description before its NOT-for clause, capabilities, display name and slug."""
    fm = skill.frontmatter
    positive = _NOT_RE.split(" ".join(str(fm.get("description") or "").split()), maxsplit=1)[0]
    return " ".join([positive, " ".join(fm.get("capabilities") or []), str(fm.get("display_name") or ""),
                     skill.slug.replace("-", " ")])


def passages(skill: sl.Skill) -> list[list[str]]:
    """Stemmed passages: the metadata, then each prose paragraph / list item of the body."""
    scan = sl.scan_markdown(skill.body or "")
    prose = "\n".join(line if kind == "text" else "" for line, kind in zip(scan.lines, scan.kinds, strict=True))
    prose = _LINK_RE.sub("]", _REFERENCES_RE.sub("", prose))
    return [stems_all(metadata_text(skill))] + [stems_all(p) for p in _PASSAGE_RE.split(prose) if p.strip()]


def key_evidence(key: str, value: str, units: dict[str, list[list[str]]]) -> dict:
    """{"skills": skills with a same-passage use, "with_concept": n, "with_key": n, "passes": bool}."""
    k = tuple(stems_all(key))
    canon = [c for c in router._seq(value) if c not in k]
    with_key = with_concept = 0
    skills = set()
    for slug, seqs in units.items():
        for seq in seqs:
            if contains(seq, k):
                with_key += 1
                if canon and any(c in seq for c in canon):
                    with_concept += 1
                    skills.add(slug)
    sense_ok = len(k) > 1 or (with_key > 0 and with_concept / with_key >= SENSE_MIN)
    return {"skills": sorted(skills), "with_concept": with_concept, "with_key": with_key,
            "passes": bool(skills) and sense_ok}


def library_units() -> dict[str, list[list[str]]]:
    return {s.slug: passages(s) for s in sl.load_all_skills()}


class TestSynonymAudit(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.audit = json.loads(AUDIT_PATH.read_text(encoding="utf-8"))
        cls.units = library_units()
        cls.metadata_stems = {s for seqs in cls.units.values() for s in seqs[0]}

    def test_map_matches_the_audit(self):
        kept = {r["key"]: r["value"] for r in self.audit["kept"]}
        self.assertEqual(kept, router.SYNONYMS)
        for r in self.audit["removed"]:
            with self.subTest(removed=r["key"]):
                self.assertNotIn(r["key"], router.SYNONYMS, "re-added without skill-text evidence? update the audit")

    def test_every_kept_key_still_has_its_evidence(self):
        kept = {r["key"] for r in self.audit["kept"] if r["basis"] != "variant"}
        for r in self.audit["kept"]:
            with self.subTest(key=r["key"]):
                self.assertIn(r["basis"], BASES)
                if r["basis"] == "variant":
                    target = r["variant_of"]
                    in_metadata = all(s in self.metadata_stems for s in router._seq(target))
                    self.assertTrue(target in kept or in_metadata, f"{r['key']}: variant of nothing backed")
                    continue
                ev = key_evidence(r["key"], r["value"], self.units)
                self.assertTrue(ev["passes"], f"{r['key']!r} no longer meets the same-passage rule: {ev}")
                self.assertTrue(set(r["skills"]) & set(ev["skills"]),
                                f"none of {r['skills']} still uses {r['key']!r} with {r['value']!r}")

    def test_removed_keys_record_why(self):
        for r in self.audit["removed"]:
            with self.subTest(removed=r["key"]):
                self.assertTrue(r["reason"])
                self.assertIn("audit", r)

    def test_rule_rejects_the_ambiguous_one_word_keys(self):
        # Keys the 2026-10-29 round-1 audit kept on "occurs somewhere in a skill's text": in
        # this library they mostly mean something else (or are only a citation title).
        # ("bot", "llm") was on this list until 2026-10-30, when rag-pipeline's description
        # gained "a chatbot or support bot that answers from your own documents" next to
        # "the LLM": the library now uses "bot" in the LLM sense, so the rule accepts it.
        # The key stays out of router.SYNONYMS (test_map_matches_the_audit covers that).
        for key, value in (("nan", "missing"), ("assistant", "llm"), ("strip", "remove"),
                           ("shrink", "smaller"), ("attribution", "explain feature importance")):
            with self.subTest(key=key):
                self.assertFalse(key_evidence(key, value, self.units)["passes"])
        self.assertTrue(key_evidence("bot", "llm", self.units)["passes"])
        self.assertNotIn("bot", router.SYNONYMS)

    def test_rule_unit(self):
        units = {"a": [["drift"], stems_all("data drift: the input distribution shift"),
                       stems_all("shift the window by one step")],
                 "b": [stems_all("press shift to select")]}
        ev = key_evidence("shift", "drift", units)
        self.assertEqual((ev["with_concept"], ev["with_key"]), (1, 3))
        self.assertFalse(ev["passes"])  # 1 of 3 < SENSE_MIN
        units["a"].append(stems_all("a covariate shift is data drift"))
        units["b"] = [["x"]]
        self.assertTrue(key_evidence("shift", "drift", units)["passes"])  # 2 of 3
        # multi-word keys need one same-passage use, not a sense majority
        self.assertTrue(key_evidence("covariate shift", "drift", units)["passes"])
        # a canonical stem that is part of the key is not evidence ("labelled" -> "supervised label")
        self.assertFalse(key_evidence("labelled", "supervised label", {"a": [stems_all("labelled rows")]})["passes"])

    def test_audit_never_cites_test_queries(self):
        # Removed keys list the dev cases that used them; no test-split id may appear.
        import eval_router as er  # noqa: PLC0415
        split = {c["id"]: c["split"] for c in er.load_benchmark()["cases"]}
        for r in self.audit["removed"]:
            for cid in r["dev_ids"]:
                self.assertEqual(split.get(cid), "dev", f"{r['key']}: {cid}")


if __name__ == "__main__":
    unittest.main()
