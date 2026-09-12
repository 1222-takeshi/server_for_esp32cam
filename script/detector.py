from __future__ import annotations

import os
from dataclasses import dataclass
from io import BytesIO
from pathlib import Path


@dataclass(frozen=True, slots=True)
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
        num_threads: int | None = None,
    ) -> None:
        try:
            import numpy as np
            from PIL import Image, ImageOps
        except ImportError as exc:  # pragma: no cover - depends on optional deps
            raise RuntimeError("Install the ml extra to use TFLite detection") from exc

        Interpreter = _load_interpreter_class()

        self._np = np
        self._image = Image
        self._image_ops = ImageOps
        self.threshold = threshold
        self.person_label = person_label
        self.person_class_id = person_class_id
        # Every supported Interpreter (tflite_runtime, ai_edge_litert,
        # tensorflow.lite) defaults num_threads to a conservative,
        # effectively single-threaded value when it's omitted. Deployments
        # target multi-core CPUs (e.g. Raspberry Pi 4), so default to using
        # every available core instead of leaving them idle.
        resolved_num_threads = (
            num_threads if num_threads and num_threads > 0 else (os.cpu_count() or 4)
        )
        self._interpreter = Interpreter(
            model_path=str(model_path), num_threads=resolved_num_threads
        )
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
        image.draft("RGB", (width, height))
        image = self._image_ops.exif_transpose(image).convert("RGB").resize((width, height))
        array = self._np.asarray(image)

        dtype = input_detail["dtype"]
        if dtype == self._np.float32:
            # `.astype(float32)` already allocates a fresh array, so dividing
            # it out-of-place (`/ 255.0`) allocated and populated a *second*
            # full-size float32 array on every single inference call, for
            # the lifetime of the process. `/=` reuses the array `.astype()`
            # just allocated instead, halving the allocations/copies for
            # this step -- same result, no behavior change.
            array = array.astype(self._np.float32)
            array /= 255.0
        else:
            array = array.astype(dtype)

        return self._np.expand_dims(array, axis=0)

    def _output_role_indices(self) -> dict[str, int]:
        """Classify each output tensor's role (boxes/classes/scores/count) by
        name, cached after the first call.

        `self._output_details` (and each entry's `"name"`) is fixed once the
        model is loaded and never changes between `detect()` calls, so
        re-parsing every output name on every single inference was redundant
        work done on every uploaded image, for the lifetime of the process.
        Classifying by name is only done once here, lazily, and the
        resulting name -> tensor index mapping is reused; `_output_map()`
        still fetches fresh tensors (via `get_tensor()`) from that mapping
        on every call, since tensor contents change after each `invoke()`.
        """
        cached = getattr(self, "_output_role_indices_cache", None)
        if cached is not None:
            return cached

        roles: dict[str, int] = {}
        for detail in self._output_details:
            name = detail["name"].lower()
            if "box" in name:
                roles["boxes"] = detail["index"]
            elif "class" in name:
                roles["classes"] = detail["index"]
            elif "score" in name:
                roles["scores"] = detail["index"]
            elif "count" in name:
                roles["count"] = detail["index"]
        self._output_role_indices_cache = roles
        return roles

    def _output_map(self) -> dict[str, object]:
        roles = self._output_role_indices()
        if roles:
            return {role: self._interpreter.get_tensor(index) for role, index in roles.items()}

        output_map: dict[str, object] = {}
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
        # Fast path: numpy arrays (the normal TFLite output) expose `.shape`
        # directly, so this is the only branch exercised in production.
        shape = getattr(tensor, "shape", None)
        if shape is not None:
            return tuple(int(dim) for dim in shape)

        # Fallback for genuinely non-ndarray inputs (nested list/tuple
        # tensors, e.g. from lightweight test doubles). Walk iteratively
        # instead of recursing per dimension to avoid Python-level call
        # overhead on each `_shape_of` invocation.
        dims: list[int] = []
        node = tensor
        while isinstance(node, (list, tuple)):
            if not node:
                dims.append(0)
                return tuple(dims)
            dims.append(len(node))
            node = node[0]
        return tuple(dims)

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

        # Not lock-protected: InferenceService (script/inference.py) funnels
        # every detect() call -- from either the async worker loop or a
        # "sync"-mode request greenlet -- through a single-thread
        # gevent.threadpool.ThreadPool, so exactly one caller ever executes
        # this section at a time in the current architecture, and never
        # concurrently with itself. A threading.Lock here would be unused
        # scaffolding rather than an active safeguard (see #261). Re-add one
        # (protecting per-instance state correctly) if concurrent inference
        # is ever introduced (e.g. a pool size > 1).
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
