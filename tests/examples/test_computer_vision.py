"""Executes computer-vision/SKILL.md's python blocks on tiny synthetic images (CPU).

Injected via ``namespace=`` (the user's data, per the skill's `inputs`):
``image``/``boxes``/``labels``/``mask`` (one sample for step 1),
``train_samples``/``val_samples`` (lists of ``(image, boxes, labels)``) and
``seg_train_samples``/``seg_val_samples`` (lists of ``(uint8 image, uint8
mask)``, the dtype PNG masks load as). The skill's own loaders are built from
these. Only ``test_flip_tta_merge_maps_mirrored_boxes_back`` swaps in a stub
model, to get known boxes (a random-init detector can't provide them).

Substitutions (``replace=``), and why:

* ``weights="DEFAULT"`` -> ``weights=None, weights_backbone=None, ...`` for
  both models: the default downloads pretrained checkpoints, and the suite must
  not need the network. ``aux_loss=True`` is passed to DeepLabV3 because the
  pretrained weights switch the aux head on (torchvision source:
  ``_ovewrite_value_param("aux_loss", aux_loss, True)``), so the test keeps
  the head-replacement code path the real run takes. The remaining Faster
  R-CNN kwargs are compute-size knobs only (like shrinking an iteration
  count): ``min_size/max_size=64`` shrinks the internal resize (default 800)
  and the RPN/ROI proposal counts drop from 1000-2000 proposals / 512 sampled
  ROIs to 50 / 32 — at default counts the v2 conv box head dominates CPU time
  (measured ~17.6 s vs ~2.4 s for one 2-image train step on a loaded 12-core
  machine). The score threshold stays at the skill's default (0.05).
  Random-init weights mean metric *values* are meaningless; only
  shapes/ranges/contracts are asserted.
* The segmentation crop ``size=(512, 512)`` -> ``(64, 64)`` (compute size
  only; the detection crop keeps 512).
* ``num_epochs = 10`` -> ``1`` (iteration count; shared by both training loops).

Note: ``import torchmetrics`` also imports ``transformers`` when that package
is installed (torchmetrics.functional.text.bert), which can add tens of
seconds to this file's runtime; the pinned examples environment does not need it.

Regression tests pin the defects found in the audit; each was demonstrated
against the pre-fix skill code (with the same weights=None substitution):

* ImageNet ``A.Normalize`` was fed to Faster R-CNN, whose ``model.transform``
  normalizes again (inputs ranged ~[-2.1, 2.6] instead of [0, 1]).
* ``BboxParams(format="coco")`` produced ``xywh`` boxes; torchvision reads
  ``xyxy`` and raised "All bounding boxes should have positive height and
  width" (or silently trained on wrong geometry).
* No Dataset existed (``train_dataset`` NameError); an image whose boxes were
  all cropped away yields ``np.asarray([])`` of shape ``(0,)``, which
  torchvision rejects — the target must be ``(0, 4)``.
* Replacing only ``classifier[4]`` left DeepLabV3's aux head predicting 21
  classes.
* Step 5 used undefined ``preds``/``seg_logits``/``seg_masks``; step 6 called
  ``.to()`` on the numpy image (AttributeError).

Defects found in review of the first fix (demonstrated on that code):

* ``seg_tfms`` + ``ToTensorV2`` yields a uint8 mask, and ``CrossEntropyLoss``
  raised "expected scalar type Long but found Byte"; there was no segmentation
  Dataset/loader, and ``seg_val_loader`` was undefined.
* Step 6 ran ``batched_nms`` on one model's already-NMS-ed output (a no-op)
  under a comment about merging, and overwrote step 1's ground-truth
  ``boxes``/``labels`` names with predictions.
"""
from __future__ import annotations

import contextlib
import io
import math
import unittest

from examples import harness

SLUG = "computer-vision"
NO_DOWNLOAD = {
    # compute-size only: the segmentation crop 512 -> 64 (a 512x512 DeepLabV3 train step at
    # B=2 measured ~46 s on this CPU); the detection crop keeps 512 and is asserted below
    0: {"seg_tfms = A.Compose([\n    A.RandomResizedCrop(size=(512, 512)":
        "seg_tfms = A.Compose([\n    A.RandomResizedCrop(size=(64, 64)"},
    2: {'fasterrcnn_resnet50_fpn_v2(weights="DEFAULT")':
        "fasterrcnn_resnet50_fpn_v2(weights=None, weights_backbone=None, min_size=64, max_size=64,"
        " rpn_pre_nms_top_n_train=100, rpn_post_nms_top_n_train=50, rpn_pre_nms_top_n_test=100,"
        " rpn_post_nms_top_n_test=50, box_batch_size_per_image=32)"},
    3: {'deeplabv3_resnet50(weights="DEFAULT")':
        "deeplabv3_resnet50(weights=None, weights_backbone=None, aux_loss=True)"},
    4: {"num_epochs = 10": "num_epochs = 1"},
}


