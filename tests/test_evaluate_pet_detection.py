from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from script.detector import Detection
from scripts.evaluate_pet_detection import evaluate, load_manifest, match_boxes, validate_label_mapping


class FakeDetector:
    def __init__(self, **kwargs):
        self.kwargs = kwargs

    def detect(self, image_bytes):
        if image_bytes == b"two-cats":
            return [
                Detection("cat", 0.9, 0.1, 0.1, 0.3, 0.3),
                Detection("cat", 0.8, 0.6, 0.6, 0.8, 0.8),
            ]
        if image_bytes == b"false-alarm":
            return [Detection("cat", 0.7, 0.2, 0.2, 0.4, 0.4)]
        return []


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _fixture(tmp_path: Path, rows):
    model = tmp_path / "model.tflite"
    labels = tmp_path / "labels.txt"
    manifest = tmp_path / "manifest.json"
    model.write_bytes(b"model-placeholder")
    labels.write_text("person\ncat\ndog\n", encoding="utf-8")
    samples = []
    for name, image_bytes, boxes in rows:
        (tmp_path / name).write_bytes(image_bytes)
        samples.append({"path": name, "sha256": _sha(image_bytes), "boxes": boxes})
    manifest.write_text(json.dumps({"version": 1, "samples": samples}), encoding="utf-8")
    return manifest, model, labels


def _evaluate(manifest, model, labels):
    return evaluate(
        manifest, model, labels, label="cat", class_id=1, detector_factory=FakeDetector
    )


def test_evaluation_separates_image_and_object_metrics(tmp_path):
    paths = _fixture(
        tmp_path,
        [
            ("a.jpg", b"two-cats", [[0.1, 0.1, 0.3, 0.3], [0.5, 0.5, 0.7, 0.7]]),
            ("b.jpg", b"false-alarm", []),
            ("c.jpg", b"missed-cat", [[0.1, 0.1, 0.3, 0.3]]),
            ("d.jpg", b"clear", []),
        ],
    )
    result = _evaluate(*paths)
    assert result["images"] == {"positive": 2, "negative": 2}
    assert result["image_level"] == {
        "tp": 1, "fp": 1, "fn": 1, "tn": 1, "precision": 0.5, "recall": 0.5
    }
    assert result["object_level"] == {
        "tp": 1, "fp": 2, "fn": 2, "precision": 1 / 3, "recall": 1 / 3
    }
    assert result["model_sha256"] == _sha(b"model-placeholder")
    assert result["config"]["threshold"] == 0.5
    assert result["config"]["num_threads"] >= 1
    assert result["latency_ms"]["count"] == 4


def test_zero_denominator_is_null_not_perfect_score(tmp_path):
    paths = _fixture(tmp_path, [("a.jpg", b"clear", [])])
    result = _evaluate(*paths)
    assert result["image_level"]["precision"] is None
    assert result["image_level"]["recall"] is None
    assert result["object_level"]["precision"] is None
    assert result["object_level"]["recall"] is None
    assert result["images"] == {"positive": 0, "negative": 1}


@pytest.mark.parametrize(
    "mutation",
    [
        lambda m: m.update(version=2),
        lambda m: m["samples"][0].update(path="../outside.jpg"),
        lambda m: m["samples"][0].update(sha256="0" * 64),
        lambda m: m["samples"][0].update(boxes=[[0.5, 0.5, 0.5, 1.0]]),
        lambda m: m["samples"].append(m["samples"][0].copy()),
    ],
)
def test_invalid_manifest_rejected(tmp_path, mutation):
    manifest, _, _ = _fixture(tmp_path, [("a.jpg", b"clear", [])])
    payload = json.loads(manifest.read_text(encoding="utf-8"))
    mutation(payload)
    manifest.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(ValueError):
        load_manifest(manifest)


def test_label_mapping_rejects_wrong_id_and_duplicate_label(tmp_path):
    labels = tmp_path / "labels.txt"
    labels.write_text("person\ncat\n", encoding="utf-8")
    with pytest.raises(ValueError, match="do not match"):
        validate_label_mapping(labels, "cat", 0)
    labels.write_text("cat\ncat\n", encoding="utf-8")
    with pytest.raises(ValueError, match="exactly once"):
        validate_label_mapping(labels, "cat", 1)


