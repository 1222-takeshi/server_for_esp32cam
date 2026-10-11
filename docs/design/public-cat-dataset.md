# Public Cat development data

`scripts/collect_public_cat_dataset.py` collects at most 300 Cat-positive images from the
[Open Images V7 validation split](https://storage.googleapis.com/openimages/web/download_v7.html).
Its three official CSV inputs are the [boxable classes](https://storage.googleapis.com/openimages/v7/oidv7-class-descriptions-boxable.csv),
[validation boxes](https://storage.googleapis.com/openimages/v5/validation-annotations-bbox.csv), and
[validation image metadata](https://storage.googleapis.com/openimages/2018_04/validation/validation-images-with-rotation.csv)
(about 40 MB combined). Individual JPEGs come from the [official public S3 bucket](https://github.com/openimages/dataset/blob/master/downloader.py).
The tool verifies `/m/01yrx` is Cat in the class CSV, then takes Cat boxes and image IDs in sorted order.

This is a **public development set**, useful for testing the evaluator and diagnosing model behavior. It is not the household-camera held-out set; overlap with a pretrained model's training data is unknown. No Cat-negative images are emitted, so this run cannot estimate false-positive rate or image-level precision. The later household set needs reviewed Cat-present and Cat-absent images across day and night, including the target Scottish Fold and box annotations for every visible cat. Keep that set separate from public development data and model selection.

The [dataset description](https://storage.googleapis.com/openimages/web/factsfigures_v7.html) says annotations are CC BY 4.0 and lists images as CC BY 2.0, but warns that it does not warrant individual image rights. The collector retains each image's metadata license, author, title, landing URL, source URL, and SHA-256. It accepts only declared CC BY 2.0/4.0 licenses with attribution fields; missing or other licenses are skipped and counted. This is metadata evidence, not an independent rights verification. Check original rights before reuse or redistribution.

Run from the repo root after review. The output path must be new and outside Git; each file is exclusive-created, with private directory/file permissions (700/600). A run never resumes or overwrites a prior one. Choose a fresh path for a retry:

```sh
python scripts/collect_public_cat_dataset.py \
  --output-dir "$HOME/.local/share/esp32cam-training-data/open-images-v7-validation-cat/run-001" \
  --max-images 300 --max-image-bytes 2000000 --max-total-bytes 200000000 \
  --timeout 20 --retries 1 --deadline-seconds 1800
```

`source/*.csv` preserves the source bytes; `inventory.json` records their URLs and hashes, image provenance, per-image hashes, per-box `IsOccluded`/`IsTruncated` source flags, skip counts, and `complete` or `partial` status. `manifest.json` contains normalized `[xmin, ymin, xmax, ymax]` Cat boxes in the existing evaluator's version 1 format. It is emitted only when at least one image succeeds. A `partial` run stays partial even if some images are usable; do not report 300 images unless `collected` is 300. The run caps image count and bytes, applies a wall-clock deadline to each request and the overall collection, and excludes an **entire image** if any of its Cat boxes has `IsGroupOf`, `IsDepiction`, or `IsInside` set. Images with unresolved rotation are excluded so source boxes remain aligned. JPEG decode failures are counted and skipped.

The resulting manifest can be passed to `scripts/evaluate_pet_detection.py` with an explicitly selected local model and labels. Record that model and threshold unchanged; do not use the public set to silently tune a deployment threshold. The household held-out evaluation remains required before claiming Cat precision or recall in the actual cameras.
