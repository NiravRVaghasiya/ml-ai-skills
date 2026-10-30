---
name: computer-vision
display_name: Computer Vision (Detection, Segmentation, Augmentation)
description: >
  Use when the user wants to detect objects, segment images, or set up
  image augmentation for a computer vision training pipeline. Trigger
  phrases: "detect objects in these images", "train an object detector",
  "segment this image", "build an image augmentation pipeline", "fine-tune
  a detection model on my dataset". NOT for convolutional architecture
  theory or how CNNs work internally (see cnn-vision) or generic training
  loop mechanics (see training-deep-models).
type: workflow
domain: specialized
level: intermediate
lifecycle: stable
risk_level: low
evidence_level: official-documentation
last_verified: 2026-10-27
capabilities:
  - bbox-mask-aware-augmentation
  - object-detector-finetuning
  - segmentation-model-finetuning
  - map-iou-evaluation
  - non-max-suppression-tuning
requires:
conflicts:
related:
  - cnn-vision
  - data-preprocessing
  - model-evaluation
  - training-deep-models
inputs: An image dataset with bounding-box or segmentation-mask annotations plus a target class list.
outputs: A fine-tuned torchvision detector or segmentation model, an augmentation pipeline, and mAP/IoU evaluation results.
version_constraints:
  - "torchvision>=0.13: weights= API, fasterrcnn_resnet50_fpn_v2, deeplabv3_resnet50 aux_classifier, ops.batched_nms (0.13 lower bound documented: the torchvision v0.13.0 release notes introduce the multi-weight weights= API and fasterrcnn_resnet50_fpn_v2, read 2026-10-29; 0.13 itself not executed; executed with weights=None against torchvision 0.28.0 + torch 2.13.0 CPU on 2026-10-27; pretrained-weight download not executed)"
  - "albumentations>=2.0: RandomResizedCrop(size=...), BboxParams(min_visibility=...), ToFloat, ToTensorV2 (executed against albumentations 2.0.8 on 2026-10-27; whether 1.4.x also accepts size= was not verified)"
  - "torchmetrics>=1.0: detection.MeanAveragePrecision (pycocotools backend) and JaccardIndex(task=..., ignore_index=...) (1.0 lower bound documented for the backend: the torchmetrics v1.0.0 release notes say MeanAveragePrecision changed to the pycocotools backend, read 2026-10-29; JaccardIndex(task=) on 1.0 and 1.0 itself not verified; executed against torchmetrics 1.9.0 + pycocotools 2.0.11 on 2026-10-27)"
---

## Overview
Covers the applied computer-vision pipeline: augmenting an image dataset
correctly, fine-tuning a pretrained object detector, and fine-tuning a
pretrained segmentation model, using torchvision and Albumentations. The
user walks away with a training-ready dataset/model pair and the right
metrics (mAP, IoU) to judge them — architecture internals live in
`cnn-vision`.

