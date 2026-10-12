# Public Cat-negative development data

`scripts/collect_public_cat_negatives.py` gathers up to 150 public development negatives from the official Open Images V7 [validation and test human image labels](https://storage.googleapis.com/openimages/web/download_v7.html). It requires an immutable run of the [positive collector](public-cat-dataset.md) as `--positive-run`. An image qualifies only when Cat `/m/01yrx` has a human `verification` or `crowdsource-verification` label with `Confidence=0`. Any conflicting Cat image label or Cat bbox excludes it; missing boxes alone never establish absence. A private spot review should still check that selected images do not visibly contain cats.

Validation alone provides too few candidates: preflight found 66 human Cat-negative IDs, 53 after license, rotation, and positive-author filters. The approved test addition uses [test image labels](https://storage.googleapis.com/openimages/v7/oidv7-test-annotations-human-imagelabels.csv), [test metadata](https://storage.googleapis.com/openimages/2018_04/test/test-images-with-rotation.csv), and [test boxes](https://storage.googleapis.com/openimages/v5/test-annotations-bbox.csv). The tool reuses the positive run's verified validation metadata and boxes without modifying them. New source CSV downloads total about 244 MB, capped per file at 30/100/50/80 MB; JPEGs are capped at 150 images, 2 MB each and 200 MB total. Requests share an 1800-second collection deadline. It runs as a standalone POSIX CLI on the main thread because the shared fetch helper uses `SIGALRM` for wall-clock HTTP timeouts.

Each selected image has a distinct author/profile group, original URL, landing URL, and SHA-256 from every selected negative and from the prior positives. A metadata CC BY 2.0/4.0 license, author, landing URL, and zero rotation are required. The source label, split, image license/attribution, URLs, and file hashes are stored in `inventory.json`. This metadata is not independent rights verification; check originals before reuse or redistribution. No raw image or CSV belongs in Git.

Run from the repository root with a fresh private output path:

```sh
python scripts/collect_public_cat_negatives.py \
  --positive-run "$HOME/.local/share/esp32cam-training-data/open-images-v7-validation-cat/run-20261012-0155" \
  --output-dir "$HOME/.local/share/esp32cam-training-data/open-images-v7-cat-negative/run-001" \
  --max-images 150 --max-image-bytes 2000000 --max-total-bytes 200000000 \
  --timeout 30 --retries 1 --deadline-seconds 1800
```

The output must be outside Git, including worktrees. Files and directories are exclusive-created at 600/700 permissions. If a source download fails before `inventory.json` exists, the directory is an incomplete attempt, not a collected dataset; inspect it and retry at a fresh path. `manifest.json` is the existing evaluator's version 1 format with explicit `boxes: []`, emitted only if at least one verified negative succeeds. `inventory.json` declares `complete` only when all requested images succeed; otherwise it records `partial` and skip reasons. Local roles are fixed before any model result: 100 `train`, 25 `val`, 25 `test` in ascending SHA-256 order of `42:{source_split}:{ImageID}`, with author groups isolated across roles. The selection seed `42` is recorded. `source_split` is preserved separately: an Open Images test image used in local training is **not** an official held-out test sample. Never reassign local roles after seeing model results. These public negatives do not replace reviewed room, dog, human, day, and night images from the target cameras.
