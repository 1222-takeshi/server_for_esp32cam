import io
import sys
import types
import unittest
from unittest.mock import patch

import numpy as np
from PIL import Image, UnidentifiedImageError

from script.detector import Detection
from script.onnx_detector import OnnxYoloDetector


class Node:
    def __init__(self, name, shape, kind="tensor(float)"):
        self.name, self.shape, self.type = name, shape, kind


class Session:
    output = None
    metadata = "{15: 'cat'}"
    input_shape = ["batch", 3, "height", "width"]
    output_shape = ["batch", 84, "anchors"]

    def __init__(self, path, sess_options, providers):
        self.path, self.options, self.providers = path, sess_options, providers
        self.received = None

    def get_inputs(self):
        return [Node("images", self.input_shape)]

    def get_outputs(self):
        return [Node("output0", self.output_shape)]

    def get_modelmeta(self):
        metadata = {} if self.metadata is None else {"names": self.metadata}
        return types.SimpleNamespace(custom_metadata_map=metadata)

    def run(self, output_names, input_feed):
        self.received = input_feed["images"]
        return [self.output if self.output is not None else np.zeros((1, 84, 2100), np.float32)]


class OnnxDetectorTests(unittest.TestCase):
    def setUp(self):
        Session.output = None
        Session.metadata = "{15: 'cat'}"
        Session.input_shape = ["batch", 3, "height", "width"]
        Session.output_shape = ["batch", 84, "anchors"]
        options = type("Options", (), {})
        ort = types.SimpleNamespace(
            SessionOptions=options,
            InferenceSession=Session,
            ExecutionMode=types.SimpleNamespace(ORT_SEQUENTIAL="sequential"),
        )
        self.patch = patch.dict(sys.modules, {"onnxruntime": ort})
        self.patch.start()
        self.addCleanup(self.patch.stop)

    def detector(self, **kwargs):
        return OnnxYoloDetector("test.onnx", **kwargs)

    @staticmethod
    def jpeg(width=160, height=80, exif_orientation=None):
        image = Image.new("RGB", (width, height), (255, 0, 0))
        output = io.BytesIO()
        exif = Image.Exif()
        if exif_orientation:
            exif[274] = exif_orientation
        image.save(output, "JPEG", exif=exif)
        return output.getvalue()

    @staticmethod
    def output(*rows):
        raw = np.zeros((1, 84, 2100), np.float32)
        for i, (cls, score, box) in enumerate(rows):
            raw[0, :4, i] = box
            raw[0, 4 + cls, i] = score
        return raw

    def test_session_options_and_letterbox_input(self):
        detector = self.detector(num_threads=3)
        Session.output = self.output()
        self.assertEqual(detector._session.providers, ["CPUExecutionProvider"])
        self.assertEqual(detector._session.options.intra_op_num_threads, 3)
        self.assertEqual(detector._session.options.inter_op_num_threads, 1)
        self.assertEqual(detector._session.options.execution_mode, "sequential")
        self.assertEqual(detector.detect(self.jpeg()), [])
        tensor = detector._session.received
        self.assertEqual(tensor.shape, (1, 3, 320, 320))
        self.assertEqual(tensor.dtype, np.float32)
        self.assertTrue(tensor.flags.c_contiguous)
        self.assertAlmostEqual(tensor[0, 0, 0, 0], 114 / 255)
        self.assertGreater(tensor[0, 0, 100, 100], 0.95)

    def test_cat_box_is_deletterboxed_and_normalized(self):
        # Original 160x80 -> 320x160 at y=80. Network xywh=(160,160,160,80).
        Session.output = self.output((15, 0.9, (160, 160, 160, 80)))
        found = self.detector().detect(self.jpeg())
        self.assertEqual(len(found), 1)
        self.assertIsInstance(found[0], Detection)
        self.assertEqual(
            (found[0].label, found[0].x1, found[0].y1, found[0].x2, found[0].y2),
            ("cat", 0.25, 0.25, 0.75, 0.75),
        )
        self.assertAlmostEqual(found[0].score, 0.9)

    def test_exif_orientation_and_clipping(self):
        # Orientation 6 makes the source 80x160; padding is horizontal.
        Session.output = self.output((15, 0.8, (160, 160, 240, 160)))
        found = self.detector().detect(self.jpeg(exif_orientation=6))
        self.assertEqual(len(found), 1)
        self.assertEqual((found[0].x1, found[0].y1, found[0].x2, found[0].y2), (0, 0.25, 1, 0.75))

    def test_extremely_wide_valid_jpeg_keeps_nonzero_resize_height(self):
        detector = self.detector()
        Session.output = self.output()
        self.assertEqual(detector.detect(self.jpeg(width=4096, height=1)), [])
        self.assertEqual(detector._session.received.shape, (1, 3, 320, 320))

    def test_threshold_class_and_nms_before_clipping(self):
        Session.output = self.output(
            (15, 0.9, (160, 160, 160, 80)),
            (15, 0.8, (160, 160, 160, 80)),
            (16, 0.99, (160, 160, 160, 80)),
            (15, 0.49, (100, 100, 20, 20)),
        )
        found = self.detector().detect(self.jpeg())
        self.assertEqual(len(found), 1)
        self.assertAlmostEqual(found[0].score, 0.9)

    def test_nms_uses_network_boxes_before_clipping(self):
        # Network IoU is 70/90 (>0.7); clipped IoU is only 20/30 (<0.7).
        Session.output = self.output(
            (15, 0.9, (330, 160, 80, 40)),
            (15, 0.8, (340, 160, 80, 40)),
        )
        self.assertEqual(len(self.detector().detect(self.jpeg())), 1)

    def test_absent_class_metadata_is_allowed(self):
        Session.metadata = None
        detector = self.detector(threshold=1.0)
        Session.output = self.output((15, 1.0, (160, 160, 160, 80)))
        self.assertEqual(len(detector.detect(self.jpeg())), 1)

    def test_invalid_settings_or_model_are_rejected(self):
        for threshold in (-0.1, 1.1, float("nan"), float("inf")):
            with self.subTest(threshold=threshold), self.assertRaises(ValueError):
                self.detector(threshold=threshold)
        for threads in (0, -1, 257, 2.5):
            with self.subTest(threads=threads), self.assertRaises(ValueError):
                self.detector(num_threads=threads)
        for size in (640, 320.0, True, "320"):
            with self.subTest(size=size), self.assertRaises(ValueError):
                self.detector(input_size=size)
        Session.metadata = "{15: 'dog'}"
        with self.assertRaises(ValueError):
            self.detector()
        Session.metadata = "{15: 'cat'}"
        Session.input_shape = ["batch", 1, "height", "width"]
        with self.assertRaises(ValueError):
            self.detector()
        Session.input_shape = ["batch", 3, "height", "width"]
        Session.output_shape = ["batch", 85, "anchors"]
        with self.assertRaises(ValueError):
            self.detector()

    def test_invalid_output_and_image_are_rejected(self):
        detector = self.detector()
        for raw in (
            np.zeros((1, 85, 2100), np.float32),
            np.full((1, 84, 2100), np.nan, np.float32),
            self.output((15, 0.9, (160, 160, -1, 80))),
        ):
            with self.subTest(shape=raw.shape), self.assertRaises(ValueError):
                Session.output = raw
                detector.detect(self.jpeg())
        with self.assertRaises(UnidentifiedImageError):
            detector.detect(b"not a jpeg")
