from __future__ import annotations

from collections.abc import Callable, Iterable
from datetime import datetime, timedelta, timezone
import logging
import threading

from flask import current_app

from script.storage import StorageRetentionPolicy, StoredImage, StoredVideo


_logger = logging.getLogger(__name__)
_logger.setLevel(logging.INFO)


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
    candidates: Iterable[tuple[str, int, datetime]],
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
            # Candidates are ordered oldest-first. Once a row is neither
            # age-expired nor needed for the byte quota, no later row can be
            # age-expired and the quota is already satisfied.
            break
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


def _optimized_retention_readers(
    metadata_store: object,
    *,
    candidate_method: str,
    table: str,
) -> tuple[
    Callable[..., Iterable[tuple[str, int, datetime]]] | None,
    Callable[[], int] | None,
]:
    if getattr(type(metadata_store), candidate_method, None) is None:
        return None, None
    candidates = getattr(metadata_store, candidate_method)
    return candidates, lambda: metadata_store._total_retention_bytes(table)


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
    iter_candidates: Callable[..., Iterable[tuple[str, int, datetime]]] | None = None,
    total_bytes_reader: Callable[[], int] | None = None,
) -> list[str]:
    if policy is None or not policy.enabled:
        return []

    if honor_upload_gate and gate is not None and not gate.should_scan():
        return []

    with _retention_lock():
        if iter_candidates is not None and total_bytes_reader is not None:
            age_cutoff = None
            if policy.max_age_seconds is not None and (
                policy.max_bytes is None or policy.max_bytes <= 0
            ):
                age_cutoff = datetime.now(tz=timezone.utc) - timedelta(
                    seconds=policy.max_age_seconds
                )
            candidates = iter_candidates(
                exclude_relative_paths=exclude_relative_paths,
                older_than=age_cutoff,
            )
            total_bytes = total_bytes_reader()
        else:
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
        _logger.info(
            "%s retention deleted %s files (%s bytes), metadata_rows=%s",
            log_prefix,
            len(deleted),
            deleted_bytes,
            deleted_metadata,
        )
        return deleted


def apply_upload_retention(stored: StoredImage) -> list[str]:
    metadata_store = current_app.extensions["metadata_store"]
    iter_candidates, total_bytes_reader = _optimized_retention_readers(
        metadata_store,
        candidate_method="iter_snapshot_retention_candidates",
        table="snapshots",
    )
    return _apply_retention(
        policy=current_app.extensions.get("storage_retention_policy"),
        gate=current_app.extensions.get("storage_retention_scan_gate"),
        exclude_relative_paths={stored.relative_path},
        list_candidates=metadata_store.list_snapshot_retention_candidates,
        delete_file=current_app.extensions["storage"].delete_image_by_relative_path,
        delete_metadata=metadata_store.delete_snapshots_by_relative_paths,
        log_prefix="Storage",
        honor_upload_gate=True,
        iter_candidates=iter_candidates,
        total_bytes_reader=total_bytes_reader,
    )


def apply_video_upload_retention(stored: StoredVideo) -> list[str]:
    metadata_store = current_app.extensions["metadata_store"]
    iter_candidates, total_bytes_reader = _optimized_retention_readers(
        metadata_store,
        candidate_method="iter_video_retention_candidates",
        table="videos",
    )
    return _apply_retention(
        policy=current_app.extensions.get("video_retention_policy"),
        gate=current_app.extensions.get("video_retention_scan_gate"),
        exclude_relative_paths={stored.relative_path},
        list_candidates=metadata_store.list_video_retention_candidates,
        delete_file=current_app.extensions["storage"].delete_video_by_relative_path,
        delete_metadata=metadata_store.delete_videos_by_relative_paths,
        log_prefix="Video",
        honor_upload_gate=True,
        iter_candidates=iter_candidates,
        total_bytes_reader=total_bytes_reader,
    )


def apply_device_log_retention() -> int:
    """Delete device_logs rows older than the configured max age.

    device_logs has no on-disk file and no byte budget, so unlike
    apply_upload_retention()/apply_video_upload_retention() this only needs
    an age cutoff and a single DELETE -- no candidate scan. Disabled by
    default (`DEVICE_LOG_RETENTION_MAX_AGE_SECONDS` <= 0). Runs on the same
    RetentionScheduler cadence as image/video retention rather than a
    separate background loop (#357).
    """
    max_age_seconds = current_app.config.get("DEVICE_LOG_RETENTION_MAX_AGE_SECONDS", 0)
    if not max_age_seconds or max_age_seconds <= 0:
        return 0

    metadata_store = current_app.extensions["metadata_store"]
    cutoff = datetime.now(tz=timezone.utc) - timedelta(seconds=max_age_seconds)
    deleted = metadata_store.delete_device_logs_older_than(cutoff)
    if deleted:
        _logger.info("Device log retention deleted %s rows", deleted)
    return deleted


def apply_scheduled_retention() -> tuple[list[str], list[str], int]:
    """Run both retention policies without depending on a new upload."""

    metadata_store = current_app.extensions["metadata_store"]
    image_iter_candidates, image_total_bytes_reader = _optimized_retention_readers(
        metadata_store,
        candidate_method="iter_snapshot_retention_candidates",
        table="snapshots",
    )
    video_iter_candidates, video_total_bytes_reader = _optimized_retention_readers(
        metadata_store,
        candidate_method="iter_video_retention_candidates",
        table="videos",
    )

    image_paths = _apply_retention(
        policy=current_app.extensions.get("storage_retention_policy"),
        gate=None,
        exclude_relative_paths=set(),
        list_candidates=metadata_store.list_snapshot_retention_candidates,
        delete_file=current_app.extensions["storage"].delete_image_by_relative_path,
        delete_metadata=metadata_store.delete_snapshots_by_relative_paths,
        log_prefix="Storage",
        honor_upload_gate=False,
        iter_candidates=image_iter_candidates,
        total_bytes_reader=image_total_bytes_reader,
    )
    video_paths = _apply_retention(
        policy=current_app.extensions.get("video_retention_policy"),
        gate=None,
        exclude_relative_paths=set(),
        list_candidates=metadata_store.list_video_retention_candidates,
        delete_file=current_app.extensions["storage"].delete_video_by_relative_path,
        delete_metadata=metadata_store.delete_videos_by_relative_paths,
        log_prefix="Video",
        honor_upload_gate=False,
        iter_candidates=video_iter_candidates,
        total_bytes_reader=video_total_bytes_reader,
    )
    device_logs_deleted = apply_device_log_retention()
    return image_paths, video_paths, device_logs_deleted


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
            _logger.info(
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
                _logger.warning(
                    "Storage retention scheduler stop timed out (timeout_seconds=%s)",
                    timeout,
                )
                return
            with self._state_lock:
                if self._thread is thread:
                    self._thread = None
            _logger.info("Storage retention scheduler stopped")
            return
        _logger.info("Storage retention scheduler stop requested")

    def run_once(self) -> tuple[list[str], list[str], int]:
        with self._app.app_context():
            image_paths, video_paths, device_logs_deleted = apply_scheduled_retention()
        _logger.info(
            "Scheduled retention completed (images_deleted=%s, videos_deleted=%s, "
            "device_logs_deleted=%s)",
            len(image_paths),
            len(video_paths),
            device_logs_deleted,
        )
        return image_paths, video_paths, device_logs_deleted

    def _run_loop(self) -> None:
        while not self._stop_event.is_set():
            try:
                self.run_once()
            except Exception:
                _logger.exception("Scheduled retention failed")
            if self._stop_event.wait(self.interval_seconds):
                return
