"""Opt-in: cnn-vision's TF/Keras ``padding="same"`` claim, against a real ``keras.layers.Conv2D``.

Gate: ``ML_AI_SKILLS_INTEGRATION_TF=1``. Needs TensorFlow (CPU is enough); it is not in
requirements-examples.txt because no other skill code uses it.

What it tests: cnn-vision/SKILL.md Key Concepts says Keras ``"same"`` yields ``ceil(H_in / S)``
at stride ``S > 1`` (where PyTorch raises), keeps the size at stride 1, and that ``"valid"`` is
``P = 0`` in the output-size formula. The same cases as the offline PyTorch checks in
``tests/examples/test_cnn_vision.py`` are run through Conv2D and compared with those rules. It
also checks the reason the two frameworks differ: Keras pads asymmetrically (the extra row goes
at the bottom/right), so a strided "same" output equals PyTorch's only with that explicit
asymmetric padding. Not covered: Conv1D/3D, transposed convs, channels_first on GPU.
"""
from __future__ import annotations

import math
import unittest

from examples.test_cnn_vision import CASES, formula, skill_text
from integration import gate


@gate.opt_in("TF", what="TF/Keras Conv2D padding='same' output shapes", modules=("tensorflow", "numpy"))
class TestKerasSamePadding(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import os

        os.environ.setdefault("TF_CPP_MIN_LOG_LEVEL", "2")
        import tensorflow as tf

        cls.tf = tf
        cls.text = skill_text()

    def out_size(self, h_in, k, s, padding, d=1):
        tf = self.tf
        layer = tf.keras.layers.Conv2D(1, k, strides=s, padding=padding, dilation_rate=d, use_bias=False)
        return int(layer(tf.zeros((1, h_in, h_in, 1))).shape[1])

    def test_claim_is_still_in_the_skill(self):
        self.assertIn('TF/Keras `"same"` yields `⌈H_in/S⌉`', self.text)

    def test_same_is_ceil_of_h_over_s(self):
        for h_in, k, s, d in CASES:
            if d > 1 and s > 1:
                continue   # Keras rejects strides > 1 together with dilation_rate > 1
            with self.subTest(h_in=h_in, k=k, s=s, d=d):
                self.assertEqual(self.out_size(h_in, k, s, "same", d), math.ceil(h_in / s))

    def test_valid_matches_the_formula_with_zero_padding(self):
        for h_in, k, s, d in CASES:
            if d > 1 and s > 1:
                continue
            with self.subTest(h_in=h_in, k=k, s=s, d=d):
                self.assertEqual(self.out_size(h_in, k, s, "valid", d), formula(h_in, k, s, 0, d))

    def test_same_pads_the_extra_row_after_the_input(self):
        # H=8, K=3, S=2: ceil(8/2)=4 outputs need 1 row of padding in total, and Keras puts it
        # at the bottom: the result equals a VALID conv over the input padded (0, 1).
        import numpy as np

        tf = self.tf
        x = tf.constant(np.arange(64, dtype="float32").reshape(1, 8, 8, 1))
        kernel = tf.constant(np.arange(9, dtype="float32").reshape(3, 3, 1, 1))
        same = tf.nn.conv2d(x, kernel, strides=2, padding="SAME")
        padded = tf.pad(x, [[0, 0], [0, 1], [0, 1], [0, 0]])
        valid = tf.nn.conv2d(padded, kernel, strides=2, padding="VALID")
        self.assertEqual(tuple(same.shape), (1, 4, 4, 1))
        np.testing.assert_allclose(same.numpy(), valid.numpy())

    def test_tf_contrib_does_not_exist(self):
        # backs the 'tensorflow-contrib' STALE pattern (docs/claims.json tensorflow-2-removed-contrib)
        import importlib

        self.assertFalse(hasattr(self.tf, "contrib"))
        with self.assertRaises(ModuleNotFoundError):
            importlib.import_module("tensorflow.contrib")


if __name__ == "__main__":
    unittest.main()
