# Public Cat development data

`scripts/collect_public_cat_dataset.py` collects Cat-positive images from the
[Open Images V7 validation or test split](https://storage.googleapis.com/openimages/web/download_v7.html).
The default remains validation, 300 requested images, and 200 MB total; explicit bounds are 1000 images, 2 MB per JPEG, and 1 GB total.
Its three official CSV inputs are the [boxable classes](https://storage.googleapis.com/openimages/v7/oidv7-class-descriptions-boxable.csv),
[validation boxes](https://storage.googleapis.com/openimages/v5/validation-annotations-bbox.csv), and
[validation image metadata](https://storage.googleapis.com/openimages/2018_04/validation/validation-images-with-rotation.csv)
(about 40 MB combined). Individual JPEGs come from the [official public S3 bucket](https://github.com/openimages/dataset/blob/master/downloader.py).
For test mode it uses the official test box annotations and test image metadata instead. The tool verifies `/m/01yrx` is Cat in the class CSV, then takes Cat boxes and image IDs in sorted order.

This is a **public development set**, useful for testing the evaluator and diagnosing model behavior. It is not the household-camera held-out set; overlap with a pretrained model's training data is unknown. No Cat-negative images are emitted, so this run cannot estimate false-positive rate or image-level precision. The later household set needs reviewed Cat-present and Cat-absent images across day and night, including the target Scottish Fold and box annotations for every visible cat. Keep that set separate from public development data and model selection.

The [dataset description](https://storage.googleapis.com/openimages/web/factsfigures_v7.html) says annotations are CC BY 4.0 and lists images as CC BY 2.0, but warns that it does not warrant individual image rights. The collector retains each image's metadata license, author, title, landing URL, source URL, and SHA-256. It accepts only declared CC BY 2.0/4.0 licenses with attribution fields; missing or other licenses are skipped and counted. This is metadata evidence, not an independent rights verification. Check original rights before reuse or redistribution.

Run from the repo root after review. The output path must be new and outside Git; each file is exclusive-created, with private directory/file permissions (700/600). A run never resumes or overwrites a prior one. Choose a fresh path for a retry:

```sh
python scripts/collect_public_cat_dataset.py \
  --output-dir "$HOME/.local/share/esp32cam-training-data/open-images-v7-validation-cat/run-001" \
  --max-images 300 --max-image-bytes 2000000 --max-total-bytes 200000000 \
  --timeout 20 --retries 1 --deadline-seconds 1800
```

To expand the positive development set while protecting all previous positive and negative images, use the official test split and pass every immutable prior run, including any newly collected negative run:

```sh
python scripts/collect_public_cat_dataset.py \
  --source-split test \
  --exclude-run /private/public-cat/old-positive-run \
  --exclude-run /private/public-cat/old-negative-run \
  --exclude-run /private/public-cat/new-negative-run \
  --output-dir /private/public-cat/new-positive-run \
  --max-images 600 --max-image-bytes 2000000 --max-total-bytes 1000000000 \
  --timeout 20 --retries 1 --deadline-seconds 7200
```

Each exclusion run must contain a matching `inventory.json`, `manifest.json`, and unchanged source JPEGs. The collector verifies their hashes before creating output and again before writing final metadata. For early positive runs that omitted author profile and original image URL in inventory, it verifies the recorded SHA of `source/images.csv` and recovers those identities. Existing image IDs, image hashes, author names/profiles, landing URLs, source URLs, and original URLs are excluded; image hashes are checked again after download. The inventory records prior manifest/inventory hashes and excluded counts so the selection can be audited. Run exclusions prevent known source overlap; they do not prove independence from any unseen public or household data.

`source/*.csv` preserves the source bytes; `inventory.json` records their URLs and hashes, image provenance (including author profile and original image URL), per-image hashes, per-box `IsOccluded`/`IsTruncated` source flags, skip counts, and `complete` or `partial` status. `manifest.json` contains normalized `[xmin, ymin, xmax, ymax]` Cat boxes in the existing evaluator's version 1 format. It is emitted only when at least one image succeeds. A `partial` run stays partial even if some images are usable; do not report the requested count unless `collected` matches it. The run caps image count and bytes, applies a wall-clock deadline to each request and the overall collection, and excludes an **entire image** if any of its Cat boxes has `IsGroupOf`, `IsDepiction`, or `IsInside` set. Images with unresolved metadata rotation or JPEG EXIF orientation are excluded so source boxes remain aligned. JPEG decode failures are counted and skipped.

The resulting manifest can be passed to `scripts/evaluate_pet_detection.py` with an explicitly selected local model and labels. Record that model and threshold unchanged; do not use the public set to silently tune a deployment threshold. The household held-out evaluation remains required before claiming Cat precision or recall in the actual cameras.