def make_sample(rng, h=64, w=80):
    import numpy as np

    image = rng.integers(0, 256, (h, w, 3), dtype=np.uint8)
    boxes = [[5.0, 6.0, 40.0, 50.0], [30.0, 10.0, 75.0, 60.0]]   # pascal_voc xyxy, inside the image
    return image, boxes, [1, 3]


def make_seg_sample(rng, h=64, w=80):
    """(uint8 RGB image, uint8 mask) — the dtype a PNG mask loads as; 255 marks void pixels."""
    import numpy as np

    image = rng.integers(0, 256, (h, w, 3), dtype=np.uint8)
    mask = rng.integers(0, 4, (h, w)).astype(np.uint8)
    mask[:4] = 255
    return image, mask


def make_namespace():
    import numpy as np
    import torch

    torch.manual_seed(0)
    rng = np.random.default_rng(0)
    image, boxes, labels = make_sample(rng)
    _, mask = make_seg_sample(rng)
    return {
        "image": image, "boxes": boxes, "labels": labels, "mask": mask,
        "train_samples": [make_sample(rng) for _ in range(2)] + [(make_sample(rng)[0], [], [])],
        "val_samples": [make_sample(rng) for _ in range(2)],
        # 5 samples at batch_size=4: drop_last must drop the trailing batch of 1
        "seg_train_samples": [make_seg_sample(rng) for _ in range(5)],
        # different sizes: the val loader runs at full resolution with batch_size=1
        "seg_val_samples": [make_seg_sample(rng), make_seg_sample(rng, 48, 56)],
    }