## Workflow
1. **Augment images and labels together.** Bounding boxes and masks must be
   transformed in sync with the image, or augmentation corrupts the labels.
   Use `pascal_voc` boxes (absolute `[x_min, y_min, x_max, y_max]`) — the format
   torchvision detectors and torchmetrics' `MeanAveragePrecision` expect;
   convert COCO `[x, y, w, h]` annotations first.
   ```python
   import albumentations as A
   from albumentations.pytorch import ToTensorV2

   bbox_params = A.BboxParams(format="pascal_voc", label_fields=["labels"],
                              min_visibility=0.3)   # drop boxes mostly cropped away; tune per dataset
   train_tfms = A.Compose(
       [
           A.RandomResizedCrop(size=(512, 512), scale=(0.8, 1.0)),
           A.HorizontalFlip(p=0.5),
           A.ColorJitter(brightness=0.2, contrast=0.2, p=0.3),
           # torchvision *detection* models normalize internally (model.transform), so feed
           # [0, 1] floats; an ImageNet Normalize here would be applied twice
           A.ToFloat(max_value=255),
           ToTensorV2(),
       ],
       bbox_params=bbox_params,
   )
   val_tfms = A.Compose([A.ToFloat(max_value=255), ToTensorV2()], bbox_params=bbox_params)  # no randomness
   transformed = train_tfms(image=image, bboxes=boxes, labels=labels)   # image: HxWx3 uint8 RGB

   # Segmentation models do NOT normalize internally: apply ImageNet mean/std yourself.
   seg_tfms = A.Compose([
       A.RandomResizedCrop(size=(512, 512), scale=(0.8, 1.0)),
       A.HorizontalFlip(p=0.5),
       A.Normalize(mean=(0.485, 0.456, 0.406), std=(0.229, 0.224, 0.225)),
       ToTensorV2(),
   ])
   seg_val_tfms = A.Compose([A.Normalize(mean=(0.485, 0.456, 0.406), std=(0.229, 0.224, 0.225)),
                             ToTensorV2()])   # no randomness, full resolution
   seg_sample = seg_tfms(image=image, mask=mask)   # mask: HxW int class ids (nearest-neighbour resize)
   ```
   Wrap the transforms in `Dataset`s that emit what the models and losses
   require: the detection target dict, and int64 segmentation masks.
   ```python
   import numpy as np
   import torch

   class DetectionDataset(torch.utils.data.Dataset):
       """samples: list of (HxWx3 uint8 RGB image, [[x_min, y_min, x_max, y_max], ...], [class_id, ...]).
       Class ids start at 1: torchvision detectors reserve 0 for background."""

       def __init__(self, samples, transforms):
           self.samples, self.transforms = samples, transforms

       def __len__(self):
           return len(self.samples)

       def __getitem__(self, idx):
           image, boxes, labels = self.samples[idx]
           out = self.transforms(image=image, bboxes=boxes, labels=labels)
           target = {
               # reshape keeps shape (0, 4) when every box was cropped away; the model rejects shape (0,)
               "boxes": torch.as_tensor(np.asarray(out["bboxes"], dtype=np.float32)).reshape(-1, 4),
               "labels": torch.as_tensor(np.asarray(out["labels"]), dtype=torch.int64),
           }
           return out["image"], target

   class SegmentationDataset(torch.utils.data.Dataset):
       """samples: list of (HxWx3 uint8 RGB image, HxW integer mask of class ids; 255 = void)."""

       def __init__(self, samples, transforms):
           self.samples, self.transforms = samples, transforms

       def __len__(self):
           return len(self.samples)

       def __getitem__(self, idx):
           image, mask = self.samples[idx]
           out = self.transforms(image=image, mask=mask)
           # ToTensorV2 keeps the mask's dtype (uint8 for typical PNG masks), but
           # CrossEntropyLoss needs int64 class ids ("expected scalar type Long but found Byte")
           return out["image"], out["mask"].long()
   ```
2. **Fine-tune an object detector.** Start from a COCO-pretrained
   Faster R-CNN and replace only the classification head for your classes.
   ```python
   from torchvision.models.detection import fasterrcnn_resnet50_fpn_v2
   from torchvision.models.detection.faster_rcnn import FastRCNNPredictor

   num_classes = 3 + 1  # + background
   model = fasterrcnn_resnet50_fpn_v2(weights="DEFAULT")   # downloads COCO weights on first use
   in_features = model.roi_heads.box_predictor.cls_score.in_features
   model.roi_heads.box_predictor = FastRCNNPredictor(in_features, num_classes)

   device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
   model.to(device)
   ```
3. **Fine-tune a segmentation model.** DeepLabV3 with a ResNet backbone is a
   solid pretrained baseline for semantic segmentation. Pretrained weights also
   enable its auxiliary head, which needs the same class count as the main head.
   ```python
   from torchvision.models.segmentation import deeplabv3_resnet50

   seg_model = deeplabv3_resnet50(weights="DEFAULT")
   seg_model.classifier[4] = torch.nn.Conv2d(256, num_classes, kernel_size=1)
   if seg_model.aux_classifier is not None:   # else the aux head still predicts the 21 VOC classes
       seg_model.aux_classifier[4] = torch.nn.Conv2d(256, num_classes, kernel_size=1)
   seg_model.to(device)

   seg_criterion = torch.nn.CrossEntropyLoss(ignore_index=255)   # 255 = unlabeled/void pixels
   seg_optimizer = torch.optim.SGD(seg_model.parameters(), lr=0.01, momentum=0.9, weight_decay=1e-4)

   def seg_train_step(images, masks):
       """images: float [B, 3, H, W] normalized as in seg_tfms; masks: int64 [B, H, W]; B >= 2
       (ASPP's image-pooling BatchNorm sees one value per channel at B == 1 and raises in train mode)."""
       seg_model.train()
       out = seg_model(images.to(device))         # dict: "out", plus "aux" when the aux head is enabled
       masks = masks.to(device)
       loss = seg_criterion(out["out"], masks)
       if "aux" in out:
           loss = loss + 0.5 * seg_criterion(out["aux"], masks)   # weighting used by torchvision's references
       seg_optimizer.zero_grad()
       loss.backward()
       seg_optimizer.step()
       return loss.item()
   ```
