# Public Cat training split

This offline preparation step derives a one-class YOLO dataset from the immutable public Open Images Cat development collection. It does not train a model, change the server, or measure household-camera accuracy. The source collection currently contains 291 verified positive JPEGs and 318 Cat boxes. Its `partial` status means 291 of the requested 300 were collected; all 291 images are individually checked before derivation. There are no inspected negative images, so this dataset alone cannot measure false-alert behavior.

Keep the original `manifest.json`, `inventory.json`, source CSVs, and JPEGs unchanged outside Git. Use a **new** private output directory for every derivation:

```sh
python -m scripts.prepare_cat_training_dataset \
  --manifest /private/public-cat/run/manifest.json \
  --inventory /private/public-cat/run/inventory.json \
  --output /private/public-cat/yolo-seed42-v1
```

The CLI verifies matching manifest/inventory image sets, normalized positive boxes, image SHA-256, JPEG and resolved EXIF orientation, and attribution provenance before creating output. Missing inventory, duplicate paths or hashes, unreviewed/empty boxes, hash mismatches, unsafe paths, existing output, and output inside Git are rejected. The new directories use mode `0700`; images, labels, and metadata use `0600`. Source images are copied byte-for-byte and checked again after copying. The input manifest and inventory hashes are checked again before success.

If copying or verification fails after output creation, the private directory may contain incomplete artifacts without a final `split-manifest.json`. Treat it as failed evidence and retry with a different new output path; never treat partial files as a completed dataset.

Images are grouped by connected author, image hash, and source URL identity so a linked group never crosses train, validation, and test. Seed `42` fixes group order; deterministic assignment targets 80/10/10 by image count while preserving groups and requiring all three splits to be nonempty. The `split-manifest.json` records source hashes, seed, per-image hashes/splits, group assignments, Cat box count, and achieved counts. `dataset.yaml` is JSON-valid YAML with class `0: cat`. Labels contain every Cat box as normalized YOLO `class center_x center_y width height` coordinates. The same frozen derived dataset must be used for a reported training run; do not reshuffle after inspecting results.

The public split is a **development** dataset. Reserve a separate, labeled household-camera held-out set with day/night and cat-absent scenes before claiming deployment accuracy. Preserve source license and attribution records in the original inventory and verify image rights before redistribution. Training environment, checkpoint, model artifact SHA, validation/test metrics, and any threshold decisions belong in a separate run record outside Git.
