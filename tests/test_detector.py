from __future__ import annotations

import threading

from script.detector import TFLiteCpuDetector


class FakeInterpreter:
    def __init__(self, tensors: dict[int, object]):
        self._tensors = tensors
        self.set_calls: list[tuple[int, object]] = []
        self.invoked = False

    def set_tensor(self, index: int, value: object) -> None:
        self.set_calls.append((index, value))

    def invoke(self) -> None:
        self.invoked = True

    def get_tensor(self, index: int) -> object:
        return self._tensors[index]


def test_tflite_detector_normalizes_standard_ssd_output_shapes():
    detector = TFLiteCpuDetector.__new__(TFLiteCpuDetector)
    detector.threshold = 0.5
    detector.person_label = "person"
    detector.person_class_id = 0
    detector._lock = threading.Lock()
    detector._input_details = [{"index": 0}]
    detector._output_details = [
        {"index": 1, "name": "boxes"},
        {"index": 2, "name": "classes"},
        {"index": 3, "name": "scores"},
        {"index": 4, "name": "count"},
    ]
    detector._labels = ["person", "cat"]
    detector._prepare_input = lambda _image_bytes: "prepared-input"
    detector._interpreter = FakeInterpreter(
        {
            1: [[[0.10, 0.20, 0.30, 0.40], [0.00, 0.00, 0.00, 0.00]]],
            2: [[0, 1]],
            3: [[0.95, 0.20]],
            4: [2],
        }
    )

    detections = detector.detect(b"jpeg-bytes")

    assert detector._interpreter.invoked is True
    assert detector._interpreter.set_calls == [(0, "prepared-input")]
    assert len(detections) == 1
    assert detections[0].label == "person"
    assert detections[0].score == 0.95
    assert detections[0].x1 == 0.20
    assert detections[0].y1 == 0.10
    assert detections[0].x2 == 0.40
    assert detections[0].y2 == 0.30
