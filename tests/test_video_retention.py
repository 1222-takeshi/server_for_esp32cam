from __future__ import annotations

import os
from datetime import datetime, timezone
from uuid import uuid4

from script.app import create_app
from script.storage import LocalFilesystemStorage, StorageRetentionPolicy


def _mp4() -> bytes:
    return b"\x00\x00\x00\x18ftypmp42\x00\x00\x00\x00mp42isom" + b"\x00\x00\x00\x08mdat"


def _save_video(storage: LocalFilesystemStorage, camera_id: str = "cam-a") -> object:
    return storage.save_video(
        _mp4(),
        "video/mp4",
        camera_id=camera_id,
        captured_at=datetime(2026, 6, 1, 12, 0, tzinfo=timezone.utc),
    )


def _pin_mtime(storage: LocalFilesystemStorage, video, ts: float) -> None:
    path = storage.upload_dir / video.relative_path
    os.utime(path, (ts, ts))
    sidecar = storage._metadata_path(path)
    if sidecar.exists():
        os.utime(sidecar, (ts, ts))


def test_apply_video_retention_age_deletes_old_video(tmp_path):
    storage = LocalFilesystemStorage(tmp_path / "uploads")
    old = _save_video(storage)
    keep = _save_video(storage)
    _pin_mtime(storage, old, 1_800_000_000)
    _pin_mtime(storage, keep, 1_800_000_120)

    result = storage.apply_video_retention(
        StorageRetentionPolicy(max_age_seconds=60),
        now=datetime.fromtimestamp(1_800_000_120, tz=timezone.utc),
        preserve_relative_paths={keep.relative_path},
    )

    assert result.deleted_relative_paths == [old.relative_path]
    assert result.deleted_bytes == old.size
    assert not (storage.upload_dir / old.relative_path).exists()
    assert (storage.upload_dir / keep.relative_path).exists()


def test_apply_video_retention_bytes_deletes_oldest_until_under_limit(tmp_path):
    storage = LocalFilesystemStorage(tmp_path / "uploads")
    oldest = storage.save_video(
        b"\x00\x00\x00 ftypisom" + b"a" * 20,
        "video/mp4",
        camera_id="cam-a",
        captured_at=datetime(2026, 6, 1, 12, 0, tzinfo=timezone.utc),
    )
    middle = storage.save_video(
        b"\x00\x00\x00 ftypisom" + b"b" * 30,
        "video/mp4",
        camera_id="cam-a",
        captured_at=datetime(2026, 6, 1, 12, 0, tzinfo=timezone.utc),
    )
    newest = storage.save_video(
        b"\x00\x00\x00 ftypisom" + b"c" * 40,
        "video/mp4",
        camera_id="cam-a",
        captured_at=datetime(2026, 6, 1, 12, 0, tzinfo=timezone.utc),
    )
    for offset, v in enumerate([oldest, middle, newest]):
        _pin_mtime(storage, v, 1_800_000_000 + offset)

    total = oldest.size + middle.size + newest.size
    limit = total - oldest.size

    result = storage.apply_video_retention(
        StorageRetentionPolicy(max_bytes=limit),
        preserve_relative_paths={newest.relative_path},
    )

    assert oldest.relative_path in result.deleted_relative_paths
    assert not (storage.upload_dir / oldest.relative_path).exists()
    assert (storage.upload_dir / middle.relative_path).exists()
    assert (storage.upload_dir / newest.relative_path).exists()


def test_apply_video_retention_preserve_skips_current_upload(tmp_path):
    storage = LocalFilesystemStorage(tmp_path / "uploads")
    v1 = _save_video(storage)
    v2 = _save_video(storage)
    _pin_mtime(storage, v1, 1_700_000_000)
    _pin_mtime(storage, v2, 1_700_001_000)

    result = storage.apply_video_retention(
        StorageRetentionPolicy(max_age_seconds=1),
        now=datetime.fromtimestamp(1_700_002_000, tz=timezone.utc),
        preserve_relative_paths={v2.relative_path},
    )

    assert v2.relative_path not in result.deleted_relative_paths
    assert (storage.upload_dir / v2.relative_path).exists()


def test_apply_video_retention_disabled_policy_returns_empty(tmp_path):
    storage = LocalFilesystemStorage(tmp_path / "uploads")
    v = _save_video(storage)
    _pin_mtime(storage, v, 1_000_000_000)

    result = storage.apply_video_retention(
        StorageRetentionPolicy(),
        now=datetime.fromtimestamp(2_000_000_000, tz=timezone.utc),
    )

    assert result.deleted_relative_paths == []
    assert result.deleted_bytes == 0
    assert (storage.upload_dir / v.relative_path).exists()


def _video_app(tmp_path, **overrides):
    config = {
        "TESTING": True,
        "UPLOAD_DIR": tmp_path / f"uploads-{uuid4().hex}",
        "DB_PATH": tmp_path / f"esp32cam-{uuid4().hex}.sqlite3",
        "INFERENCE_MODE": "sync",
        "RATELIMIT_ENABLED": False,
        "DEVICE_API_KEYS": {"cam-a": "key-a"},
        "MAX_CONTENT_LENGTH": 10 * 1024 * 1024,
        "MAX_VIDEO_CONTENT_LENGTH": 1024,
    }
    config.update(overrides)
    return create_app(config)


def test_video_upload_triggers_retention_and_deletes_old_video(tmp_path):
    app = _video_app(
        tmp_path,
        VIDEO_RETENTION_MAX_AGE_SECONDS=1,
    )
    storage = app.extensions["storage"]

    old = storage.save_video(
        _mp4(),
        "video/mp4",
        camera_id="cam-a",
        captured_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
    )
    old_path = storage.upload_dir / old.relative_path
    _pin_mtime(storage, old, 1_000_000_000)

    with app.test_client() as client:
        response = client.post(
            "/api/cameras/cam-a/videos",
            data=_mp4(),
            headers={
                "Content-Type": "video/mp4",
                "X-API-Key": "key-a",
                "X-Captured-At": "2026-06-01T12:00:00Z",
            },
        )

    assert response.status_code == 201
    assert not old_path.exists()
