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
    track_id: int | None = None


class DetectionSkipped(RuntimeError):
    """Raised when inference is intentionally skipped, not failed."""


def _load_interpreter_class():
    """Return the first available TFLite Interpreter implementation.

    tflite-runtime has no wheels for Python >= 3.12; ai-edge-litert is its
    official successor and covers newer interpreters, so try it next before
    falling back to the full TensorFlow package.
    """
    try:
        from tflite_runtime.interpreter import Interpreter
    except ImportError:
        pass
    else:
        return Interpreter
    try:
        from ai_edge_litert.interpreter import Interpreter
    except ImportError:
        pass
    else:
        return Interpreter
    try:
        from tensorflow.lite import Interpreter  # type: ignore[attr-defined]
    except ImportError as exc:
        raise RuntimeError("No TFLite interpreter available") from exc
    return Interpreter


class DisabledDetector:
    def __init__(self, reason: str = "Detector is disabled", *, skip: bool = True) -> None:
        self.reason = reason
        self.skip = skip

    def detect(self, _image_bytes: bytes) -> list[Detection]:
        if self.skip:
            raise DetectionSkipped(self.reason)
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

        Interpreter = _load_interpreter_class()

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
            tensor = self._interpreter.get_tensor(detail["index"])
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
        by_rank = sorted(tensors, key=lambda tensor: len(self._shape_of(tensor)), reverse=True)
        output_map["boxes"] = by_rank[0]
        output_map["classes"] = by_rank[1]
        output_map["scores"] = by_rank[2]
        if len(by_rank) > 3:
            output_map["count"] = by_rank[3]
        return output_map

    @staticmethod
    def _shape_of(tensor: object) -> tuple[int, ...]:
        shape = getattr(tensor, "shape", None)
        if shape is not None:
            return tuple(int(dim) for dim in shape)
        if isinstance(tensor, (list, tuple)):
            if not tensor:
                return (0,)
            return (len(tensor),) + TFLiteCpuDetector._shape_of(tensor[0])
        return ()

    @classmethod
    def _collapse_leading_singleton_dims(cls, tensor: object):
        normalized = tensor
        shape = cls._shape_of(normalized)
        while shape and shape[0] == 1:
            normalized = normalized[0]
            shape = cls._shape_of(normalized)
        return normalized

    @classmethod
    def _normalize_box_rows(cls, tensor: object):
        normalized = cls._collapse_leading_singleton_dims(tensor)
        shape = cls._shape_of(normalized)
        if not shape:
            return [[normalized]]
        if len(shape) == 1:
            return [normalized]
        return normalized

    @classmethod
    def _normalize_values(cls, tensor: object):
        normalized = cls._collapse_leading_singleton_dims(tensor)
        if not cls._shape_of(normalized):
            return [normalized]
        return normalized

    @classmethod
    def _normalize_count(cls, tensor: object) -> int:
        normalized = cls._collapse_leading_singleton_dims(tensor)
        while isinstance(normalized, (list, tuple)):
            if not normalized:
                return 0
            normalized = normalized[0]
        if hasattr(normalized, "item"):
            return int(normalized.item())
        return int(normalized)

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

        boxes = self._normalize_box_rows(output_map["boxes"])
        classes = self._normalize_values(output_map["classes"])
        scores = self._normalize_values(output_map["scores"])
        if "count" in output_map:
            count = self._normalize_count(output_map["count"])
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
