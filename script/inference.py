from __future__ import annotations

from queue import Empty, Queue
import threading
from typing import Protocol

from script.detector import Detection, DetectionSkipped
from script.metadata import SQLiteMetadataStore
from script.storage import Storage
from script.tracking import ObjectTracker
from script.webhook import DISABLED_NOTIFIER


class DetectorLike(Protocol):
    def detect(self, image_bytes: bytes):
        ...


class InferenceService:
    def __init__(
        self,
        *,
        metadata_store: SQLiteMetadataStore,
        storage: Storage,
        detector: DetectorLike,
        mode: str = "async",
        notifier=DISABLED_NOTIFIER,
        keep_only_detected_snapshots: bool = False,
        tracker: ObjectTracker | None = None,
    ) -> None:
        self.metadata_store = metadata_store
        self.storage = storage
        self.detector = detector
        self.mode = mode
        self._notifier = notifier
        self.keep_only_detected_snapshots = keep_only_detected_snapshots
        self.tracker = tracker
        self._queue: Queue[int] = Queue()
        self._stop_event = threading.Event()
        self._worker: threading.Thread | None = None

    def start(self) -> None:
        if self.mode != "async":
            return
        if self._worker is not None:
            return

        self.metadata_store.reset_processing_snapshots()
        # Startup-only recovery sweep, not a hot path: re-queue every
        # snapshot left pending/processing from a prior run, not just the
        # oldest 100 (anything beyond that cap was previously orphaned
        # forever, since nothing re-scans for pending work after boot).
        for snapshot_id in self.metadata_store.list_pending_snapshot_ids(limit=None):
            self._queue.put(snapshot_id)

        self._worker = threading.Thread(target=self._worker_loop, daemon=True, name="esp32cam-inference")
        self._worker.start()

    def stop(self) -> None:
        self._stop_event.set()
        if self._worker is not None:
            self._worker.join(timeout=1.0)
            self._worker = None

    def enqueue_snapshot(self, snapshot_id: int) -> None:
        if self.mode == "sync":
            self.process_snapshot(snapshot_id)
            return
        self._queue.put(snapshot_id)

    def queue_length(self) -> int:
        return self._queue.qsize()

    def worker_is_alive(self) -> bool:
        return self._worker is not None and self._worker.is_alive()

    def _worker_loop(self) -> None:
        while not self._stop_event.is_set():
            try:
                snapshot_id = self._queue.get(timeout=0.2)
            except Empty:
                continue
            try:
                self.process_snapshot(snapshot_id)
            finally:
                self._queue.task_done()

    def process_snapshot(self, snapshot_id: int) -> None:
        snapshot = self.metadata_store.get_snapshot(snapshot_id)
        if snapshot is None:
            return

        self.metadata_store.mark_snapshot_processing(snapshot_id)

        try:
            image_bytes = self.storage.read_image_by_relative_path(snapshot.relative_path)
            detections = self.detector.detect(image_bytes)
        except DetectionSkipped as exc:
            self._update_tracking(snapshot.camera_id, [])
            self.metadata_store.mark_snapshot_skipped(snapshot_id, reason=str(exc))
            return
        except Exception as exc:  # pragma: no cover - exercised via route behavior
            self._update_tracking(snapshot.camera_id, [])
            self.metadata_store.mark_snapshot_result(snapshot_id, detections=[], error=str(exc))
            return

        detections = self._update_tracking(snapshot.camera_id, detections)
        self.metadata_store.mark_snapshot_result(snapshot_id, detections=detections)
        if detections:
            self._notifier.notify(snapshot, detections)
        elif self.keep_only_detected_snapshots:
            self._discard_undetected_snapshot(snapshot.relative_path)

    def _update_tracking(
        self, camera_id: str | None, detections: list[Detection]
    ) -> list[Detection]:
        if self.tracker is None or camera_id is None:
            return detections
        return self.tracker.update(camera_id, detections)

    def _discard_undetected_snapshot(self, relative_path: str) -> None:
        try:
            self.storage.delete_image_by_relative_path(relative_path)
        except FileNotFoundError:
            pass
        except Exception:
            return
        self.metadata_store.delete_snapshots_by_relative_paths([relative_path])
