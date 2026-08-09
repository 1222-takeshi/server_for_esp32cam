from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timezone
import threading

from flask import current_app

from script.storage import StorageRetentionPolicy, StoredImage, StoredVideo


class RetentionScanGate:
    """Gates full retention scans to run once every `interval` calls.

    Even sourced from the SQLite index instead of a filesystem walk (see
    `_run_retention_scan()` below), a retention scan is still synchronous
    work in the request path. This gate lets callers skip most of those
    scans and only pay the cost every ``interval`` uploads, while still
    guaranteeing a scan on the very first call so retention is enforced
    immediately in the common case (default ``interval=1`` preserves the old
    every-upload behavior exactly).

    Thread-safe: a single gate instance is shared across request-handling
    threads for the lifetime of the Flask app.
    """

    def __init__(self, interval: int = 1) -> None:
        self.interval = max(1, interval)
        # Start "primed" so the first call always scans, regardless of
        # interval, matching the previous unconditional-scan behavior for
        # the first upload.
        self._count = self.interval - 1
        self._lock = threading.Lock()

    def should_scan(self) -> bool:
        if self.interval <= 1:
            return True
        with self._lock:
            self._count += 1
            if self._count >= self.interval:
                self._count = 0
                return True
            return False


def _run_retention_scan(
    *,
    policy: StorageRetentionPolicy,
    candidates: list[tuple[str, int, datetime]],
    total_bytes: int,
    delete_file: Callable[[str], int],
) -> tuple[list[str], int]:
    """Evict candidates (oldest-first) until age/max_bytes limits are met.

    ``candidates``/``total_bytes`` come from the SQLite index
    (`SQLiteMetadataStore.list_snapshot_retention_candidates()` /
    `list_video_retention_candidates()`) rather than a filesystem `rglob()`
    walk (#200 / #225 / #246 / #260); the eviction decision itself mirrors
    the loop the now-removed `LocalFilesystemStorage.apply_retention()` /
    `apply_video_retention()` used to run directly.

    The returned ``deleted`` list drives the caller's metadata-row cleanup
    (``delete_snapshots_by_relative_paths()`` / ``delete_videos_by_relative_paths()``),
    so it includes candidates chosen for eviction even when ``delete_file()``
    finds nothing on disk to remove (``FileNotFoundError``) -- an index row
    with no backing file is exactly the kind of drift this scan should
    self-heal, not leave behind for every future scan to silently re-skip.
    ``deleted_bytes`` only counts bytes actually freed from disk.
    """
    reference = datetime.now(tz=timezone.utc)
    deleted: list[str] = []
    deleted_bytes = 0

    for relative_path, size, stored_at in candidates:
        age_expired = (
            policy.max_age_seconds is not None
            and policy.max_age_seconds > 0
            and (reference - stored_at).total_seconds() > policy.max_age_seconds
        )
        over_quota = (
            policy.max_bytes is not None and policy.max_bytes > 0 and total_bytes > policy.max_bytes
        )
        if not age_expired and not over_quota:
            continue
        try:
            deleted_bytes += delete_file(relative_path)
        except FileNotFoundError:
            # The index says this candidate should be evicted, but the file
            # is already gone from disk (e.g. removed out-of-band, or left
            # behind by a prior partial failure). Nothing to delete, but the
            # SQLite row is now stale and must still be purged below --
            # otherwise this same missing-file candidate gets silently
            # re-evaluated (and silently skipped) on every future scan
            # forever, permanently drifting the index from disk reality.
            pass
        deleted.append(relative_path)
        total_bytes -= size

    return deleted, deleted_bytes


def _retention_lock() -> threading.RLock:
    lock = current_app.extensions.get("storage_retention_lock")
    if lock is None:
        lock = threading.RLock()
        current_app.extensions["storage_retention_lock"] = lock
    return lock


def _apply_retention(
    *,
    policy: StorageRetentionPolicy | None,
    gate: RetentionScanGate | None,
    exclude_relative_paths: set[str],
    list_candidates: Callable[..., tuple[list[tuple[str, int, datetime]], int]],
    delete_file: Callable[[str], int],
    delete_metadata: Callable[[list[str]], int],
    log_prefix: str,
    honor_upload_gate: bool,
) -> list[str]:
    if policy is None or not policy.enabled:
        return []

    if honor_upload_gate and gate is not None and not gate.should_scan():
        return []

    with _retention_lock():
        candidates, total_bytes = list_candidates(
            exclude_relative_paths=exclude_relative_paths
        )
        deleted, deleted_bytes = _run_retention_scan(
            policy=policy,
            candidates=candidates,
            total_bytes=total_bytes,
            delete_file=delete_file,
        )
        if not deleted:
            return []

        deleted_metadata = delete_metadata(deleted)
        current_app.logger.info(
            "%s retention deleted %s files (%s bytes), metadata_rows=%s",
            log_prefix,
            len(deleted),
            deleted_bytes,
            deleted_metadata,
        )
        return deleted


