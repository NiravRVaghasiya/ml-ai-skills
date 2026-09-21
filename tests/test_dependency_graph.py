import unittest

from testutil import make_skill
import build_dependency_graph as bdg


class TestCycles(unittest.TestCase):
    def test_no_cycle(self):
        a = make_skill("a", requires=["b"])
        b = make_skill("b", requires=[])
        self.assertEqual(bdg.find_cycles([a, b]), [])

    def test_direct_cycle(self):
        a = make_skill("a", requires=["b"])
        b = make_skill("b", requires=["a"])
        cycles = bdg.find_cycles([a, b])
        self.assertEqual(len(cycles), 1)

    def test_self_reference_ignored_if_target_missing(self):
        a = make_skill("a", requires=["ghost"])
        self.assertEqual(bdg.find_cycles([a]), [])

    def test_three_node_cycle(self):
        a = make_skill("a", requires=["b"])
        b = make_skill("b", requires=["c"])
        c = make_skill("c", requires=["a"])
        cycles = bdg.find_cycles([a, b, c])
        self.assertEqual(len(cycles), 1)


class TestOrphans(unittest.TestCase):
    def test_orphan_detected(self):
        a = make_skill("a", related=[], requires=[])
        b = make_skill("b", related=["a"], requires=[])
        # a is referenced by b, so a is NOT an orphan; b has outgoing edges so
        # b is not an orphan either. Add a truly isolated one.
        c = make_skill("c", related=[], requires=[])
        orphans = bdg.find_orphans([a, b, c])
        self.assertIn("c", orphans)
        self.assertNotIn("a", orphans)
        self.assertNotIn("b", orphans)


if __name__ == "__main__":
    unittest.main()
