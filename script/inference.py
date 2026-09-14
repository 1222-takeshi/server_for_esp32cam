from __future__ import annotations

import threading
from queue import Empty, Queue
from typing import Protocol

from gevent.threadpool import ThreadPool

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
        # detector.detect() (TFLiteCpuDetector.detect() -> interpreter.invoke())
        # is a long native call that doesn't yield to gevent's event loop --
        # run inline on this service's calling greenlet (the async worker
        # loop below, or the request greenlet itself in "sync" mode), it
        # freezes every other greenlet (uploads, dashboard, /healthz) for the
        # call's whole duration (see #244, #259). Dispatching it through a
        # single real OS thread keeps detect() calls serialized -- matching
        # the "exactly one caller at a time" invariant detector.py already
        # relies on -- while letting the gevent hub keep servicing other
        # greenlets while that thread is busy.
        self._detect_pool = ThreadPool(1)

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
        # iter_pending_snapshot_ids() keyset-paginates the underlying SELECT
        # in bounded batches instead of one unbounded fetchall(), so this
        # sweep's memory footprint stays bounded even if the backlog has
        # grown very large (e.g. the server was down for a long time, or a
        # bug left items stuck in "processing") (#407).
        for snapshot_id in self.metadata_store.iter_pending_snapshot_ids():
            self._queue.put(snapshot_id)

        self._worker = threading.Thread(target=self._worker_loop, daemon=True, name="esp32cam-inference")
        self._worker.start()

    def stop(self) -> None:
        self._stop_event.set()
        if self._worker is not None:
            self._worker.join(timeout=1.0)
            # Only kill the pool if the worker actually finished within
            # the join timeout above. In "sync" mode, script/app.py's
            # teardown_appcontext calls stop() after *every* request --
            # self._worker is never set for that mode, so this whole
            # branch (and the pool) is untouched there, same as before
            # detect() moved onto this pool.
            #
            # ThreadPool.kill() blocks the calling greenlet until the
            # pool's worker thread goes idle, with no timeout of its own.
            # If the worker loop's last process_snapshot() call is still
            # running past the 1s join() above (a single slow frame, or a
            # detector that hangs), that same in-flight detect() call is
            # still occupying the pool's one thread -- calling kill() here
            # regardless would trade the bounded join() timeout for an
            # unbounded wait on that call to finish. Skipping it in that
            # case leaves the pool's thread to exit on its own once the
            # call completes; a leaked idle ThreadPool worker thread does
            # not block process exit.
            worker_stopped_in_time = not self._worker.is_alive()
            self._worker = None
            if worker_stopped_in_time:
                self._detect_pool.kill()

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
        # include_detections=False: this snapshot was just enqueued and has
        # not been through mark_snapshot_result()/mark_snapshot_skipped() yet,
        # so it can't have any detection rows -- and only .relative_path /
        # .camera_id from the returned record are used below. Fetching
        # detections here is a wasted query on every single inference run
        # (see script/metadata.py's get_snapshot()).
        snapshot = self.metadata_store.get_snapshot(snapshot_id, include_detections=False)
        if snapshot is None:
            return

        self.metadata_store.mark_snapshot_processing(snapshot_id)

        try:
            image_bytes = self.storage.read_image_by_relative_path(snapshot.relative_path)
            detections = self._detect_pool.apply(self.detector.detect, (image_bytes,))
        except DetectionSkipped as exc:
            self._update_tracking(snapshot.camera_id, [])
            self.metadata_store.mark_snapshot_skipped(snapshot_id, reason=str(exc))
            return
        except Exception as exc:  # noqa: BLE001 - pragma: no cover - exercised via route behavior
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
        except Exception:  # noqa: BLE001 - leave the index row if the file delete failed
            return
        self.metadata_store.delete_snapshots_by_relative_paths([relative_path])