@harness.requires("numpy", "torch", "torchvision", "albumentations", "torchmetrics", "pycocotools")
class ComputerVisionExampleTest(unittest.TestCase):
    ns: dict

    @classmethod
    def setUpClass(cls):
        with contextlib.redirect_stdout(io.StringIO()):   # step 5 prints metrics
            cls.ns = harness.run_blocks(SLUG, namespace=make_namespace(), replace=NO_DOWNLOAD)

    def test_detection_inputs_are_unit_range_not_imagenet_normalized(self):
        img = self.ns["transformed"]["image"]
        self.assertEqual(tuple(img.shape), (3, 512, 512))
        self.assertGreaterEqual(float(img.min()), 0.0)
        self.assertLessEqual(float(img.max()), 1.0)
        # the detector normalizes on its own — the reason the skill must not
        self.assertEqual(list(self.ns["model"].transform.image_mean), [0.485, 0.456, 0.406])

    def test_segmentation_inputs_are_imagenet_normalized(self):
        sample = self.ns["seg_sample"]
        self.assertLess(float(sample["image"].min()), 0.0)
        self.assertEqual(tuple(sample["mask"].shape), (64, 64))   # crop shrunk from 512 by NO_DOWNLOAD[0]
        self.assertTrue(set(sample["mask"].unique().tolist()) <= {0, 1, 2, 3, 255})
        # raw seg_tfms output keeps the uint8 mask dtype — why SegmentationDataset casts to int64
        self.assertEqual(str(sample["mask"].dtype), "torch.uint8")

    def test_val_transforms_keep_pascal_voc_boxes_unchanged(self):
        import numpy as np

        _, boxes, labels = make_sample(np.random.default_rng(1))
        ds = self.ns["DetectionDataset"]([(self.ns["image"], boxes, labels)], self.ns["val_tfms"])
        _, target = ds[0]
        self.assertEqual(target["boxes"].tolist(), boxes)
        self.assertEqual(target["labels"].tolist(), labels)

    def test_image_without_boxes_yields_n_by_4_target_the_model_accepts(self):
        import torch

        ds = self.ns["DetectionDataset"]([(self.ns["image"], [], [])], self.ns["val_tfms"])
        img, target = ds[0]
        self.assertEqual(tuple(target["boxes"].shape), (0, 4))
        self.assertEqual(target["labels"].dtype, torch.int64)
        model = self.ns["model"]
        model.train()
        losses = model([img], [target])
        self.assertTrue(all(math.isfinite(v.item()) for v in losses.values()))

    def test_segmentation_heads_match_num_classes_and_train_step_runs(self):
        import torch

        seg_model, k = self.ns["seg_model"], self.ns["num_classes"]
        self.assertEqual(seg_model.classifier[4].out_channels, k)
        self.assertEqual(seg_model.aux_classifier[4].out_channels, k)
        seg_model.eval()
        with torch.no_grad():
            out = seg_model(torch.randn(2, 3, 64, 64))
        self.assertEqual(out["aux"].shape[1], k)

    def test_segmentation_flow_feeds_int64_masks_from_seg_tfms(self):
        # regression: seg_tfms output (uint8 mask) went straight into CrossEntropyLoss
        import torch

        loader = self.ns["seg_loader"]
        self.assertEqual(len(loader), 1, "5 samples at batch_size=4 with drop_last -> one batch")
        images, masks = next(iter(loader))
        self.assertEqual(masks.dtype, torch.int64)
        self.assertEqual(tuple(masks.shape), (4, 64, 64))
        self.assertTrue(set(masks.unique().tolist()) <= {0, 1, 2, 3, 255})
        self.assertTrue(math.isfinite(self.ns["seg_loss"]))           # step 4 ran on these
        self.assertTrue(math.isfinite(self.ns["seg_train_step"](images, masks)))
        val_masks = [m for _, m in self.ns["seg_val_loader"]]
        self.assertEqual([tuple(m.shape) for m in val_masks], [(1, 64, 80), (1, 48, 56)])
        self.assertTrue(all(m.dtype == torch.int64 for m in val_masks))

    def test_segmentation_loader_rejects_fewer_samples_than_batch_size(self):
        # drop_last would otherwise make the epoch loop silently train on nothing
        needed = ("math", "DataLoader", "SegmentationDataset", "seg_tfms", "seg_val_tfms",
                  "seg_train_step", "num_epochs")
        ns = {k: self.ns[k] for k in needed}
        ns.update(seg_train_samples=self.ns["seg_train_samples"][:3], seg_val_samples=[])
        with self.assertRaisesRegex(ValueError, "at least 4"):
            harness.run_blocks(SLUG, [5], namespace=ns)

    def test_segmentation_train_step_rejects_batch_of_one(self):
        # pins the docstring claim: ASPP pooling BatchNorm needs B >= 2 in train mode
        import torch

        with self.assertRaisesRegex(ValueError, "Expected more than 1 value per channel"):
            self.ns["seg_train_step"](torch.randn(1, 3, 64, 64), torch.zeros(1, 64, 64, dtype=torch.int64))

    def test_metrics_and_inference_produce_well_formed_outputs(self):
        result = self.ns["map_metric"].compute()
        for key in ("map", "map_50", "map_75"):
            self.assertIn(key, result)
        iou = float(self.ns["iou_metric"].compute())
        self.assertTrue(0.0 <= iou <= 1.0)
        # step 6 ran on the raw numpy image; the merged union stays aligned and in-bounds
        all_boxes, width = self.ns["all_boxes"], self.ns["image"].shape[1]
        n_all = len(self.ns["all_scores"])
        self.assertGreater(n_all, 0, "default score threshold still yields detections to merge")
        self.assertEqual(tuple(all_boxes.shape), (n_all, 4))
        self.assertTrue(bool((all_boxes[:, 0] <= all_boxes[:, 2]).all()))
        self.assertTrue(bool((all_boxes[:, [0, 2]] >= 0).all() and (all_boxes[:, [0, 2]] <= width).all()))
        n = len(self.ns["pred_scores"])
        self.assertEqual(tuple(self.ns["pred_boxes"].shape), (n, 4))
        self.assertEqual(len(self.ns["pred_labels"]), n)
        self.assertTrue(bool((self.ns["pred_scores"] > 0.5).all()) if n else True)

    def test_inference_does_not_overwrite_ground_truth_names(self):
        # regression: step 6 reassigned `boxes`/`labels`, step 1's annotation inputs
        self.assertEqual(self.ns["boxes"], make_namespace()["boxes"])
        self.assertEqual(self.ns["labels"], make_namespace()["labels"])

    def test_flip_tta_merge_maps_mirrored_boxes_back(self):
        # stub detector: one box on the original image and its mirror image on the flipped
        # input. A correct un-flip makes the two coincide, so class-aware NMS keeps one.
        import torch

        original = torch.from_numpy(self.ns["image"]).permute(2, 0, 1).float().div(255)
        width = original.shape[-1]

        class MirrorStub:
            def eval(self):
                return self

            def __call__(self, imgs):
                mirrored = not torch.equal(imgs[0], original)
                box = [width - 30.0, 5.0, width - 10.0, 40.0] if mirrored else [10.0, 5.0, 30.0, 40.0]
                return [{"boxes": torch.tensor([box]), "scores": torch.tensor([0.9 if mirrored else 0.8]),
                         "labels": torch.tensor([1])}]

        ns = harness.run_blocks(SLUG, [7], namespace={
            "torch": torch, "model": MirrorStub(), "image": self.ns["image"], "device": torch.device("cpu")})
        self.assertEqual(ns["all_boxes"].tolist(), [[10.0, 5.0, 30.0, 40.0]] * 2)
        self.assertEqual(ns["pred_boxes"].tolist(), [[10.0, 5.0, 30.0, 40.0]])
        self.assertEqual(ns["pred_scores"].tolist(), [0.8999999761581421])   # the higher-scoring duplicate


if __name__ == "__main__":
    unittest.main()
