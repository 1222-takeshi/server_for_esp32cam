"""Cat detection for the official 80-class YOLO11n FP32 ONNX export.

The model computes an unfiltered [1, 84, 2100] head for a 320px input.
Call detect() through a dedicated single-thread gevent pool (as in
InferenceService): JPEG decode, ONNX Runtime, and NMS are blocking native/CPU work.
"""

from __future__ import annotations

import ast
import math
from io import BytesIO
from pathlib import Path

from script.detector import Detection


class OnnxYoloDetector:
    CAT_CLASS = 15
    NMS_IOU = 0.7
    MAX_DETECTIONS = 300  # Match the frozen pi-benchmark-20261014/bench.py reference.

    def __init__(
        self,
        model_path: str | Path,
        threshold: float = 0.5,
        num_threads: int = 4,
        input_size: int = 320,
    ) -> None:
        if isinstance(threshold, bool) or not isinstance(threshold, (int, float)):
            raise ValueError("threshold must be a finite number in [0, 1]")
        if not math.isfinite(threshold) or not 0 <= threshold <= 1:
            raise ValueError("threshold must be a finite number in [0, 1]")
        if (
            isinstance(num_threads, bool)
            or not isinstance(num_threads, int)
            or not 1 <= num_threads <= 256
        ):
            raise ValueError("num_threads must be an integer in [1, 256]")
        if isinstance(input_size, bool) or not isinstance(input_size, int) or input_size != 320:
            raise ValueError("input_size must be the integer 320")

        try:
            import numpy as np
            import onnxruntime as ort
            from PIL import Image, ImageOps
        except ImportError as exc:
            raise RuntimeError("Install the onnx extra to use ONNX detection") from exc

        self._np = np
        self._image = Image
        self._image_ops = ImageOps
        self.threshold = float(threshold)
        self.input_size = input_size
        options = ort.SessionOptions()
        options.intra_op_num_threads = num_threads
        options.inter_op_num_threads = 1
        options.execution_mode = ort.ExecutionMode.ORT_SEQUENTIAL
        self._session = ort.InferenceSession(
            str(model_path), sess_options=options, providers=["CPUExecutionProvider"]
        )
        inputs, outputs = self._session.get_inputs(), self._session.get_outputs()
        if len(inputs) != 1 or len(outputs) != 1:
            raise ValueError("ONNX model must have exactly one input and one output")
        input_node, output_node = inputs[0], outputs[0]
        if (
            input_node.type != "tensor(float)"
            or len(input_node.shape) != 4
            or not self._allows(input_node.shape[0], 1)
            or input_node.shape[1] != 3
            or not self._allows(input_node.shape[2], input_size)
            or not self._allows(input_node.shape[3], input_size)
        ):
            raise ValueError("expected FP32 NCHW [1, 3, 320, 320] ONNX input")
        if (
            output_node.type != "tensor(float)"
            or len(output_node.shape) != 3
            or not self._allows(output_node.shape[0], 1)
            or not self._allows(output_node.shape[1], 84)
            or not self._allows(output_node.shape[2], 2100)
        ):
            raise ValueError("expected FP32 [1, 84, 2100] YOLO11 output")
        self._input_name = input_node.name
        self._output_name = output_node.name

        metadata = self._session.get_modelmeta().custom_metadata_map or {}
        if "names" in metadata:
            try:
                names = ast.literal_eval(metadata["names"])
                cat = (
                    names[self.CAT_CLASS]
                    if not isinstance(names, dict)
                    else names.get(self.CAT_CLASS, names.get(str(self.CAT_CLASS)))
                )
            except (ValueError, SyntaxError, TypeError, KeyError, IndexError) as exc:
                raise ValueError("invalid ONNX class metadata") from exc
            if cat != "cat":
                raise ValueError("ONNX class 15 must be cat")

    @staticmethod
    def _allows(actual: object, expected: int) -> bool:
        return actual == expected or actual is None or isinstance(actual, str)

    def _prepare_input(self, image_bytes: bytes):
        image = self._image_ops.exif_transpose(self._image.open(BytesIO(image_bytes))).convert(
            "RGB"
        )
        width, height = image.size
        scale = min(self.input_size / width, self.input_size / height)
        resized_width = max(1, round(width * scale))
        resized_height = max(1, round(height * scale))
        image = image.resize((resized_width, resized_height), self._image.Resampling.BILINEAR)
        left = (self.input_size - resized_width) // 2
        top = (self.input_size - resized_height) // 2
        canvas = self._image.new("RGB", (self.input_size, self.input_size), (114, 114, 114))
        canvas.paste(image, (left, top))
        tensor = self._np.ascontiguousarray(
            self._np.asarray(canvas).transpose(2, 0, 1)[None].astype(self._np.float32) / 255.0
        )
        return tensor, (width, height, scale, left, top)

    def detect(self, image_bytes: bytes) -> list[Detection]:
        tensor, (width, height, scale, left, top) = self._prepare_input(image_bytes)
        raw = self._session.run([self._output_name], {self._input_name: tensor})[0]
        np = self._np
        if not isinstance(raw, np.ndarray) or raw.shape != (1, 84, 2100) or raw.dtype != np.float32:
            raise ValueError("expected FP32 [1, 84, 2100] YOLO11 output")
        if not np.isfinite(raw).all():
            raise ValueError("ONNX output contains NaN or infinity")

        # Match the frozen reference: best class per anchor, score cutoff, then
        # class-aware NMS on the original network boxes before deletterboxing.
        pred = raw[0].T
        boxes = pred[:, :4]
        scores = pred[:, 4:]
        classes = np.argmax(scores, axis=1)
        confidence = scores[np.arange(len(scores)), classes]
        keep = confidence >= self.threshold
        boxes, classes, confidence = boxes[keep], classes[keep], confidence[keep]
        if not len(boxes):
            return []
        if np.any(boxes[:, 2:] <= 0) or np.any((confidence < 0) | (confidence > 1)):
            raise ValueError("ONNX output contains invalid boxes or scores")
        xyxy = np.column_stack((boxes[:, :2] - boxes[:, 2:] / 2, boxes[:, :2] + boxes[:, 2:] / 2))
        areas = np.prod(np.maximum(xyxy[:, 2:] - xyxy[:, :2], 0), axis=1)
        order = np.argsort(-confidence)
        detections: list[Detection] = []
        kept = 0
        while len(order) and kept < self.MAX_DETECTIONS:
            index = order[0]
            kept += 1
            if classes[index] == self.CAT_CLASS:
                x1, y1, x2, y2 = xyxy[index]
                x1 = max(0.0, min(1.0, float((x1 - left) / scale / width)))
                y1 = max(0.0, min(1.0, float((y1 - top) / scale / height)))
                x2 = max(0.0, min(1.0, float((x2 - left) / scale / width)))
                y2 = max(0.0, min(1.0, float((y2 - top) / scale / height)))
                if x1 < x2 and y1 < y2:
                    detections.append(Detection("cat", float(confidence[index]), x1, y1, x2, y2))
            rest = order[1:]
            if not len(rest):
                break
            upper_left = np.maximum(xyxy[index, :2], xyxy[rest, :2])
            lower_right = np.minimum(xyxy[index, 2:], xyxy[rest, 2:])
            intersection = np.prod(np.maximum(lower_right - upper_left, 0), axis=1)
            overlap = intersection / np.maximum(areas[index] + areas[rest] - intersection, 1e-9)
            order = rest[(classes[rest] != classes[index]) | (overlap <= self.NMS_IOU)]
        return detections
