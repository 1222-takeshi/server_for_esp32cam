import hashlib
import json
import sys
from types import SimpleNamespace

import pytest

from script.detector import Detection
from scripts import evaluate_native_cat as native


@pytest.fixture
def data(tmp_path, monkeypatch):
    model = tmp_path / "model.onnx"
    model.write_bytes(b"frozen-model")
    labels = tmp_path / "labels.txt"
    labels.write_text("\n".join(["other"] * 15 + ["cat"] + ["other"] * 64))
    rows = []
    for name, image, boxes in [("positive.jpg", b"two", [[.1, .1, .3, .3], [.6, .6, .8, .8]]),
                               ("negative.jpg", b"clear", [])]:
        (tmp_path / name).write_bytes(image)
        rows.append({"path": name, "sha256": hashlib.sha256(image).hexdigest(), "boxes": boxes})
    manifest = tmp_path / "manifest.json"
    manifest.write_text(json.dumps({"version": 1, "samples": rows}))
    monkeypatch.setitem(sys.modules, "onnxruntime", SimpleNamespace(__version__="test-runtime"))

    class Detector:
        NMS_IOU = .7

        def __init__(self, path, **kwargs):
            assert path == model
            assert kwargs == {"threshold": .5, "num_threads": 3, "input_size": 320,
                              "allow_spinning": False}

        def detect(self, image):
            if image == b"clear":
                return []
            return [Detection("cat", .9, .1, .1, .3, .3), Detection("cat", .8, .6, .6, .8, .8)]

    monkeypatch.setattr(native, "OnnxYoloDetector", Detector)
    return manifest, model, labels, hashlib.sha256(model.read_bytes()).hexdigest()


def test_native_profile_reuses_image_and_object_metrics_without_accepting_field(data):
    result = native.evaluate_native(*data)
    assert result["images"] == {"positive": 1, "negative": 1}
    assert result["image_level"] == {"tp": 1, "fp": 0, "fn": 0, "tn": 1, "precision": 1, "recall": 1}
    assert result["object_level"] == {"tp": 2, "fp": 0, "fn": 0, "precision": 1, "recall": 1}
    assert result["config"]["class_id"] == 15
    assert result["config"]["providers"] == ["CPUExecutionProvider"]
    assert result["config"]["nms_iou"] == .7
    assert result["runtime_versions"]["onnxruntime"] == "test-runtime"
    assert result["field_accuracy_accepted"] is False


@pytest.mark.parametrize("digest", ["", "0" * 64, "G" * 64, None])
def test_wrong_or_invalid_model_digest_is_rejected_before_inference(data, monkeypatch, digest):
    monkeypatch.setattr(native, "OnnxYoloDetector", lambda *a, **k: pytest.fail("must not initialize"))
    with pytest.raises(ValueError):
        native.evaluate_native(*data[:3], digest)


def test_manifest_and_labels_cannot_be_reinterpreted_to_make_results_pass(data):
    manifest, model, labels, digest = data
    labels.write_text("person\ncat\n")
    with pytest.raises(ValueError):
        native.evaluate_native(manifest, model, labels, digest)


def test_negative_only_data_keeps_recall_unevaluated(data):
    manifest = data[0]
    payload = json.loads(manifest.read_text())
    payload["samples"] = payload["samples"][1:]
    manifest.write_text(json.dumps(payload))
    result = native.evaluate_native(*data)
    assert result["images"] == {"positive": 0, "negative": 1}
    assert result["image_level"]["recall"] is None
    assert result["object_level"]["precision"] is None


@pytest.mark.parametrize("count", [79, 81])
def test_wrong_class_count_is_rejected(data, count):
    data[2].write_text("\n".join(["other"] * 15 + ["cat"] + ["other"] * (count - 16)))
    with pytest.raises(ValueError, match="80-class"):
        native.evaluate_native(*data)


def test_model_swap_between_plan_check_and_evaluator_is_rejected(data, monkeypatch):
    evaluate = native.evaluate

    def swapped(*args, **kwargs):
        data[1].write_bytes(b"another-model")
        return evaluate(*args, **kwargs)

    monkeypatch.setattr(native, "evaluate", swapped)
    with pytest.raises(ValueError, match="before detector initialization"):
        native.evaluate_native(*data)


def test_mutation_during_inference_preserves_source_failure(data, monkeypatch):
    class Mutating:
        def __init__(self, *args, **kwargs):
            pass

        def detect(self, image):
            data[1].write_bytes(b"changed")
            return []

    monkeypatch.setattr(native, "OnnxYoloDetector", Mutating)
    with pytest.raises(ValueError, match="changed during evaluation"):
        native.evaluate_native(*data)


def test_cli_writes_once_and_preserves_inputs(data, tmp_path):
    manifest, model, labels, digest = data
    output = tmp_path / "result.json"
    args = ["--manifest", str(manifest), "--model", str(model), "--labels", str(labels),
            "--expected-model-sha256", digest, "--output", str(output)]
    assert native.main(args) == 0
    original = output.read_bytes()
    with pytest.raises(SystemExit):
        native.main(args)
    assert output.read_bytes() == original
    assert model.read_bytes() == b"frozen-model"
