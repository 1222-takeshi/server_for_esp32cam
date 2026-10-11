"""Offline, reproducible evaluation of the existing TFLite CPU detector."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import sys
import time
from pathlib import Path

from script.detector import TFLiteCpuDetector


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _box(value: object) -> tuple[float, float, float, float]:
    if not isinstance(value, list) or len(value) != 4:
        raise ValueError("Each box must be [x1, y1, x2, y2]")
    if any(isinstance(coord, bool) or not isinstance(coord, (int, float)) for coord in value):
        raise ValueError("Box coordinates must be numbers")
    x1, y1, x2, y2 = (float(coord) for coord in value)
    if not all(math.isfinite(coord) for coord in (x1, y1, x2, y2)):
        raise ValueError("Box coordinates must be finite")
    if not (0 <= x1 < x2 <= 1 and 0 <= y1 < y2 <= 1):
        raise ValueError("Box coordinates must be normalized and nonempty")
    return x1, y1, x2, y2


def load_manifest(path: Path) -> list[tuple[Path, str, list[tuple[float, ...]]]]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict) or set(payload) != {"version", "samples"}:
        raise ValueError("Manifest must contain only version and samples")
    if type(payload["version"]) is not int or payload["version"] != 1:
        raise ValueError("Unsupported manifest version")
    if not isinstance(payload["samples"], list) or not payload["samples"]:
        raise ValueError("Manifest samples must be a nonempty list")
    root = path.resolve().parent
    seen: set[Path] = set()
    samples = []
    for sample in payload["samples"]:
        if not isinstance(sample, dict) or set(sample) != {"path", "sha256", "boxes"}:
            raise ValueError("Each sample needs path, sha256, and boxes only")
        relative = sample["path"]
        if not isinstance(relative, str) or not relative or Path(relative).is_absolute():
            raise ValueError("Image path must be relative to the manifest")
        image = (root / relative).resolve()
        if not image.is_relative_to(root) or image in seen:
            raise ValueError("Image path escapes manifest directory or is duplicated")
        seen.add(image)
        expected_hash = sample["sha256"]
        if (
            not isinstance(expected_hash, str)
            or len(expected_hash) != 64
            or any(char not in "0123456789abcdef" for char in expected_hash)
        ):
            raise ValueError("Image sha256 must be a lowercase hex digest")
        if not isinstance(sample["boxes"], list):
            raise ValueError("boxes must be a list")
        boxes = [_box(box) for box in sample["boxes"]]
        if not image.is_file() or sha256(image) != expected_hash:
            raise ValueError(f"Image missing or SHA-256 mismatch: {relative}")
        samples.append((image, expected_hash, boxes))
    return samples


def validate_label_mapping(labels_path: Path, label: str, class_id: int) -> None:
    labels = [line.strip() for line in labels_path.read_text(encoding="utf-8").splitlines()]
    labels = [entry for entry in labels if entry]
    if not label or class_id < 0 or class_id >= len(labels) or labels[class_id] != label:
        raise ValueError("Requested label and class ID do not match the labels file")
    if labels.count(label) != 1:
        raise ValueError("Target label must occur exactly once in the labels file")


def _iou(first: tuple[float, ...], second: tuple[float, ...]) -> float:
    overlap = max(0.0, min(first[2], second[2]) - max(first[0], second[0])) * max(
        0.0, min(first[3], second[3]) - max(first[1], second[1])
    )
    first_area = (first[2] - first[0]) * (first[3] - first[1])
    second_area = (second[2] - second[0]) * (second[3] - second[1])
    return overlap / (first_area + second_area - overlap) if overlap else 0.0


def match_boxes(
    truth: list[tuple[float, ...]], predictions: list[tuple[float, ...]], iou_threshold: float
) -> int:
    candidates = sorted(
        (
            (_iou(expected, observed), truth_index, prediction_index)
            for truth_index, expected in enumerate(truth)
            for prediction_index, observed in enumerate(predictions)
        ),
        reverse=True,
    )
    used_truth: set[int] = set()
    used_predictions: set[int] = set()
    for iou, truth_index, prediction_index in candidates:
        if iou < iou_threshold:
            break
        if truth_index not in used_truth and prediction_index not in used_predictions:
            used_truth.add(truth_index)
            used_predictions.add(prediction_index)
    return len(used_truth)


def _metrics(tp: int, fp: int, fn: int) -> dict[str, int | float | None]:
    return {
        "tp": tp,
        "fp": fp,
        "fn": fn,
        "precision": tp / (tp + fp) if tp + fp else None,
        "recall": tp / (tp + fn) if tp + fn else None,
    }


def evaluate(
    manifest: Path,
    model: Path,
    labels: Path,
    *,
    label: str,
    class_id: int,
    threshold: float = 0.5,
    iou_threshold: float = 0.5,
    num_threads: int | None = None,
    detector_factory=TFLiteCpuDetector,
) -> dict[str, object]:
    if not 0 <= threshold <= 1 or not 0 < iou_threshold <= 1:
        raise ValueError("Detection threshold must be [0,1] and IoU threshold must be (0,1]")
    if num_threads is not None and num_threads < 1:
        raise ValueError("num_threads must be positive")
    validate_label_mapping(labels, label, class_id)
    samples = load_manifest(manifest)
    model_hash, labels_hash, manifest_hash = sha256(model), sha256(labels), sha256(manifest)
    effective_threads = num_threads if num_threads is not None else (os.cpu_count() or 4)
    detector = detector_factory(
        model_path=model,
        threshold=threshold,
        labels_path=labels,
        person_label=label,
        person_class_id=class_id,
        num_threads=effective_threads,
    )
    image_tp = image_fp = image_fn = image_tn = 0
    object_tp = object_fp = object_fn = 0
    latencies_ms: list[float] = []
    for image, expected_hash, truth in samples:
        image_bytes = image.read_bytes()
        if hashlib.sha256(image_bytes).hexdigest() != expected_hash:
            raise ValueError(f"Image changed during evaluation: {image}")
        started = time.perf_counter()
        detections = detector.detect(image_bytes)
        latencies_ms.append((time.perf_counter() - started) * 1000)
        predictions = [(d.x1, d.y1, d.x2, d.y2) for d in detections]
        if truth and predictions:
            image_tp += 1
        elif truth:
            image_fn += 1
        elif predictions:
            image_fp += 1
        else:
            image_tn += 1
        matched = match_boxes(truth, predictions, iou_threshold)
        object_tp += matched
        object_fp += len(predictions) - matched
        object_fn += len(truth) - matched
    if (sha256(model), sha256(labels), sha256(manifest)) != (
        model_hash, labels_hash, manifest_hash
    ):
        raise ValueError("Model, labels, or manifest changed during evaluation")
    for image, expected_hash, _ in samples:
        if sha256(image) != expected_hash:
            raise ValueError(f"Image changed during evaluation: {image}")
    ordered = sorted(latencies_ms)
    p95_index = math.ceil(0.95 * len(ordered)) - 1
    return {
        "schema_version": 1,
        "model_sha256": model_hash,
        "labels_sha256": labels_hash,
        "manifest_sha256": manifest_hash,
        "config": {
            "label": label,
            "class_id": class_id,
            "threshold": threshold,
            "iou_threshold": iou_threshold,
            "num_threads": effective_threads,
        },
        "images": {"positive": image_tp + image_fn, "negative": image_fp + image_tn},
        "image_level": {**_metrics(image_tp, image_fp, image_fn), "tn": image_tn},
        "object_level": _metrics(object_tp, object_fp, object_fn),
        "latency_ms": {"count": len(ordered), "mean": sum(ordered) / len(ordered), "p95": ordered[p95_index]},
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--model", type=Path, required=True)
    parser.add_argument("--labels", type=Path, required=True)
    parser.add_argument("--label", required=True)
    parser.add_argument("--class-id", type=int, required=True)
    parser.add_argument("--threshold", type=float, default=0.5)
    parser.add_argument("--iou-threshold", type=float, default=0.5)
    parser.add_argument("--num-threads", type=int)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        result = evaluate(
            args.manifest, args.model, args.labels, label=args.label,
            class_id=args.class_id, threshold=args.threshold,
            iou_threshold=args.iou_threshold, num_threads=args.num_threads,
        )
        with args.output.open("x", encoding="utf-8") as output:
            output.write(json.dumps(result, indent=2, sort_keys=True) + "\n")
    except (OSError, ValueError, RuntimeError, json.JSONDecodeError) as exc:
        parser.error(str(exc))
    return 0


if __name__ == "__main__":
    sys.exit(main())