def test_match_boxes_is_one_to_one_and_respects_iou_threshold():
    truth = [(0.1, 0.1, 0.3, 0.3), (0.6, 0.6, 0.8, 0.8)]
    prediction = [(0.1, 0.1, 0.3, 0.3), (0.1, 0.1, 0.3, 0.3)]
    assert match_boxes(truth, prediction, 0.5) == 1
    assert match_boxes(truth, [(0.2, 0.2, 0.4, 0.4)], 0.5) == 0


def test_changed_image_fails_before_inference(tmp_path):
    paths = _fixture(tmp_path, [("a.jpg", b"clear", [])])
    manifest, model, labels = paths
    (tmp_path / "a.jpg").write_bytes(b"altered")
    with pytest.raises(ValueError, match="SHA-256 mismatch"):
        _evaluate(manifest, model, labels)

@pytest.mark.parametrize("source", ["model.tflite", "labels.txt", "manifest.json", "a.jpg"])
def test_source_changed_during_inference_rejected(tmp_path, source):
    manifest, model, labels = _fixture(tmp_path, [("a.jpg", b"clear", [])])

    class MutatingDetector(FakeDetector):
        def detect(self, image_bytes):
            target = tmp_path / source
            target.write_bytes(target.read_bytes() + b"changed")
            return super().detect(image_bytes)

    with pytest.raises(ValueError, match="changed during evaluation"):
        evaluate(
            manifest, model, labels, label="cat", class_id=1,
            detector_factory=MutatingDetector,
        )


def test_cli_refuses_existing_output_even_after_successful_evaluation(tmp_path, monkeypatch):
    from scripts import evaluate_pet_detection as cli

    manifest, model, labels = _fixture(tmp_path, [("a.jpg", b"clear", [])])
    original = model.read_bytes()
    monkeypatch.setattr(cli, "evaluate", lambda *args, **kwargs: {"ok": True})
    with pytest.raises(SystemExit) as exc:
        cli.main([
            "--manifest", str(manifest), "--model", str(model),
            "--labels", str(labels), "--label", "cat", "--class-id", "1",
            "--output", str(model),
        ])
    assert exc.value.code == 2
    assert model.read_bytes() == original


def test_match_boxes_maximizes_object_true_positives():
    # Greedy highest-IoU matching takes A-X and strands B. A-Y + B-X yields 2 TPs.
    truth = [(0.0, 0.0, 0.6, 1.0), (0.4, 0.0, 1.0, 1.0)]
    predictions = [(0.0, 0.0, 0.9, 1.0), (0.05, 0.0, 0.85, 1.0)]
    assert match_boxes(truth, predictions, 0.5) == 2


def test_manifest_hash_matches_bytes_parsed_even_if_file_changes(tmp_path, monkeypatch):
    from scripts import evaluate_pet_detection as cli

    manifest, model, labels = _fixture(tmp_path, [("a.jpg", b"clear", [])])
    original_loader = cli.load_manifest

    def changing_loader(path, *, source_bytes):
        path.write_bytes(path.read_bytes() + b" ")
        return original_loader(path, source_bytes=source_bytes)

    monkeypatch.setattr(cli, "load_manifest", changing_loader)
    with pytest.raises(ValueError, match="changed during detector initialization"):
        _evaluate(manifest, model, labels)


def test_label_file_change_during_detector_initialization_rejected(tmp_path):
    manifest, model, labels = _fixture(tmp_path, [("a.jpg", b"clear", [])])

    class ChangingDetector(FakeDetector):
        def __init__(self, **kwargs):
            super().__init__(**kwargs)
            labels.write_text("person\ndog\ncat\n", encoding="utf-8")

    with pytest.raises(ValueError, match="changed during detector initialization"):
        evaluate(
            manifest, model, labels, label="cat", class_id=1,
            detector_factory=ChangingDetector,
        )
