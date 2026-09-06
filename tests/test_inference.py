from __future__ import annotations

from datetime import datetime, timezone
import time

import gevent

from script.detector import Detection, DetectionSkipped
from script.inference import InferenceService
from script.metadata import SnapshotRecord


def _snapshot(snapshot_id: int = 1, camera_id: str | None = "cam1") -> SnapshotRecord:
    return SnapshotRecord(
        id=snapshot_id,
        filename="snap.jpg",
        relative_path=f"{camera_id}/images/snap.jpg",
        size=123,
        camera_id=camera_id,
        captured_at=None,
        stored_at=datetime.now(timezone.utc),
        inference_status="pending",
        inference_error=None,
    )


class FakeMetadataStore:
    def __init__(self, snapshot: SnapshotRecord) -> None:
        self._snapshot = snapshot
        self.processing_calls: list[int] = []
        self.result_calls: list[tuple[int, list[Detection], str | None]] = []
        self.skipped_calls: list[tuple[int, str]] = []

    def get_snapshot(self, snapshot_id: int, include_detections: bool = False):
        return self._snapshot if snapshot_id == self._snapshot.id else None

    def mark_snapshot_processing(self, snapshot_id: int) -> None:
        self.processing_calls.append(snapshot_id)

    def mark_snapshot_result(self, snapshot_id: int, *, detections, error=None) -> None:
        self.result_calls.append((snapshot_id, detections, error))

    def mark_snapshot_skipped(self, snapshot_id: int, *, reason: str) -> None:
        self.skipped_calls.append((snapshot_id, reason))


class FakeStorage:
    def __init__(self, image_bytes: bytes = b"jpeg-bytes") -> None:
        self.image_bytes = image_bytes

    def read_image_by_relative_path(self, relative_path: str) -> bytes:
        return self.image_bytes


class SlowDetector:
    """Stands in for TFLiteCpuDetector's interpreter.invoke(): a call that
    blocks for a while doing native (GIL-releasing) work, not Python
    bytecode -- time.sleep releases the GIL the same way."""

    def __init__(self, delay: float = 0.3) -> None:
        self.delay = delay
        self.call_count = 0

    def detect(self, image_bytes: bytes) -> list[Detection]:
        self.call_count += 1
        time.sleep(self.delay)
        return [Detection(label="person", score=0.9, x1=0.1, y1=0.1, x2=0.5, y2=0.5)]


class RaisingDetector:
    def __init__(self, exc: Exception) -> None:
        self.exc = exc

    def detect(self, image_bytes: bytes) -> list[Detection]:
        raise self.exc


def _make_service(detector, metadata_store=None, storage=None) -> tuple[InferenceService, FakeMetadataStore]:
    snapshot = _snapshot()
    store = metadata_store or FakeMetadataStore(snapshot)
    service = InferenceService(
        metadata_store=store,
        storage=storage or FakeStorage(),
        detector=detector,
    )
    return service, store


def test_process_snapshot_dispatches_detect_off_the_calling_greenlet():
    # Regression test for #244/#259: detect() previously ran inline on
    # whichever greenlet called process_snapshot() (the async worker
    # greenlet, or the request greenlet in "sync" mode), so a slow,
    # non-yielding call (like TFLite's invoke()) froze every other
    # greenlet -- uploads, dashboard, /healthz -- for its whole duration.
    detector = SlowDetector(delay=0.3)
    service, store = _make_service(detector)
    snapshot = store._snapshot

    other_ticks: list[float] = []

    def other_greenlet() -> None:
        for _ in range(6):
            other_ticks.append(time.monotonic())
            gevent.sleep(0.05)

    g = gevent.spawn(other_greenlet)
    start = time.monotonic()
    service.process_snapshot(snapshot.id)
    g.join()

    assert detector.call_count == 1
    # If detect() still ran inline, it would occupy the only OS thread for
    # the full 0.3s and no tick could land inside that window.
    assert any(start < t < start + detector.delay for t in other_ticks), other_ticks


def test_process_snapshot_still_records_successful_detections():
    detector = SlowDetector(delay=0.01)
    service, store = _make_service(detector)
    snapshot = store._snapshot

    service.process_snapshot(snapshot.id)

    assert store.processing_calls == [snapshot.id]
    assert len(store.result_calls) == 1
    result_id, detections, error = store.result_calls[0]
    assert result_id == snapshot.id
    assert error is None
    assert [d.label for d in detections] == ["person"]


def test_process_snapshot_propagates_detection_skipped_through_the_pool():
    detector = RaisingDetector(DetectionSkipped("disabled"))
    service, store = _make_service(detector)
    snapshot = store._snapshot

    service.process_snapshot(snapshot.id)

    assert store.skipped_calls == [(snapshot.id, "disabled")]
    assert store.result_calls == []


def test_process_snapshot_records_error_when_detect_raises_through_the_pool():
    detector = RaisingDetector(RuntimeError("boom"))
    service, store = _make_service(detector)
    snapshot = store._snapshot

    service.process_snapshot(snapshot.id)

    assert len(store.result_calls) == 1
    result_id, detections, error = store.result_calls[0]
    assert result_id == snapshot.id
    assert detections == []
    assert error == "boom"


def test_stop_kills_the_detect_pool_without_hanging():
    detector = SlowDetector(delay=0.01)
    service, store = _make_service(detector)
    # Prime the pool so it actually has a live worker thread to kill.
    service.process_snapshot(store._snapshot.id)
    assert service._detect_pool.size == 1

    service.stop()

    # kill() terminates the idle worker thread immediately rather than
    # leaving it running past the service's own shutdown; ThreadPool
    # lazily respawns workers on demand, so this isn't a permanent
    # poison -- just confirms stop() actually tore the thread down instead
    # of leaking it.
    assert service._detect_pool.size == 0