4. **Run the training loop.** Detection models in torchvision return the loss
   dict directly in train mode. `train_samples` / `val_samples` are your lists
   of `(image, boxes, labels)` in the format `DetectionDataset` documents.
   ```python
   import math
   from torch.utils.data import DataLoader

   def collate_fn(batch):          # images/targets vary in size, so keep them as tuples; a module-level
       return tuple(zip(*batch))   # function (unlike a lambda) still pickles for num_workers > 0

   loader = DataLoader(DetectionDataset(train_samples, train_tfms), batch_size=4, shuffle=True,
                       collate_fn=collate_fn)
   val_loader = DataLoader(DetectionDataset(val_samples, val_tfms), batch_size=4, collate_fn=collate_fn)
   optimizer = torch.optim.SGD(model.parameters(), lr=0.005, momentum=0.9, weight_decay=5e-4)
   num_epochs = 10

   for epoch in range(num_epochs):
       model.train()
       for images, targets in loader:
           images = [img.to(device) for img in images]
           targets = [{k: v.to(device) for k, v in t.items()} for t in targets]
           loss_dict = model(images, targets)
           loss = sum(loss_dict.values())
           if not math.isfinite(loss.item()):    # diverged: stop instead of training on NaN
               raise RuntimeError(f"non-finite loss at epoch {epoch}: {loss_dict}")
           optimizer.zero_grad()
           loss.backward()
           optimizer.step()
   ```
   The segmentation model trains the same way through `seg_train_step`;
   `seg_train_samples` / `seg_val_samples` are lists of `(image, mask)` in the
   format `SegmentationDataset` documents.
   ```python
   seg_loader = DataLoader(SegmentationDataset(seg_train_samples, seg_tfms), batch_size=4, shuffle=True,
                           drop_last=True)   # a trailing batch of 1 would hit seg_train_step's B >= 2 limit
   if len(seg_loader) == 0:                  # drop_last with fewer samples than batch_size yields no batches
       raise ValueError(f"need at least {seg_loader.batch_size} segmentation training samples")
   seg_val_loader = DataLoader(SegmentationDataset(seg_val_samples, seg_val_tfms),
                               batch_size=1)   # full resolution, so image sizes may differ between samples

   for epoch in range(num_epochs):
       for images, masks in seg_loader:
           seg_loss = seg_train_step(images, masks)
           if not math.isfinite(seg_loss):
               raise RuntimeError(f"non-finite segmentation loss at epoch {epoch}")
   ```
5. **Evaluate with detection/segmentation metrics.** Pixel accuracy hides
   class imbalance; mAP and IoU are the standard metrics. Feed mAP the model's
   raw (low score-threshold) detections — it integrates over the score ranking,
   so pre-filtering at a confidence cut-off understates it.
   ```python
   from torchmetrics.detection import MeanAveragePrecision   # default backend needs `pip install pycocotools`
   from torchmetrics import JaccardIndex

   model.eval()
   map_metric = MeanAveragePrecision(iou_type="bbox")   # box_format="xyxy" (default) matches pascal_voc
   with torch.no_grad():
       for images, targets in val_loader:
           preds = model([img.to(device) for img in images])
           map_metric.update([{k: v.cpu() for k, v in p.items()} for p in preds], list(targets))
   print(map_metric.compute())                # map (mAP@[.5:.95]), map_50, map_75, ...

   # seg_val_loader (step 4): seg_val_tfms (no randomness) and int64 masks with 255 = void
   iou_metric = JaccardIndex(task="multiclass", num_classes=num_classes, ignore_index=255).to(device)
   seg_model.eval()
   with torch.no_grad():
       for images, masks in seg_val_loader:
           iou_metric.update(seg_model(images.to(device))["out"].argmax(1), masks.to(device))
   print(iou_metric.compute())                # mean IoU over classes
   ```
