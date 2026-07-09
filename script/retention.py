from __future__ import annotations

import threading

from flask import current_app

from script.storage import StoredImage, StoredVideo


class RetentionScanGate:
    """Gates full retention scans to run once every `interval` calls.

    ``apply_retention()`` / ``apply_video_retention()`` walk the entire
    storage directory (``rglob``, a ``stat()`` + metadata read per file, plus
    a full sort). Running that on literally every upload is O(N) synchronous
    disk I/O per request. This gate lets callers skip most of those scans and
    only pay the cost every ``interval`` uploads, while still guaranteeing a
    scan on the very first call so retention is enforced immediately in the
    common case (default ``interval=1`` preserves the old every-upload
    behavior exactly).

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


def apply_upload_retention(stored: StoredImage) -> list[str]:
    policy = current_app.extensions.get("storage_retention_policy")
    if policy is None or not policy.enabled:
        return []

    gate = current_app.extensions.get("storage_retention_scan_gate")
    if gate is not None and not gate.should_scan():
        return []

    result = current_app.extensions["storage"].apply_retention(
        policy,
        preserve_relative_paths={stored.relative_path},
    )
    if not result.deleted_relative_paths:
        return []

    deleted_metadata = current_app.extensions["metadata_store"].delete_snapshots_by_relative_paths(
        result.deleted_relative_paths
    )
    current_app.logger.info(
        "Storage retention deleted %s files (%s bytes), metadata_rows=%s",
        len(result.deleted_relative_paths),
        result.deleted_bytes,
        deleted_metadata,
    )
    return result.deleted_relative_paths


def apply_video_upload_retention(stored: StoredVideo) -> list[str]:
    policy = current_app.extensions.get("video_retention_policy")
    if policy is None or not policy.enabled:
        return []

    gate = current_app.extensions.get("video_retention_scan_gate")
    if gate is not None and not gate.should_scan():
        return []

    result = current_app.extensions["storage"].apply_video_retention(
        policy,
        preserve_relative_paths={stored.relative_path},
    )
    if not result.deleted_relative_paths:
        return []

    deleted_metadata = current_app.extensions["metadata_store"].delete_videos_by_relative_paths(
        result.deleted_relative_paths
    )
    current_app.logger.info(
        "Video retention deleted %s files (%s bytes), metadata_rows=%s",
        len(result.deleted_relative_paths),
        result.deleted_bytes,
        deleted_metadata,
    )
    return result.deleted_relative_paths
