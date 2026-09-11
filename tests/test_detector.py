from __future__ import annotations

from io import BytesIO

from PIL import Image, ImageDraw, ImageOps
from PIL.JpegImagePlugin import JpegImageFile

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


class FakeArray:
    def __init__(self, shape: tuple[int, ...], dtype: object, pixels=()):
        self.shape = shape
        self.dtype = dtype
        self.pixels = pixels

    def astype(self, dtype: object):
        return FakeArray(self.shape, dtype, self.pixels)


class FakeNumpy:
    uint8 = object()
    float32 = object()

    @staticmethod
    def asarray(image: Image.Image):
        return FakeArray(
            (image.height, image.width, 3),
            FakeNumpy.uint8,
            tuple(image.get_flattened_data()),
        )

    @staticmethod
    def expand_dims(array: FakeArray, axis: int):
        assert axis == 0
        return FakeArray((1, *array.shape), array.dtype, array.pixels)


def test_tflite_detector_normalizes_standard_ssd_output_shapes():
    detector = TFLiteCpuDetector.__new__(TFLiteCpuDetector)
    detector.threshold = 0.5
    detector.person_label = "person"
    detector.person_class_id = 0
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


def test_detect_does_not_require_or_create_a_lock():
    # Regression guard for #261: detect() previously serialized
    # set_tensor()/invoke()/_output_map() behind a threading.Lock that never
    # saw contention (the async pipeline funnels every call through a single
    # worker thread, and "sync" mode runs inline on one cooperatively
    # scheduled gevent greenlet). The lock was removed as dead scaffolding;
    # this asserts detect() works on an instance that never had a `_lock`
    # attribute, and that it doesn't lazily create one.
    detector = TFLiteCpuDetector.__new__(TFLiteCpuDetector)
    detector.threshold = 0.5
    detector.person_label = "person"
    detector.person_class_id = 0
    detector._input_details = [{"index": 0}]
    detector._output_details = [
        {"index": 1, "name": "boxes"},
        {"index": 2, "name": "classes"},
        {"index": 3, "name": "scores"},
        {"index": 4, "name": "count"},
    ]
    detector._labels = ["person"]
    detector._prepare_input = lambda _image_bytes: "prepared-input"
    detector._interpreter = FakeInterpreter(
        {
            1: [[[0.10, 0.20, 0.30, 0.40]]],
            2: [[0]],
            3: [[0.95]],
            4: [1],
        }
    )

    assert not hasattr(detector, "_lock")
    detections = detector.detect(b"jpeg-bytes")

    assert len(detections) == 1
    assert not hasattr(detector, "_lock")


def test_output_map_caches_role_classification_after_first_call():
    detector = TFLiteCpuDetector.__new__(TFLiteCpuDetector)
    detector._output_details = [
        {"index": 1, "name": "boxes"},
        {"index": 2, "name": "classes"},
        {"index": 3, "name": "scores"},
        {"index": 4, "name": "count"},
    ]
    detector._interpreter = FakeInterpreter(
        {1: "boxes-tensor", 2: "classes-tensor", 3: "scores-tensor", 4: "count-tensor"}
    )

    first = detector._output_map()
    assert first == {
        "boxes": "boxes-tensor",
        "classes": "classes-tensor",
        "scores": "scores-tensor",
        "count": "count-tensor",
    }
    assert detector._output_role_indices_cache == {
        "boxes": 1,
        "classes": 2,
        "scores": 3,
        "count": 4,
    }

    # Mutating output_details afterward must not change subsequent behavior --
    # confirms the name -> role classification runs once and is cached,
    # rather than being re-derived from _output_details on every
    # _output_map() call (the tensors themselves are still re-fetched each
    # call, since their contents change after every invoke()).
    detector._output_details = []
    detector._interpreter = FakeInterpreter(
        {1: "boxes-tensor-2", 2: "classes-tensor-2", 3: "scores-tensor-2", 4: "count-tensor-2"}
    )
    second = detector._output_map()
    assert second == {
        "boxes": "boxes-tensor-2",
        "classes": "classes-tensor-2",
        "scores": "scores-tensor-2",
        "count": "count-tensor-2",
    }


