from __future__ import annotations

from datetime import datetime, timezone
import time

import gevent
import pytest

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

    def reset_processing_snapshots(self) -> None:
        pass

    def list_pending_snapshot_ids(self, limit: int | None = None) -> list[int]:
        return []

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


@pytest.fixture
def make_service():
    """Builds an InferenceService wired to fakes, and always tears down its
    detect pool afterward -- process_snapshot() spins up a real ThreadPool
    worker thread regardless of whether the service's own start()/stop()
    lifecycle (which most of these tests bypass to call process_snapshot()
    directly, the same way "sync" mode does) ever runs."""

    services: list[InferenceService] = []

    def _factory(detector, metadata_store=None, storage=None):
        snapshot = _snapshot()
        store = metadata_store or FakeMetadataStore(snapshot)
        service = InferenceService(
            metadata_store=store,
            storage=storage or FakeStorage(),
            detector=detector,
        )
        services.append(service)
        return service, store

    yield _factory

    for service in services:
        service._detect_pool.kill()


def test_process_snapshot_dispatches_detect_off_the_calling_greenlet(make_service):
    # Regression test for #244/#259: detect() previously ran inline on
    # whichever greenlet called process_snapshot() (the async worker
    # greenlet, or the request greenlet in "sync" mode), so a slow,
    # non-yielding call (like TFLite's invoke()) froze every other
    # greenlet -- uploads, dashboard, /healthz -- for its whole duration.
    detector = SlowDetector(delay=0.3)
    service, store = make_service(detector)
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


def test_process_snapshot_still_records_successful_detections(make_service):
    detector = SlowDetector(delay=0.01)
    service, store = make_service(detector)
    snapshot = store._snapshot

    service.process_snapshot(snapshot.id)

    assert store.processing_calls == [snapshot.id]
    assert len(store.result_calls) == 1
    result_id, detections, error = store.result_calls[0]
    assert result_id == snapshot.id
    assert error is None
    assert [d.label for d in detections] == ["person"]


def test_process_snapshot_propagates_detection_skipped_through_the_pool(make_service):
    detector = RaisingDetector(DetectionSkipped("disabled"))
    service, store = make_service(detector)
    snapshot = store._snapshot

    service.process_snapshot(snapshot.id)

    assert store.skipped_calls == [(snapshot.id, "disabled")]
    assert store.result_calls == []


def test_process_snapshot_records_error_when_detect_raises_through_the_pool(make_service):
    detector = RaisingDetector(RuntimeError("boom"))
    service, store = make_service(detector)
    snapshot = store._snapshot

    service.process_snapshot(snapshot.id)

    assert len(store.result_calls) == 1
    result_id, detections, error = store.result_calls[0]
    assert result_id == snapshot.id
    assert detections == []
    assert error == "boom"


def test_stop_without_a_running_worker_does_not_touch_the_detect_pool(make_service):
    # Regression test: script/app.py's teardown_appcontext calls
    # inference.stop() after *every* request whenever INFERENCE_MODE !=
    # "async" (the mode conftest.py runs the whole suite under) --
    # start() never runs for that mode, so self._worker stays None. Before
    # this fix, stop() killed the shared _detect_pool unconditionally,
    # which -- since the pool is shared across every concurrent request --
    # meant one request's teardown could block on a completely unrelated
    # request's in-flight detect() call, with no timeout.
    detector = SlowDetector(delay=0.01)
    service, store = make_service(detector)
    service.process_snapshot(store._snapshot.id)
    assert service._detect_pool.size == 1

    service.stop()

    assert service._detect_pool.size == 1


def test_stop_does_not_block_on_an_unrelated_in_flight_detect_call(make_service):
    # Directly reproduces the hang this fix addresses: a slow detect()
    # dispatched from one greenlet must not make a concurrent stop() call
    # wait for it.
    detector = SlowDetector(delay=1.0)
    service, store = make_service(detector)

    in_flight = gevent.spawn(service.process_snapshot, store._snapshot.id)
    gevent.sleep(0.1)  # let the pool pick up the in-flight call

    start = time.monotonic()
    service.stop()
    elapsed = time.monotonic() - start

    assert elapsed < 0.5, elapsed
    in_flight.join()


def test_stop_kills_the_detect_pool_after_the_async_worker_stops(make_service):
    detector = SlowDetector(delay=0.01)
    service, store = make_service(detector)
    service.start()
    service.enqueue_snapshot(store._snapshot.id)

    deadline = time.monotonic() + 2.0
    while detector.call_count == 0 and time.monotonic() < deadline:
        time.sleep(0.01)
    assert detector.call_count == 1

    service.stop()

    assert service._detect_pool.size == 0
