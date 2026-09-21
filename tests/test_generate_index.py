import tempfile
import unittest
from pathlib import Path

from testutil import make_skill
import generate_index as gi


SAMPLE_INDEX = """# ML / AI Skills — Master Index & Build Checklist

## 1. Foundations
- ✅ [W] `alpha` — does alpha things

## 2. Classical ML
- ⬜ [W] `beta` — does beta things

---

**Progress:** 1 / 2 written. Clone `_TEMPLATE/SKILL.md` to fill any ⬜ item.
"""


class TestIndexConsistency(unittest.TestCase):
    def _write_index(self, text: str) -> Path:
        tmp = tempfile.NamedTemporaryFile(mode="w", suffix=".md", delete=False, encoding="utf-8")
        tmp.write(text)
        tmp.close()
        return Path(tmp.name)

    def test_checkbox_drift_detected(self):
        alpha = make_skill("alpha", domain="foundations")
        beta = make_skill("beta", domain="classical-ml")
        idx = self._write_index(SAMPLE_INDEX)
        issues = gi.check_index_consistency([alpha, beta], idx)
        self.assertTrue(any("beta" in i and "checkbox" in i for i in issues))

    def test_progress_counter_drift_detected(self):
        # both alpha and beta actually exist on disk now (actual = 2/2) but
        # SAMPLE_INDEX's literal text still says "1 / 2 written"
        alpha = make_skill("alpha", domain="foundations")
        beta = make_skill("beta", domain="classical-ml")
        idx = self._write_index(SAMPLE_INDEX)
        issues = gi.check_index_consistency([alpha, beta], idx)
        self.assertTrue(any("progress counter" in i for i in issues))

    def test_missing_entry_detected(self):
        alpha = make_skill("alpha", domain="foundations")
        beta = make_skill("beta", domain="classical-ml")
        gamma = make_skill("gamma", domain="mlops")
        idx = self._write_index(SAMPLE_INDEX)
        issues = gi.check_index_consistency([alpha, beta, gamma], idx)
        self.assertTrue(any("gamma" in i and "no INDEX.md entry" in i for i in issues))

    def test_consistent_index_has_no_issues(self):
        alpha = make_skill("alpha", domain="foundations", type="workflow")
        beta = make_skill("beta", domain="classical-ml", type="workflow")
        consistent = SAMPLE_INDEX.replace("⬜", "✅").replace("1 / 2", "2 / 2")
        idx = self._write_index(consistent)
        issues = gi.check_index_consistency([alpha, beta], idx)
        self.assertEqual(issues, [])


if __name__ == "__main__":
    unittest.main()