def test_output_map_falls_back_to_rank_when_names_are_unrecognized():
    detector = TFLiteCpuDetector.__new__(TFLiteCpuDetector)
    detector._output_details = [
        {"index": 1, "name": "StatefulPartitionedCall:0"},
        {"index": 2, "name": "StatefulPartitionedCall:1"},
        {"index": 3, "name": "StatefulPartitionedCall:2"},
        {"index": 4, "name": "StatefulPartitionedCall:3"},
    ]
    detector._interpreter = FakeInterpreter(
        {
            1: [[[0.1, 0.2, 0.3, 0.4]]],
            2: [[0]],
            3: [[0.9]],
            4: [1],
        }
    )

    output_map = detector._output_map()

    assert output_map["boxes"] == [[[0.1, 0.2, 0.3, 0.4]]]
    assert output_map["classes"] == [[0]]
    assert output_map["scores"] == [[0.9]]
    assert output_map["count"] == [1]


def test_prepare_input_requests_jpeg_draft_before_resizing(monkeypatch):
    detector = TFLiteCpuDetector.__new__(TFLiteCpuDetector)
    detector._input_details = [
        {"shape": (1, 300, 300, 3), "dtype": FakeNumpy.uint8},
    ]
    detector._image = Image
    detector._image_ops = ImageOps
    detector._np = FakeNumpy

    source = Image.new("RGB", (1600, 1200), color=(12, 34, 56))
    payload = BytesIO()
    source.save(payload, format="JPEG")

    draft_calls: list[tuple[str, tuple[int, int]]] = []
    original_draft = JpegImageFile.draft

    def spy_draft(image, mode, size):
        draft_calls.append((mode, size))
        return original_draft(image, mode, size)

    monkeypatch.setattr(JpegImageFile, "draft", spy_draft)

    prepared = detector._prepare_input(payload.getvalue())

    assert draft_calls == [("RGB", (300, 300))]
    assert prepared.shape == (1, 300, 300, 3)
    assert prepared.dtype is FakeNumpy.uint8


def test_prepare_input_preserves_exif_orientation_with_draft(monkeypatch):
    detector = TFLiteCpuDetector.__new__(TFLiteCpuDetector)
    detector._input_details = [
        {"shape": (1, 300, 300, 3), "dtype": FakeNumpy.uint8},
    ]
    detector._image = Image
    detector._image_ops = ImageOps
    detector._np = FakeNumpy

    source = Image.new("RGB", (800, 600))
    draw = ImageDraw.Draw(source)
    draw.rectangle((0, 0, 399, 299), fill=(220, 30, 30))
    draw.rectangle((400, 0, 799, 299), fill=(30, 220, 30))
    draw.rectangle((0, 300, 399, 599), fill=(30, 30, 220))
    draw.rectangle((400, 300, 799, 599), fill=(220, 220, 30))
    exif = Image.Exif()
    exif[274] = 6
    payload = BytesIO()
    source.save(payload, format="JPEG", exif=exif, quality=100)

    orientation_calls: list[int | None] = []
    original_exif_transpose = ImageOps.exif_transpose

    def spy_exif_transpose(image):
        orientation_calls.append(image.getexif().get(274))
        return original_exif_transpose(image)

    monkeypatch.setattr(ImageOps, "exif_transpose", spy_exif_transpose)

    prepared = detector._prepare_input(payload.getvalue())
    expected = original_exif_transpose(Image.open(BytesIO(payload.getvalue())))
    expected = tuple(expected.convert("RGB").resize((300, 300)).get_flattened_data())
    unrotated = tuple(
        Image.open(BytesIO(payload.getvalue()))
        .convert("RGB")
        .resize((300, 300))
        .get_flattened_data()
    )

    def pixel_distance(left, right):
        return max(abs(a - b) for a, b in zip(left, right))

    assert orientation_calls == [6]
    assert prepared.shape == (1, 300, 300, 3)
    assert pixel_distance(prepared.pixels[0], expected[0]) <= 20
    assert pixel_distance(prepared.pixels[0], unrotated[0]) >= 100


def test_shape_of_uses_fast_path_for_ndarray_like_objects():
    array = FakeArray((1, 10, 4), FakeNumpy.float32)

    assert TFLiteCpuDetector._shape_of(array) == (1, 10, 4)


