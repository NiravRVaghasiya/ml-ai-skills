"""schema/skill-frontmatter.schema.json: generation, drift, and validator agreement.

The validator enforces the schema with a small stdlib JSON-Schema subset
(skills_lib.schema_errors). When the real `jsonschema` package is installed,
these tests check that both validators agree on every repo skill and on a set
of deliberately broken frontmatters, so the subset can't quietly diverge from
the standard.
"""
import copy
import json
import re
import unittest

from testutil import sl
import allowed_values as av
import generate_schema as gs

try:
    import jsonschema
except ImportError:  # pragma: no cover - exercised on bare installs
    jsonschema = None

NEEDS_JSONSCHEMA = unittest.skipIf(jsonschema is None, "jsonschema not installed; cross-check skipped")


def _walk(schema):
    yield schema
    for key in ("items", "additionalProperties"):
        if isinstance(schema.get(key), dict):
            yield from _walk(schema[key])
    for sub in schema.get("properties", {}).values():
        yield from _walk(sub)


def mutated_instances():
    base = sl.load_skill(sl.REPO_ROOT / "data-preprocessing").frontmatter
    cases = {
        "valid": {},
        "bad-enum": {"domain": "nope"},
        "bad-date": {"last_verified": "2026-13-45"},
        "non-iso-date": {"last_verified": "20260921"},
        "date-typed-wrong": {"last_verified": 20260921},
        "name-pattern": {"name": "Bad_Name"},
        "name-long": {"name": "a" * 65},
        "desc-long": {"description": "x" * 1025},
        "empty-string": {"inputs": ""},
        "null-scalar": {"outputs": None},
        "list-as-string": {"related": "feature-engineering"},
        "list-null": {"requires": None},
        "list-bad-item": {"capabilities": ["Bad Tag", "ok-tag"]},
        "list-dup": {"related": ["a-b", "a-b"]},
        "list-item-null": {"related": [None]},
        "vc-empty-item": {"version_constraints": [""]},
        "unknown-key": {"risk-level": "low"},
    }
    for label, patch in cases.items():
        inst = copy.deepcopy(base)
        inst.update(patch)
        yield label, inst
    for key in av.REQUIRED_KEYS:
        inst = copy.deepcopy(base)
        del inst[key]
        yield f"missing-{key}", inst


class TestSchemaGeneration(unittest.TestCase):
    def test_committed_schema_matches_generator(self):
        self.assertEqual(gs.check(), [], "run: python scripts/generate_schema.py --write")

    def test_schema_mirrors_allowed_values(self):
        schema = json.loads(gs.SCHEMA_PATH.read_text(encoding="utf-8"))
        self.assertEqual(schema["required"], list(av.REQUIRED_KEYS))
        self.assertEqual(set(schema["properties"]), set(av.REQUIRED_KEYS) | set(av.OPTIONAL_KEYS))
        for key, values in av.ENUM_KEYS.items():
            self.assertEqual(schema["properties"][key]["enum"], list(values))
        self.assertFalse(schema["additionalProperties"])

    def test_schema_only_uses_keywords_the_stdlib_validator_implements(self):
        for node in _walk(gs.build_schema()):
            self.assertEqual(set(node) - sl.SCHEMA_KEYWORDS - sl.SCHEMA_ANNOTATIONS, set(), node)

    def test_stdlib_validator_rejects_unknown_keywords(self):
        with self.assertRaises(ValueError):
            sl.schema_errors({"type": "string", "oneOf": []}, "x")
        with self.assertRaises(ValueError):
            sl.schema_errors({"type": "string", "format": "email"}, "x")

    def test_enums_are_not_hand_duplicated_elsewhere(self):
        # The JSON file is generated; the only hand-written copy of an enum
        # value list is schema/allowed_values.py.
        for path in (sl.REPO_ROOT / "scripts").glob("*.py"):
            text = path.read_text(encoding="utf-8")
            with self.subTest(script=path.name):
                self.assertIsNone(re.search(r'"official-documentation",\s*"established-practice"', text))

    def test_pattern_dollar_has_ecma_semantics(self):
        # JSON Schema patterns are ECMA-262, where `$` matches only at the very
        # end; Python's `$` also matches before a final "\n", so a `|` block
        # item "tag\n" would otherwise pass the slug pattern.
        schema = gs.build_schema()["properties"]["capabilities"]
        self.assertEqual(sl.schema_errors(schema, ["tabular-zz"]), [])
        errs = sl.schema_errors(schema, ["tabular-zz\n"])
        self.assertEqual([(e.path, e.keyword) for e in errs], [((0,), "pattern")])
        self.assertTrue(sl.schema_errors(gs.build_schema()["properties"]["name"], "data-preprocessing\n"))
        # `$` inside a class or escaped is literal, not an anchor.
        self.assertEqual(sl.schema_errors({"type": "string", "pattern": r"^[$]\$$"}, "$$"), [])

    def test_every_repo_skill_is_schema_valid(self):
        schema = gs.build_schema()
        for skill in sl.load_all_skills():
            with self.subTest(skill=skill.slug):
                self.assertEqual(sl.schema_errors(schema, skill.frontmatter), [])


@NEEDS_JSONSCHEMA
class TestAgreementWithJsonschema(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.schema = json.loads(gs.SCHEMA_PATH.read_text(encoding="utf-8"))
        jsonschema.Draft202012Validator.check_schema(cls.schema)  # valid against the 2020-12 metaschema
        cls.reference = jsonschema.Draft202012Validator(cls.schema, format_checker=jsonschema.FormatChecker())

    def _theirs(self, instance):
        out = set()
        for e in self.reference.iter_errors(instance):
            prop = None
            if e.validator == "required":
                prop = re.match(r"'(.+)' is a required property", e.message).group(1)
            elif e.validator == "additionalProperties":
                prop = re.search(r"\('(.+)' was unexpected\)", e.message).group(1)
            out.add((tuple(e.absolute_path), e.validator, prop))
        return out

    @staticmethod
    def _ours(schema, instance):
        return {(e.path, e.keyword, e.property) for e in sl.schema_errors(schema, instance)}

    def test_agree_on_repo_skills(self):
        for skill in sl.load_all_skills():
            with self.subTest(skill=skill.slug):
                self.assertEqual(self._ours(self.schema, skill.frontmatter), self._theirs(skill.frontmatter))

    def test_known_deviation_trailing_newline(self):
        # The one intended disagreement: python-jsonschema evaluates `pattern`
        # with Python `re`, whose `$` accepts a trailing "\n"; we follow the
        # ECMA-262 semantics the JSON Schema spec requires. If this starts
        # failing, jsonschema changed and the deviation note can go.
        inst = copy.deepcopy(sl.load_skill(sl.REPO_ROOT / "data-preprocessing").frontmatter)
        inst["capabilities"] = ["tabular-zz\n"]
        self.assertEqual(self._theirs(inst), set())
        self.assertEqual(self._ours(self.schema, inst), {(("capabilities", 0), "pattern", None)})

    def test_agree_on_mutated_frontmatter(self):
        disagreements = 0
        for label, inst in mutated_instances():
            with self.subTest(case=label):
                ours, theirs = self._ours(self.schema, inst), self._theirs(inst)
                self.assertEqual(ours, theirs)
                if label != "valid" and label != "list-null":
                    self.assertTrue(ours, f"{label} should be invalid")
                disagreements += ours != theirs
        self.assertEqual(disagreements, 0)


if __name__ == "__main__":
    unittest.main()
