"""Offline cat metrics using the fixed Pi native ONNX inference settings."""

from __future__ import annotations

import argparse
import importlib
from pathlib import Path
import re
import sys

import PIL

from script.onnx_detector import OnnxYoloDetector
from scripts.evaluate_pet_detection import evaluate, sha256
from scripts.summarize_operating_audit import write_output


def evaluate_native(manifest: Path, model: Path, labels: Path, expected_model_sha256: str):
    if (not isinstance(expected_model_sha256, str)
            or re.fullmatch(r"[0-9a-f]{64}", expected_model_sha256) is None):
        raise ValueError("Expected model SHA256 must be a lowercase 64-digit digest")
    if sha256(model) != expected_model_sha256:
        raise ValueError("Model does not match the frozen evaluation plan")
    if len([line for line in labels.read_text().splitlines() if line.strip()]) != 80:
        raise ValueError("Expected the fixed 80-class label mapping")

    def factory(**kwargs):
        if sha256(model) != expected_model_sha256:
            raise ValueError("Model changed before detector initialization")
        return OnnxYoloDetector(kwargs["model_path"], threshold=kwargs["threshold"],
                                num_threads=kwargs["num_threads"], input_size=320,
                                allow_spinning=False)

    result = evaluate(manifest, model, labels, label="cat", class_id=15, threshold=.5,
                      iou_threshold=.5, num_threads=3, detector_factory=factory)
    if result["model_sha256"] != expected_model_sha256:
        raise ValueError("Evaluated model differs from the frozen plan")
    result["config"].update(backend="onnx_yolo11n_cpu", input_size=320, allow_spinning=False,
                            providers=["CPUExecutionProvider"], nms_iou=OnnxYoloDetector.NMS_IOU)
    result["runtime_versions"] = {
        "onnxruntime": importlib.import_module("onnxruntime").__version__,
        "pillow": PIL.__version__,
        "numpy": importlib.import_module("numpy").__version__,
    }
    result["expected_model_sha256"] = expected_model_sha256
    result["field_accuracy_accepted"] = False
    result["interpretation"] = (
        "Image/object metrics only. Day/night session coverage, temporal misses, false alerts "
        "per day, power and acceptance thresholds require a separate frozen evaluation plan."
    )
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--model", type=Path, required=True)
    parser.add_argument("--labels", type=Path, required=True)
    parser.add_argument("--expected-model-sha256", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        result = evaluate_native(args.manifest, args.model, args.labels, args.expected_model_sha256)
        write_output(args.output, result)
    except (OSError, ValueError, RuntimeError, ImportError) as exc:
        parser.error(str(exc))
    return 0


if __name__ == "__main__":
    sys.exit(main())
