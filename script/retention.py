from __future__ import annotations

from time import monotonic as _monotonic

from flask import current_app

from script.storage import StoredImage, StoredVideo

_LAST_RUN_KEY = "storage_retention_last_run_monotonic"


def apply_upload_retention(stored: StoredImage) -> list[str]:
    policy = current_app.extensions.get("storage_retention_policy")
    if policy is None or not policy.enabled:
        return []

    # The retention scan walks the whole upload directory, which blocks the
    # event loop under gevent. Running it on every upload melts down once the
    # file count grows, so rate-limit the scan; deletions still catch up on
    # the next allowed run.
    min_interval = current_app.config.get("STORAGE_RETENTION_MIN_INTERVAL_SECONDS", 0)
    if min_interval > 0:
        last_run = current_app.extensions.get(_LAST_RUN_KEY)
        now = _monotonic()
        if last_run is not None and now - last_run < min_interval:
            return []
        current_app.extensions[_LAST_RUN_KEY] = now

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