def test_shape_of_falls_back_correctly_for_nested_lists_and_tuples():
    assert TFLiteCpuDetector._shape_of([[1, 2, 3, 4], [5, 6, 7, 8]]) == (2, 4)
    assert TFLiteCpuDetector._shape_of(((1, 2), (3, 4), (5, 6))) == (3, 2)
    assert TFLiteCpuDetector._shape_of([]) == (0,)
    assert TFLiteCpuDetector._shape_of(0.5) == ()


def _fake_module(name: str, **attrs):
    import types

    module = types.ModuleType(name)
    for key, value in attrs.items():
        setattr(module, key, value)
    return module


def test_interpreter_loader_falls_back_to_ai_edge_litert(monkeypatch):
    from script.detector import _load_interpreter_class

    class LitertInterpreter:
        pass

    monkeypatch.setitem(__import__("sys").modules, "tflite_runtime", None)
    monkeypatch.setitem(__import__("sys").modules, "tflite_runtime.interpreter", None)
    monkeypatch.setitem(__import__("sys").modules, "ai_edge_litert", _fake_module("ai_edge_litert"))
    monkeypatch.setitem(
        __import__("sys").modules,
        "ai_edge_litert.interpreter",
        _fake_module("ai_edge_litert.interpreter", Interpreter=LitertInterpreter),
    )

    assert _load_interpreter_class() is LitertInterpreter


def test_interpreter_loader_prefers_tflite_runtime(monkeypatch):
    from script.detector import _load_interpreter_class

    class RuntimeInterpreter:
        pass

    class LitertInterpreter:
        pass

    monkeypatch.setitem(__import__("sys").modules, "tflite_runtime", _fake_module("tflite_runtime"))
    monkeypatch.setitem(
        __import__("sys").modules,
        "tflite_runtime.interpreter",
        _fake_module("tflite_runtime.interpreter", Interpreter=RuntimeInterpreter),
    )
    monkeypatch.setitem(
        __import__("sys").modules,
        "ai_edge_litert.interpreter",
        _fake_module("ai_edge_litert.interpreter", Interpreter=LitertInterpreter),
    )

    assert _load_interpreter_class() is RuntimeInterpreter


def _install_recording_interpreter(monkeypatch, captured: dict[str, object]) -> None:
    class RecordingInterpreter:
        def __init__(self, *, model_path, num_threads=None):
            captured["model_path"] = model_path
            captured["num_threads"] = num_threads

        def allocate_tensors(self):
            pass

        def get_input_details(self):
            return []

        def get_output_details(self):
            return []

    monkeypatch.setitem(__import__("sys").modules, "tflite_runtime", _fake_module("tflite_runtime"))
    monkeypatch.setitem(
        __import__("sys").modules,
        "tflite_runtime.interpreter",
        _fake_module("tflite_runtime.interpreter", Interpreter=RecordingInterpreter),
    )


def test_tflite_detector_passes_explicit_num_threads_to_interpreter(monkeypatch, tmp_path):
    captured: dict[str, object] = {}
    _install_recording_interpreter(monkeypatch, captured)
    model_path = tmp_path / "model.tflite"
    model_path.write_bytes(b"")

    TFLiteCpuDetector(model_path=model_path, num_threads=2)

    assert captured["num_threads"] == 2


def test_tflite_detector_defaults_num_threads_to_cpu_count_when_unset(monkeypatch, tmp_path):
    captured: dict[str, object] = {}
    _install_recording_interpreter(monkeypatch, captured)
    monkeypatch.setattr("script.detector.os.cpu_count", lambda: 4)
    model_path = tmp_path / "model.tflite"
    model_path.write_bytes(b"")

    TFLiteCpuDetector(model_path=model_path)

    assert captured["num_threads"] == 4


def test_tflite_detector_falls_back_to_four_threads_when_cpu_count_unknown(monkeypatch, tmp_path):
    captured: dict[str, object] = {}
    _install_recording_interpreter(monkeypatch, captured)
    monkeypatch.setattr("script.detector.os.cpu_count", lambda: None)
    model_path = tmp_path / "model.tflite"
    model_path.write_bytes(b"")

    TFLiteCpuDetector(model_path=model_path)

    assert captured["num_threads"] == 4
