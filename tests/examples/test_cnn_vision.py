"""Checks the convolution output-size claims in cnn-vision/SKILL.md (a reference skill, no code blocks).

Key Concepts states the output-size formula, that stride-1 ``padding="same"`` keeps the size,
that PyTorch raises for ``padding="same"`` on strided convolutions, and that TF/Keras ``"same"``
yields ``ceil(H_in / S)``. The PyTorch half runs here, offline, against real ``torch.nn.Conv2d``;
the Keras half needs TensorFlow, which is not in requirements-examples.txt, and lives in the
opt-in ``tests/integration/test_tensorflow.py`` (``ML_AI_SKILLS_INTEGRATION_TF=1``).

Before this module the claims came from documentation only (docs/KNOWN-ISSUES.md #6).
"""
from __future__ import annotations

import unittest

from examples import harness

SLUG = "cnn-vision"
# (H_in, K, S, D) cases: odd/even sizes, strides 1-3, a dilated kernel
CASES = [(7, 3, 1, 1), (8, 3, 1, 1), (7, 3, 2, 1), (8, 3, 2, 1), (9, 5, 3, 1), (10, 3, 1, 2), (11, 3, 2, 2)]


def skill_text() -> str:
    return " ".join((harness.REPO_ROOT / SLUG / "SKILL.md").read_text(encoding="utf-8").split())


def formula(h_in: int, k: int, s: int, p: int, d: int = 1) -> int:
    """The skill's formula: floor((H_in + 2P - K_eff) / S) + 1 with K_eff = D(K-1)+1."""
    return (h_in + 2 * p - (d * (k - 1) + 1)) // s + 1


class TestClaimsAreStatedInTheSkill(unittest.TestCase):
    """Pins the wording the behavioural tests below check, so an edit to it is noticed."""

    def test_wording(self):
        text = skill_text()
        self.assertIn(r"H_{out} = \left\lfloor \frac{H_{in} + 2P - K}{S} \right\rfloor + 1", text)
        self.assertIn("replace `K` by `D(K-1)+1`", text)
        self.assertIn('At stride 1, `padding="same"` picks `P` so `H_out == H_in`', text)
        self.assertIn('TF/Keras `"same"` yields `⌈H_in/S⌉`', text)
        self.assertIn('PyTorch raises an error for `padding="same"` on strided convs', text)
        self.assertIn('`padding="valid"` means `P=0`', text)


@harness.requires("torch")
class TestPyTorchConvMatchesTheSkill(unittest.TestCase):
    def out_size(self, h_in, k, s, padding, d=1):
        import torch

        conv = torch.nn.Conv2d(1, 1, kernel_size=k, stride=s, padding=padding, dilation=d, bias=False)
        with torch.no_grad():
            return conv(torch.zeros(1, 1, h_in, h_in)).shape[-1]

    def test_formula_matches_explicit_padding(self):
        for h_in, k, s, d in CASES:
            for p in (0, 1, 2):
                with self.subTest(h_in=h_in, k=k, s=s, d=d, p=p):
                    self.assertEqual(self.out_size(h_in, k, s, p, d), formula(h_in, k, s, p, d))

    def test_valid_is_zero_padding(self):
        for h_in, k, s, d in CASES:
            with self.subTest(h_in=h_in, k=k, s=s, d=d):
                self.assertEqual(self.out_size(h_in, k, s, "valid", d), formula(h_in, k, s, 0, d))

    def test_same_keeps_size_at_stride_1(self):
        for h_in, k, s, d in CASES:
            if s == 1:
                with self.subTest(h_in=h_in, k=k, d=d):
                    self.assertEqual(self.out_size(h_in, k, 1, "same", d), h_in)

    def test_same_raises_for_strided_convs(self):
        import torch

        with self.assertRaisesRegex(ValueError, "same"):
            torch.nn.Conv2d(1, 1, kernel_size=3, stride=2, padding="same")


if __name__ == "__main__":
    unittest.main()
