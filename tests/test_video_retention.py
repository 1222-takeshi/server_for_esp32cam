from __future__ import annotations

import io
import os
from dataclasses import replace
from datetime import datetime, timezone
from uuid import uuid4

from script.app import create_app
from script.storage import LocalFilesystemStorage


def _mp4() -> bytes:
    return b"\x00\x00\x00\x18ftypmp42\x00\x00\x00\x00mp42isom" + b"\x00\x00\x00\x08mdat"


def _pin_mtime(storage: LocalFilesystemStorage, video, ts: float) -> None:
    path = storage.upload_dir / video.relative_path
    os.utime(path, (ts, ts))
    sidecar = storage._metadata_path(path)
    if sidecar.exists():
        os.utime(sidecar, (ts, ts))


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
    metadata_store = app.extensions["metadata_store"]

    old_payload = _mp4()
    old = storage.save_video_stream(
        io.BytesIO(old_payload),
        "video/mp4",
        camera_id="cam-a",
        captured_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        max_bytes=len(old_payload) + 1024,
    )
    old_path = storage.upload_dir / old.relative_path
    _pin_mtime(storage, old, 1_000_000_000)
    # Retention candidates now come from the SQLite index (#200/#225/#246/#260)
    # instead of a filesystem walk, so the video needs a matching DB row --
    # mirroring what the real /api/cameras/<camera_id>/videos route does via
    # record_video() right after save_video_stream(). The recorded stored_at
    # must reflect the pinned (old) mtime, not whatever save_video_stream() saw.
    metadata_store.record_video(
        replace(old, stored_at=datetime.fromtimestamp(1_000_000_000, tz=timezone.utc))
    )

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
