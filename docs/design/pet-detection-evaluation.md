# Pet detection evaluation (offline gate)

This procedure evaluates the existing `TFLiteCpuDetector` against labeled field snapshots. It does not claim cat accuracy until representative images are labeled and run through the target Raspberry Pi. It does not enable cat detection, alerts, or video in the server.

## Inputs and custody

Keep the TFLite model, its plain-text labels file, camera images, manifest, and result JSON outside Git. Restrict access to images because they can show people or private spaces. Preserve originals. Create a new manifest for corrections; do not silently relabel a prior run. Use separate development and held-out evaluation sets, grouped by capture session/camera to avoid near-duplicate frames leaking across sets.

Collect consecutive snapshots from each intended camera, including cats at different positions, distances, lighting, and partial occlusions. Include empty scenes, humans, other animals, moving shadows, and likely false alarms. Record camera, capture time, and scene conditions in a separate local inventory. Freeze the evaluation set before choosing thresholds. Review annotation disagreements with a second person. The current repository contains no validated cat ground truth.

Manifest v1 is JSON beside the referenced images. `boxes` is a list of normalized `[x1,y1,x2,y2]` cat rectangles; `[]` means an inspected negative image. Multiple cats require multiple boxes. Every sample has an immutable SHA-256 digest. Coordinates must satisfy `0 <= x1 < x2 <= 1` and `0 <= y1 < y2 <= 1`. Paths are relative to the manifest directory and may not escape it. The manifest itself is hashed in the result. Example below is only the schema; its digest and path must be replaced with real data.

```json
{
  "version": 1,
  "samples": [
    {
      "path": "images/frame-001.jpg",
      "sha256": "<lowercase 64-character SHA-256 digest>",
      "boxes": [[0.1, 0.2, 0.4, 0.7]]
    }
  ]
}
```

The labels file is one label per line, zero-based after blank lines are removed. Verify its provenance against the *actual* model export documentation and inspect a known control image before interpreting class IDs. The CLI rejects a target label that does not occur exactly once at `--class-id`; it cannot prove that an unrelated labels file matches the model's output indexing. Do not assume a COCO cat ID or relabel a model to make a run pass. `TFLiteCpuDetector` uses the existing `person_label` / `person_class_id` constructor parameters as its target filter; no server configuration is changed by this offline run.

## Run

Install the project's `ml` extra in the environment used for evaluation. Use the same model, labels, target class, thread count, and detection threshold planned for deployment. The detector threshold defaults to the server's current `0.5`; the box match IoU defaults to `0.5`. Declare any proposed threshold change before examining held-out results. On the Raspberry Pi, compare the latency under deployment-like CPU load; a laptop result is not a Pi benchmark.

```sh
python -m pip install -e '.[ml]'
python scripts/evaluate_pet_detection.py \
  --manifest /private/pet-eval/manifest.json \
  --model /private/models/detect.tflite \
  --labels /private/models/labels.txt \
  --label cat --class-id <verified-id> \
  --num-threads <deployment-count> \
  --output /private/pet-eval/result.json
```

The command refuses an existing output path (including any input path) and exits without writing a result if a manifest, hash, label mapping, or inference call fails. It rechecks all input hashes after inference to detect changes during the run. Use a new output filename for each run. Results contain SHA-256 hashes for model, labels, and manifest; configuration; positive and negative image counts; image-level and object-level metrics; and per-image inference mean/p95 latency in milliseconds. Latency includes preprocessing and model inference, but excludes disk read and upload/network time. `null` precision or recall means that denominator is zero and the metric is **not evaluated**, never 100%. The image-level measure asks whether *any* cat was predicted in a frame. Object-level counts use maximum-cardinality one-to-one rectangle matching at the configured IoU (IoU only ranks candidate edges, so ambiguous overlaps do not undercount true positives); an image with two cats can have image-level TP but object-level FN. Repeated detections of one cat count as extra object-level FP. Counts are retained so the rates can be audited.

## Acceptance and next phases

### Native Pi ONNX evaluation

For the deployed YOLO11n FP32 ONNX profile, use the separate CLI below with the same
immutable manifest and verified 80-class labels (cat at zero-based index 15). It fixes
320px input, confidence 0.5, three threads, CPUExecutionProvider and disabled spinning.
The box-match IoU is 0.5; the detector's frozen NMS IoU remains 0.7. The original
TFLite CLI above keeps its interface and behavior.

```sh
python scripts/evaluate_native_cat.py \
  --manifest /private/pet-eval/manifest.json --model /private/models/yolo11n.onnx \
  --labels /private/models/coco-labels.txt \
  --expected-model-sha256 ff1013da8734fe2b8ede5f34a2124855d447fbefd248d148e9f29e7f8e28c9c4 \
  --output /private/pet-eval/native-result.json
```

Install the `onnx` extra on the target Pi. Pin the expected model digest in the
evaluation plan before running: a mismatch is rejected before inference, and all
existing model/labels/manifest/image immutability checks remain in effect. Outputs
include ONNX Runtime/Pillow versions and the fixed backend settings. They are published
atomically and never overwrite inputs or an existing result. Use separate development
and held-out sessions and keep the source images, labels and results outside Git.

`field_accuracy_accepted` stays false: metrics alone do not prove day/night coverage,
short appearances missed between eco frames, false alerts per day or watts. Freeze and
evaluate those conditions separately. A public-fixture compatibility run is not field
ground truth. Avoid a sustained offline inference load during a normal-mode 24h audit.

### Field acceptance

Before collecting held-out results, approve and record the numeric gate in a separate reviewable evaluation plan. A proposed starting gate is at least 100 positive and 100 negative held-out images per intended camera, spanning both day and night; image-level and object-level precision and recall each at least 0.90; observed false alerts at most one per camera-day; and Pi p95 inference time below half the snapshot interval. Confirm these numbers against operational needs before locking the evaluation set. Include both positive and negative images from each intended camera and operating condition. Do not accept a metric with a `null` denominator or tune thresholds on the held-out set. Investigate every high-confidence FP/FN and report confidence intervals or uncertainty alongside any claimed rate. A deployment decision needs Pi results under normal server load, stability across day/night sessions, and a rollback to the existing person-only configuration if cat alerts misbehave.

Notification integration depends on a passing detection gate, a chosen event policy (per frame versus tracked episode), deduplication, recipient consent, and observed end-to-end delivery/false-alert rates. Short-video integration additionally depends on a defined pre/post-event recording window, camera capture/transport support (including any Tapo source), retention/privacy policy, storage budget, and a test showing the clip corresponds to the triggering event. These are later-phase acceptance criteria, not features delivered by this evaluation tool.
