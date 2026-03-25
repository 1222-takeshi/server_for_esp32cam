from __future__ import annotations

from dataclasses import dataclass
from io import BytesIO
from pathlib import Path
import threading


@dataclass(frozen=True)
class Detection:
    label: str
    score: float
    x1: float
    y1: float
    x2: float
    y2: float


class DisabledDetector:
    def __init__(self, reason: str = "Detector is disabled") -> None:
        self.reason = reason

    def detect(self, _image_bytes: bytes) -> list[Detection]:
        raise RuntimeError(self.reason)


class TFLiteCpuDetector:
    def __init__(
        self,
        *,
        model_path: str | Path,
        threshold: float = 0.5,
        labels_path: str | Path | None = None,
        person_label: str = "person",
        person_class_id: int = 0,
    ) -> None:
        try:
            import numpy as np
            from PIL import Image
            from PIL import ImageOps
        except ImportError as exc:  # pragma: no cover - depends on optional deps
            raise RuntimeError("Install the ml extra to use TFLite detection") from exc

        try:
            from tflite_runtime.interpreter import Interpreter
        except ImportError:  # pragma: no cover - depends on optional deps
            try:
                from tensorflow.lite import Interpreter  # type: ignore[attr-defined]
            except ImportError as exc:
                raise RuntimeError("No TFLite interpreter available") from exc

        self._np = np
        self._image = Image
        self._image_ops = ImageOps
        self.threshold = threshold
        self.person_label = person_label
        self.person_class_id = person_class_id
        self._lock = threading.Lock()
        self._interpreter = Interpreter(model_path=str(model_path))
        self._interpreter.allocate_tensors()
        self._input_details = self._interpreter.get_input_details()
        self._output_details = self._interpreter.get_output_details()
        self._labels = self._load_labels(labels_path)

    def _load_labels(self, labels_path: str | Path | None) -> list[str]:
        if labels_path is None:
            return []

        payload = Path(labels_path).read_text(encoding="utf-8")
        return [line.strip() for line in payload.splitlines() if line.strip()]

    def _prepare_input(self, image_bytes: bytes):
        input_detail = self._input_details[0]
        _, height, width, _channels = input_detail["shape"]
        image = self._image.open(BytesIO(image_bytes))
        image = self._image_ops.exif_transpose(image).convert("RGB").resize((width, height))
        array = self._np.asarray(image)

        dtype = input_detail["dtype"]
        if dtype == self._np.float32:
            array = array.astype(self._np.float32) / 255.0
        else:
            array = array.astype(dtype)

        return self._np.expand_dims(array, axis=0)

    def _output_map(self) -> dict[str, object]:
        output_map: dict[str, object] = {}
        for detail in self._output_details:
            name = detail["name"].lower()
            tensor = self._np.squeeze(self._interpreter.get_tensor(detail["index"]))
            if "box" in name:
                output_map["boxes"] = tensor
            elif "class" in name:
                output_map["classes"] = tensor
            elif "score" in name:
                output_map["scores"] = tensor
            elif "count" in name:
                output_map["count"] = tensor

        if output_map:
            return output_map

        tensors = [self._interpreter.get_tensor(detail["index"]) for detail in self._output_details]
        by_rank = sorted(tensors, key=lambda tensor: len(tensor.shape), reverse=True)
        output_map["boxes"] = by_rank[0]
        output_map["classes"] = by_rank[1]
        output_map["scores"] = by_rank[2]
        if len(by_rank) > 3:
            output_map["count"] = by_rank[3]
        return output_map

    def _label_for_class(self, class_id: int) -> str:
        if 0 <= class_id < len(self._labels):
            return self._labels[class_id]
        if class_id == self.person_class_id:
            return self.person_label
        return str(class_id)

    def detect(self, image_bytes: bytes) -> list[Detection]:
        input_tensor = self._prepare_input(image_bytes)

        with self._lock:
            self._interpreter.set_tensor(self._input_details[0]["index"], input_tensor)
            self._interpreter.invoke()
            output_map = self._output_map()

        boxes = output_map["boxes"][0]
        classes = output_map["classes"][0]
        scores = output_map["scores"][0]
        if "count" in output_map:
            count = int(output_map["count"].flatten()[0])
        else:
            count = len(scores)

        detections: list[Detection] = []
        for index in range(min(count, len(scores))):
            score = float(scores[index])
            if score < self.threshold:
                continue

            class_id = int(classes[index])
            label = self._label_for_class(class_id)
            if label != self.person_label and class_id != self.person_class_id:
                continue

            y1, x1, y2, x2 = [float(value) for value in boxes[index]]
            detections.append(
                Detection(
                    label=self.person_label,
                    score=score,
                    x1=max(0.0, min(1.0, x1)),
                    y1=max(0.0, min(1.0, y1)),
                    x2=max(0.0, min(1.0, x2)),
                    y2=max(0.0, min(1.0, y2)),
                )
            )
        return detections