6. **Run inference and apply non-max suppression.** Pretrained detectors
   apply NMS internally (`model.roi_heads.nms_thresh`, 0.5 by default), so
   re-running it on one model's output changes nothing. Re-run class-aware NMS
   when you merge several prediction sets — here, horizontal-flip test-time
   augmentation (the same applies to multi-scale or ensembled models).
   ```python
   from torchvision.ops import batched_nms

   model.eval()
   img_tensor = torch.from_numpy(image).permute(2, 0, 1).float().div(255)   # HxWx3 uint8 -> 3xHxW in [0, 1]
   with torch.no_grad():
       runs = [model([img_tensor.to(device)])[0],
               model([img_tensor.flip(-1).to(device)])[0]]   # second pass on the mirrored image
   width = img_tensor.shape[-1]
   flipped = runs[1]["boxes"].clone()
   flipped[:, [0, 2]] = width - runs[1]["boxes"][:, [2, 0]]   # map mirrored x_min/x_max back
   runs[1] = {**runs[1], "boxes": flipped}

   all_boxes = torch.cat([r["boxes"] for r in runs])
   all_scores = torch.cat([r["scores"] for r in runs])
   all_labels = torch.cat([r["labels"] for r in runs])
   keep_idx = batched_nms(all_boxes, all_scores, all_labels, iou_threshold=0.5)   # per-class NMS on the union
   keep_idx = keep_idx[all_scores[keep_idx] > 0.5]   # application-level cut-off; tune on validation data
   # distinct names: step 1's `boxes`/`labels` are ground-truth annotations, not predictions
   pred_boxes, pred_labels, pred_scores = all_boxes[keep_idx], all_labels[keep_idx], all_scores[keep_idx]
   ```

## Gotchas
- **Transforming image without labels.** Plain `torchvision.transforms`
  applied only to the image (not the boxes/masks) silently desyncs labels
  after crops, flips, or resizes — use Albumentations with `bbox_params`/
  `mask` support, or write the box/mask transform by hand.
- **Normalization mismatch (and double normalization).** Pretrained
  torchvision classification/segmentation models expect ImageNet mean/std
  normalization from you; torchvision *detection* models (Faster R-CNN,
  RetinaNet, ...) apply it themselves in `model.transform` and expect `[0, 1]`
  inputs. Skipping it, or applying it twice, tanks accuracy without raising an
  error.
- **Box format and empty targets.** torchvision detectors want absolute
  `xyxy` boxes as a float tensor of shape `[N, 4]` and int64 labels starting
  at 1; COCO `xywh` boxes passed through unconverted either fail the
  positive-width/height assertion or train on wrong geometry, and an image
  whose boxes were all cropped away must still yield shape `(0, 4)`, not `(0,)`.
- **Mask dtype.** Masks loaded from PNGs are usually `uint8`, and
  `ToTensorV2` keeps that dtype; `CrossEntropyLoss` needs `int64` class ids
  and raises "expected scalar type Long but found Byte". Cast with `.long()`
  in the `Dataset` (as `SegmentationDataset` does) and keep the void value
  (255) equal to `ignore_index`.
- **Pixel accuracy is misleading for segmentation.** When background pixels
  dominate the mask, a model that predicts all-background scores high
  pixel accuracy — use IoU/Dice per class instead.
- **Aspect-ratio distortion.** Naive resize-to-square stretches small/thin
  objects out of proportion; use letterbox padding (resize + pad) to
  preserve aspect ratio, especially for detection.
- **NMS threshold tuning.** Too aggressive an IoU threshold in non-max
  suppression drops genuinely separate, closely-spaced objects; too loose a
  threshold leaves duplicate boxes — tune per dataset, don't trust the
  torchvision default blindly.

## References
- [torchvision: Object Detection reference](https://pytorch.org/vision/stable/models.html#object-detection) — pretrained detection/segmentation model zoo and fine-tuning API.
- [Albumentations documentation](https://albumentations.ai/docs/) — bbox/mask-aware augmentation pipelines.
- [torchmetrics: Detection](https://lightning.ai/docs/torchmetrics/stable/detection/mean_average_precision.html) — correct mAP computation for object detection.
