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


def apply_upload_retention(stored: StoredImage) -> list[str]:
    policy = current_app.extensions.get("storage_retention_policy")
    if policy is None or not policy.enabled:
        return []

    gate = current_app.extensions.get("storage_retention_scan_gate")
    if gate is not None and not gate.should_scan():
        return []

    metadata_store = current_app.extensions["metadata_store"]
    candidates, total_bytes = metadata_store.list_snapshot_retention_candidates(
        exclude_relative_paths={stored.relative_path}
    )
    deleted, deleted_bytes = _run_retention_scan(
        policy=policy,
        candidates=candidates,
        total_bytes=total_bytes,
        delete_file=current_app.extensions["storage"].delete_image_by_relative_path,
    )
    if not deleted:
        return []

    deleted_metadata = metadata_store.delete_snapshots_by_relative_paths(deleted)
    current_app.logger.info(
        "Storage retention deleted %s files (%s bytes), metadata_rows=%s",
        len(deleted),
        deleted_bytes,
        deleted_metadata,
    )
    return deleted


def apply_video_upload_retention(stored: StoredVideo) -> list[str]:
    policy = current_app.extensions.get("video_retention_policy")
    if policy is None or not policy.enabled:
        return []

    gate = current_app.extensions.get("video_retention_scan_gate")
    if gate is not None and not gate.should_scan():
        return []

    metadata_store = current_app.extensions["metadata_store"]
    candidates, total_bytes = metadata_store.list_video_retention_candidates(
        exclude_relative_paths={stored.relative_path}
    )
    deleted, deleted_bytes = _run_retention_scan(
        policy=policy,
        candidates=candidates,
        total_bytes=total_bytes,
        delete_file=current_app.extensions["storage"].delete_video_by_relative_path,
    )
    if not deleted:
        return []

    deleted_metadata = metadata_store.delete_videos_by_relative_paths(deleted)
    current_app.logger.info(
        "Video retention deleted %s files (%s bytes), metadata_rows=%s",
        len(deleted),
        deleted_bytes,
        deleted_metadata,
    )
    return deleted