def apply_upload_retention(stored: StoredImage) -> list[str]:
    return _apply_retention(
        policy=current_app.extensions.get("storage_retention_policy"),
        gate=current_app.extensions.get("storage_retention_scan_gate"),
        exclude_relative_paths={stored.relative_path},
        list_candidates=current_app.extensions["metadata_store"].list_snapshot_retention_candidates,
        delete_file=current_app.extensions["storage"].delete_image_by_relative_path,
        delete_metadata=current_app.extensions["metadata_store"].delete_snapshots_by_relative_paths,
        log_prefix="Storage",
        honor_upload_gate=True,
    )


def apply_video_upload_retention(stored: StoredVideo) -> list[str]:
    return _apply_retention(
        policy=current_app.extensions.get("video_retention_policy"),
        gate=current_app.extensions.get("video_retention_scan_gate"),
        exclude_relative_paths={stored.relative_path},
        list_candidates=current_app.extensions["metadata_store"].list_video_retention_candidates,
        delete_file=current_app.extensions["storage"].delete_video_by_relative_path,
        delete_metadata=current_app.extensions["metadata_store"].delete_videos_by_relative_paths,
        log_prefix="Video",
        honor_upload_gate=True,
    )


def apply_scheduled_retention() -> tuple[list[str], list[str]]:
    """Run both retention policies without depending on a new upload."""

    image_paths = _apply_retention(
        policy=current_app.extensions.get("storage_retention_policy"),
        gate=None,
        exclude_relative_paths=set(),
        list_candidates=current_app.extensions["metadata_store"].list_snapshot_retention_candidates,
        delete_file=current_app.extensions["storage"].delete_image_by_relative_path,
        delete_metadata=current_app.extensions["metadata_store"].delete_snapshots_by_relative_paths,
        log_prefix="Storage",
        honor_upload_gate=False,
    )
    video_paths = _apply_retention(
        policy=current_app.extensions.get("video_retention_policy"),
        gate=None,
        exclude_relative_paths=set(),
        list_candidates=current_app.extensions["metadata_store"].list_video_retention_candidates,
        delete_file=current_app.extensions["storage"].delete_video_by_relative_path,
        delete_metadata=current_app.extensions["metadata_store"].delete_videos_by_relative_paths,
        log_prefix="Video",
        honor_upload_gate=False,
    )
    return image_paths, video_paths


class RetentionScheduler:
    """Periodically enforce image/video retention independently of uploads."""

    def __init__(self, app, *, interval_seconds: float) -> None:
        self._app = app
        self.interval_seconds = max(0.0, float(interval_seconds))
        self._stop_event = threading.Event()
        self._state_lock = threading.Lock()
        self._thread: threading.Thread | None = None

    @property
    def is_running(self) -> bool:
        thread = self._thread
        return thread is not None and thread.is_alive()

    def start(self) -> bool:
        if self.interval_seconds <= 0:
            return False
        with self._state_lock:
            if self.is_running:
                return False
            self._stop_event.clear()
            self._thread = threading.Thread(
                target=self._run_loop,
                name="storage-retention",
                daemon=True,
            )
            self._thread.start()
            self._app.logger.info(
                "Storage retention scheduler started (interval_seconds=%s)",
                self.interval_seconds,
            )
        return True

    def stop(self, timeout: float = 5.0) -> None:
        self._stop_event.set()
        thread = self._thread
        if thread is None:
            return
        if thread is not threading.current_thread():
            thread.join(timeout=timeout)
            if thread.is_alive():
                self._app.logger.warning(
                    "Storage retention scheduler stop timed out (timeout_seconds=%s)",
                    timeout,
                )
                return
            with self._state_lock:
                if self._thread is thread:
                    self._thread = None
            self._app.logger.info("Storage retention scheduler stopped")
            return
        self._app.logger.info("Storage retention scheduler stop requested")

    def run_once(self) -> tuple[list[str], list[str]]:
        with self._app.app_context():
            image_paths, video_paths = apply_scheduled_retention()
        self._app.logger.info(
            "Scheduled retention completed (images_deleted=%s, videos_deleted=%s)",
            len(image_paths),
            len(video_paths),
        )
        return image_paths, video_paths

    def _run_loop(self) -> None:
        while not self._stop_event.is_set():
            try:
                self.run_once()
            except Exception:
                self._app.logger.exception("Scheduled retention failed")
            if self._stop_event.wait(self.interval_seconds):
                return
