from __future__ import annotations

from flask import current_app

from script.storage import StoredImage, StoredVideo


def apply_upload_retention(stored: StoredImage) -> list[str]:
    policy = current_app.extensions.get("storage_retention_policy")
    if policy is None or not policy.enabled:
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
