from __future__ import annotations

import hashlib
import io
import json
import os
from datetime import datetime, timezone

import pytest

from script.storage import LocalFilesystemStorage, StorageRetentionPolicy


def test_storage_creates_upload_directory(tmp_path):
    upload_dir = tmp_path / "uploads"

    LocalFilesystemStorage(upload_dir)

    assert upload_dir.exists()
    assert upload_dir.is_dir()


def test_storage_generates_unique_names(tmp_path):
    storage = LocalFilesystemStorage(tmp_path / "uploads")

    first = storage.save_image(
        b"a",
        "image/jpeg",
        device_id="cam-a",
        captured_at=datetime(2026, 3, 11, 12, 0, tzinfo=timezone.utc),
    )
    second = storage.save_image(
        b"b",
        "image/jpeg",
        device_id="cam-a",
        captured_at=datetime(2026, 3, 11, 12, 1, tzinfo=timezone.utc),
    )

    assert first.name != second.name


def test_storage_lists_latest_first(tmp_path):
    storage = LocalFilesystemStorage(tmp_path / "uploads")
    first = storage.save_image(
        b"a",
        "image/jpeg",
        device_id="cam-a",
        captured_at=datetime(2026, 3, 11, 12, 0, tzinfo=timezone.utc),
    )
    # Pin mtime to a deterministic past time so ordering doesn't depend on wall-clock resolution
    first_path = tmp_path / "uploads" / first.relative_path
    past_ts = datetime(2026, 3, 11, 11, 59, 59, tzinfo=timezone.utc).timestamp()
    os.utime(first_path, (past_ts, past_ts))
    second = storage.save_image(
        b"b",
        "image/jpeg",
        device_id="cam-b",
        captured_at=datetime(2026, 3, 11, 12, 1, tzinfo=timezone.utc),
    )

    files = storage.list_images()

    assert [file.name for file in files] == [second.name, first.name]
    assert files[0].device_id == "cam-b"
    assert files[0].captured_at == datetime(2026, 3, 11, 12, 1, tzinfo=timezone.utc)


def test_storage_rejects_traversal(tmp_path):
    storage = LocalFilesystemStorage(tmp_path / "uploads")

    with pytest.raises(FileNotFoundError):
        storage.read_image_by_relative_path("../secret.jpg")


def test_storage_lists_only_jpeg_files(tmp_path):
    storage = LocalFilesystemStorage(tmp_path / "uploads")
    storage.save_image(
        b"\xff\xd8jpeg",
        "image/jpeg",
        device_id="cam-a",
        captured_at=datetime(2026, 3, 11, 12, 0, tzinfo=timezone.utc),
    )
    (tmp_path / "uploads" / "notes.txt").write_text("ignore me")

    files = storage.list_images()

    assert len(files) == 1


def test_storage_persists_request_metadata_in_camera_directory(tmp_path):
    storage = LocalFilesystemStorage(tmp_path / "uploads")
    stored = storage.save_image(
        b"\xff\xd8jpeg",
        "image/jpeg",
        device_id="esp32cam-front-door",
        captured_at=datetime(2026, 3, 11, 12, 34, 56, tzinfo=timezone.utc),
    )

    image_path = tmp_path / "uploads" / stored.relative_path
    metadata_path = image_path.with_suffix(".jpg.json")
    files = storage.list_images()
    payload = json.loads(metadata_path.read_text(encoding="utf-8"))

    assert image_path.exists()
    assert stored.relative_path.startswith("esp32cam-front-door/images/")
    assert payload == {
        "device_id": "esp32cam-front-door",
        "captured_at": "2026-03-11T12:34:56+00:00",
    }
    assert files[0].device_id == "esp32cam-front-door"
    assert files[0].captured_at == datetime(2026, 3, 11, 12, 34, 56, tzinfo=timezone.utc)


def test_storage_read_image_metadata_returns_device_id_and_captured_at(tmp_path):
    storage = LocalFilesystemStorage(tmp_path / "uploads")
    stored = storage.save_image(
        b"\xff\xd8jpeg",
        "image/jpeg",
        device_id="esp32cam-front-door",
        captured_at=datetime(2026, 3, 11, 12, 34, 56, tzinfo=timezone.utc),
    )

    device_id, captured_at = storage.read_image_metadata(stored.relative_path)

    assert device_id == "esp32cam-front-door"
    assert captured_at == datetime(2026, 3, 11, 12, 34, 56, tzinfo=timezone.utc)


def test_storage_cleans_up_partial_write_when_metadata_write_fails(tmp_path, monkeypatch):
    storage = LocalFilesystemStorage(tmp_path / "uploads")

    def fail_write_metadata(*_args, **_kwargs):
        raise OSError("metadata write failed")

    monkeypatch.setattr(storage, "_write_metadata", fail_write_metadata)

    with pytest.raises(OSError):
        storage.save_image(
            b"\xff\xd8jpeg",
            "image/jpeg",
            device_id="esp32cam-front-door",
            captured_at=datetime(2026, 3, 11, 12, 34, 56, tzinfo=timezone.utc),
        )

    assert list((tmp_path / "uploads").rglob("*")) == []


def test_storage_lists_legacy_images_without_metadata(tmp_path):
    upload_dir = tmp_path / "uploads"
    storage = LocalFilesystemStorage(upload_dir)
    legacy_path = upload_dir / "legacy.jpg"
    legacy_path.write_bytes(b"\xff\xd8legacy")

    files = storage.list_images()

    assert files[0].name == legacy_path.name
    assert files[0].relative_path == legacy_path.name
    assert files[0].device_id is None
    assert files[0].captured_at is None


def test_storage_ignores_invalid_metadata_sidecar(tmp_path):
    storage = LocalFilesystemStorage(tmp_path / "uploads")
    image_path = tmp_path / "uploads" / "legacy.jpg"
    image_path.write_bytes(b"\xff\xd8legacy")
    image_path.with_suffix(".jpg.json").write_text("{broken", encoding="utf-8")

    files = storage.list_images()

    assert files[0].name == "legacy.jpg"
    assert files[0].device_id is None
    assert files[0].captured_at is None


def test_storage_ignores_non_utf8_metadata_sidecar(tmp_path):
    storage = LocalFilesystemStorage(tmp_path / "uploads")
    image_path = tmp_path / "uploads" / "legacy.jpg"
    image_path.write_bytes(b"\xff\xd8legacy")
    image_path.with_suffix(".jpg.json").write_bytes(b"\xff")

    files = storage.list_images()

    assert files[0].name == "legacy.jpg"
    assert files[0].device_id is None
    assert files[0].captured_at is None


def test_storage_reads_image_by_relative_path(tmp_path):
    storage = LocalFilesystemStorage(tmp_path / "uploads")
    stored = storage.save_image(
        b"\xff\xd8jpeg",
        "image/jpeg",
        device_id="cam-a",
        captured_at=datetime(2026, 3, 11, 12, 0, tzinfo=timezone.utc),
    )

    payload = storage.read_image_by_relative_path(stored.relative_path)

    assert payload == b"\xff\xd8jpeg"


def test_storage_read_image_path_by_relative_path_returns_resolved_path(tmp_path):
    storage = LocalFilesystemStorage(tmp_path / "uploads")
    stored = storage.save_image(
        b"\xff\xd8jpeg",
        "image/jpeg",
        device_id="cam-a",
        captured_at=datetime(2026, 3, 11, 12, 0, tzinfo=timezone.utc),
    )

    path = storage.read_image_path_by_relative_path(stored.relative_path)

    assert path == storage.upload_dir / stored.relative_path
    assert path.read_bytes() == b"\xff\xd8jpeg"


def test_apply_retention_deletes_images_older_than_max_age(tmp_path):
    storage = LocalFilesystemStorage(tmp_path / "uploads")
    captured_at = datetime(2026, 5, 9, 12, 0, tzinfo=timezone.utc)
    old = storage.save_image(
        b"\xff\xd8old", "image/jpeg", device_id="cam-a", captured_at=captured_at
    )
    keep = storage.save_image(
        b"\xff\xd8keep", "image/jpeg", device_id="cam-a", captured_at=captured_at
    )

    old_path = storage.upload_dir / old.relative_path
    keep_path = storage.upload_dir / keep.relative_path
    os.utime(old_path, (1_800_000_000, 1_800_000_000))
    os.utime(keep_path, (1_800_000_120, 1_800_000_120))

    result = storage.apply_retention(
        StorageRetentionPolicy(max_age_seconds=60),
        now=datetime.fromtimestamp(1_800_000_120, tz=timezone.utc),
        preserve_relative_paths={keep.relative_path},
    )

    assert result.deleted_relative_paths == [old.relative_path]
    assert result.deleted_bytes == old.size
    assert not old_path.exists()
    assert not storage._metadata_path(old_path).exists()
    assert keep_path.exists()


def test_apply_retention_does_not_read_metadata_sidecars(tmp_path, monkeypatch):
    storage = LocalFilesystemStorage(tmp_path / "uploads")
    captured_at = datetime(2026, 5, 9, 12, 0, tzinfo=timezone.utc)
    old = storage.save_image(
        b"\xff\xd8old", "image/jpeg", device_id="cam-a", captured_at=captured_at
    )
    old_path = storage.upload_dir / old.relative_path
    os.utime(old_path, (1_800_000_000, 1_800_000_000))

    def fail_if_called(*_args, **_kwargs):
        raise AssertionError("apply_retention must not read metadata sidecars")

    monkeypatch.setattr(storage, "_read_metadata", fail_if_called)

    result = storage.apply_retention(
        StorageRetentionPolicy(max_age_seconds=60),
        now=datetime.fromtimestamp(1_800_000_120, tz=timezone.utc),
    )

    assert result.deleted_relative_paths == [old.relative_path]


def test_apply_retention_ignores_stale_candidates_that_were_already_deleted(tmp_path, monkeypatch):
    storage = LocalFilesystemStorage(tmp_path / "uploads")
    captured_at = datetime(2026, 5, 9, 12, 0, tzinfo=timezone.utc)
    stale = storage.save_image(
        b"\xff\xd8old", "image/jpeg", device_id="cam-a", captured_at=captured_at
    )
    stale_path = storage.upload_dir / stale.relative_path
    storage._metadata_path(stale_path).unlink()
    stale_path.unlink()
    monkeypatch.setattr(storage, "list_images", lambda **_: [stale])

    result = storage.apply_retention(
        StorageRetentionPolicy(max_age_seconds=60),
        now=datetime.fromtimestamp(1_800_000_120, tz=timezone.utc),
    )

    assert result.deleted_relative_paths == []
    assert result.deleted_bytes == 0


def test_delete_camera_files_removes_camera_directory(tmp_path):
    storage = LocalFilesystemStorage(tmp_path / "uploads")
    storage.save_image(
        b"\xff\xd8jpeg",
        "image/jpeg",
        device_id="cam-to-delete",
        captured_at=datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc),
    )
    camera_dir = tmp_path / "uploads" / "cam-to-delete"
    assert camera_dir.exists()

    storage.delete_camera_files("cam-to-delete")

    assert not camera_dir.exists()


def test_delete_camera_files_does_nothing_for_missing_directory(tmp_path):
    storage = LocalFilesystemStorage(tmp_path / "uploads")

    storage.delete_camera_files("nonexistent-cam")


def test_delete_camera_files_rejects_path_traversal(tmp_path):
    storage = LocalFilesystemStorage(tmp_path / "uploads")

    with pytest.raises(ValueError):
        storage.delete_camera_files("../secret")


def test_apply_retention_deletes_oldest_images_until_under_max_bytes(tmp_path):
    storage = LocalFilesystemStorage(tmp_path / "uploads")
    captured_at = datetime(2026, 5, 9, 12, 0, tzinfo=timezone.utc)
    oldest = storage.save_image(
        b"\xff\xd8" + (b"a" * 8), "image/jpeg", device_id="cam-a", captured_at=captured_at
    )
    middle = storage.save_image(
        b"\xff\xd8" + (b"b" * 18), "image/jpeg", device_id="cam-a", captured_at=captured_at
    )
    newest = storage.save_image(
        b"\xff\xd8" + (b"c" * 28), "image/jpeg", device_id="cam-a", captured_at=captured_at
    )

    for offset, image in enumerate([oldest, middle, newest]):
        path = storage.upload_dir / image.relative_path
        os.utime(path, (1_800_000_000 + offset, 1_800_000_000 + offset))

    result = storage.apply_retention(
        StorageRetentionPolicy(max_bytes=55),
        preserve_relative_paths={newest.relative_path},
    )

    assert result.deleted_relative_paths == [oldest.relative_path]
    assert result.deleted_bytes == oldest.size
    assert not (storage.upload_dir / oldest.relative_path).exists()
    assert (storage.upload_dir / middle.relative_path).exists()
    assert (storage.upload_dir / newest.relative_path).exists()


def test_save_video_persists_mp4_in_camera_video_directory_with_sidecar(tmp_path):
    storage = LocalFilesystemStorage(tmp_path / "uploads")
    captured_at = datetime(2026, 6, 1, 12, 0, 0, tzinfo=timezone.utc)
    payload = b"\x00\x00\x00 ftypisom" + b"video-bytes"

    stored = storage.save_video_stream(
        _BoundedReadStream(payload),
        "video/mp4",
        camera_id="esp32cam-01",
        captured_at=captured_at,
        duration_ms=5000,
        max_bytes=len(payload) + 1024,
    )

    video_path = storage.upload_dir / stored.relative_path
    sidecar_path = video_path.with_suffix(".mp4.json")
    sidecar = json.loads(sidecar_path.read_text(encoding="utf-8"))

    assert stored.name.startswith("video_20260601T120000000000_")
    assert stored.name.endswith(".mp4")
    assert stored.relative_path.startswith("esp32cam-01/videos/")
    assert stored.size == len(payload)
    assert stored.camera_id == "esp32cam-01"
    assert stored.captured_at == captured_at
    assert stored.duration_ms == 5000
    assert stored.content_type == "video/mp4"
    assert len(stored.sha256) == 64
    assert video_path.read_bytes() == payload

    path, content_type = storage.read_video_path_by_relative_path(stored.relative_path)
    assert path == video_path
    assert content_type == "video/mp4"
    assert path.read_bytes() == payload
    assert sidecar == {
        "camera_id": "esp32cam-01",
        "captured_at": "2026-06-01T12:00:00+00:00",
        "stored_at": stored.stored_at.isoformat(),
        "duration_ms": 5000,
        "content_type": "video/mp4",
        "size": len(payload),
        "sha256": stored.sha256,
    }


def test_save_video_rejects_unsafe_camera_id_before_creating_paths(tmp_path):
    storage = LocalFilesystemStorage(tmp_path / "uploads")

    for camera_id in ["", "../cam", "cam/one", "cam one", "cam%2Fone", "cam\none"]:
        with pytest.raises(ValueError, match="Invalid camera_id"):
            storage.save_video_stream(
                _BoundedReadStream(b"\x00\x00\x00 ftypisom"),
                "video/mp4",
                camera_id=camera_id,
                captured_at=datetime(2026, 6, 1, 12, 0, tzinfo=timezone.utc),
                max_bytes=1024,
            )

    assert list(storage.upload_dir.rglob("*")) == []


def test_save_video_rejects_non_mp4_payload_and_cleans_temporary_file(tmp_path):
    storage = LocalFilesystemStorage(tmp_path / "uploads")

    with pytest.raises(ValueError, match="Unsupported video payload"):
        storage.save_video_stream(
            _BoundedReadStream(b"not an mp4"),
            "video/mp4",
            camera_id="esp32cam-01",
            captured_at=datetime(2026, 6, 1, 12, 0, tzinfo=timezone.utc),
            max_bytes=1024,
        )

    assert list(storage.upload_dir.rglob("*")) == []


def test_list_videos_backfills_sha256_for_legacy_sidecar_missing_hash(tmp_path):
    storage = LocalFilesystemStorage(tmp_path / "uploads")
    payload = b"\x00\x00\x00 ftypisom" + b"legacy-video-bytes"
    stored = storage.save_video_stream(
        _BoundedReadStream(payload),
        "video/mp4",
        camera_id="esp32cam-01",
        captured_at=datetime(2026, 6, 1, 12, 0, tzinfo=timezone.utc),
        max_bytes=len(payload) + 1024,
    )
    video_path = storage.upload_dir / stored.relative_path
    sidecar_path = video_path.with_suffix(".mp4.json")
    sidecar = json.loads(sidecar_path.read_text(encoding="utf-8"))
    del sidecar["sha256"]
    sidecar_path.write_text(json.dumps(sidecar), encoding="utf-8")

    videos = storage.list_videos()
    assert videos[0].sha256 == stored.sha256

    # The backfilled hash must be persisted so a second scan doesn't re-hash.
    refreshed_sidecar = json.loads(sidecar_path.read_text(encoding="utf-8"))
    assert refreshed_sidecar["sha256"] == stored.sha256


def test_list_videos_returns_latest_first(tmp_path):
    storage = LocalFilesystemStorage(tmp_path / "uploads")
    first_payload = b"\x00\x00\x00 ftypisom" + b"first"
    first = storage.save_video_stream(
        _BoundedReadStream(first_payload),
        "video/mp4",
        camera_id="cam-a",
        captured_at=datetime(2026, 6, 1, 12, 0, tzinfo=timezone.utc),
        max_bytes=len(first_payload) + 1024,
    )
    first_path = storage.upload_dir / first.relative_path
    os.utime(first_path, (1_700_000_000, 1_700_000_000))
    second_payload = b"\x00\x00\x00 ftypisom" + b"second"
    second = storage.save_video_stream(
        _BoundedReadStream(second_payload),
        "video/mp4",
        camera_id="cam-b",
        captured_at=datetime(2026, 6, 1, 12, 1, tzinfo=timezone.utc),
        max_bytes=len(second_payload) + 1024,
    )

    videos = storage.list_videos()

    assert [video.name for video in videos] == [second.name, first.name]
    assert videos[0].camera_id == "cam-b"


def test_delete_video_by_relative_path_removes_file_and_sidecar(tmp_path):
    storage = LocalFilesystemStorage(tmp_path / "uploads")
    payload = b"\x00\x00\x00 ftypisom" + b"video-bytes"
    stored = storage.save_video_stream(
        _BoundedReadStream(payload),
        "video/mp4",
        camera_id="esp32cam-01",
        captured_at=datetime(2026, 6, 1, 12, 0, tzinfo=timezone.utc),
        max_bytes=len(payload) + 1024,
    )
    video_path = storage.upload_dir / stored.relative_path
    sidecar_path = video_path.with_suffix(".mp4.json")
    assert video_path.exists()
    assert sidecar_path.exists()

    size = storage.delete_video_by_relative_path(stored.relative_path)

    assert size == len(payload)
    assert not video_path.exists()
    assert not sidecar_path.exists()


def test_delete_video_by_relative_path_raises_for_missing_file(tmp_path):
    storage = LocalFilesystemStorage(tmp_path / "uploads")

    with pytest.raises(FileNotFoundError):
        storage.delete_video_by_relative_path("cam-a/videos/missing.mp4")


def test_delete_camera_files_removes_images_and_videos(tmp_path):
    storage = LocalFilesystemStorage(tmp_path / "uploads")
    storage.save_image(
        b"\xff\xd8jpeg",
        "image/jpeg",
        device_id="cam-to-delete",
        captured_at=datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc),
    )
    video_payload = b"\x00\x00\x00 ftypisom" + b"video"
    storage.save_video_stream(
        _BoundedReadStream(video_payload),
        "video/mp4",
        camera_id="cam-to-delete",
        captured_at=datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc),
        max_bytes=len(video_payload) + 1024,
    )
    camera_dir = tmp_path / "uploads" / "cam-to-delete"

    storage.delete_camera_files("cam-to-delete")

    assert not camera_dir.exists()


def test_save_video_rejects_negative_duration(tmp_path):
    storage = LocalFilesystemStorage(tmp_path / "uploads")

    with pytest.raises(ValueError, match="duration_ms must be non-negative"):
        storage.save_video_stream(
            _BoundedReadStream(b"\x00\x00\x00 ftypisom"),
            "video/mp4",
            camera_id="esp32cam-01",
            captured_at=datetime(2026, 6, 1, 12, 0, tzinfo=timezone.utc),
            duration_ms=-1,
            max_bytes=1024,
        )

    assert list(storage.upload_dir.rglob("*")) == []


class _BoundedReadStream:
    """Wraps BytesIO but fails if read() is ever called without an explicit,
    non-negative size, so tests can prove the implementation always issues
    bounded reads instead of buffering the whole payload in one
    `read()`/`read(-1)` call (see #282)."""

    def __init__(self, data: bytes) -> None:
        self._buf = io.BytesIO(data)
        self.read_sizes: list[int] = []

    def read(self, size: int = -1) -> bytes:
        if size is None or size < 0:
            raise AssertionError(f"expected a bounded read(size), got read({size!r})")
        self.read_sizes.append(size)
        return self._buf.read(size)


def test_save_video_stream_persists_mp4_without_buffering_whole_payload(tmp_path):
    storage = LocalFilesystemStorage(tmp_path / "uploads")
    captured_at = datetime(2026, 6, 1, 12, 0, 0, tzinfo=timezone.utc)
    payload = b"\x00\x00\x00 ftypisom" + b"video-bytes" * 10
    stream = _BoundedReadStream(payload)

    stored = storage.save_video_stream(
        stream,
        "video/mp4",
        camera_id="esp32cam-01",
        captured_at=captured_at,
        duration_ms=5000,
        max_bytes=1024 * 1024,
        chunk_size=8,
    )

    # Proves chunking actually happened (not a single read() call).
    assert len(stream.read_sizes) > 1

    video_path = storage.upload_dir / stored.relative_path
    sidecar_path = video_path.with_suffix(".mp4.json")
    sidecar = json.loads(sidecar_path.read_text(encoding="utf-8"))

    assert stored.name.endswith(".mp4")
    assert stored.relative_path.startswith("esp32cam-01/videos/")
    assert stored.size == len(payload)
    assert stored.camera_id == "esp32cam-01"
    assert stored.captured_at == captured_at
    assert stored.duration_ms == 5000
    assert stored.content_type == "video/mp4"
    assert len(stored.sha256) == 64
    assert video_path.read_bytes() == payload
    assert sidecar["sha256"] == stored.sha256

    # No leftover temp files from the streamed write.
    assert list(storage.upload_dir.rglob("*.tmp")) == []


def test_save_video_stream_hash_matches_sha256_of_original_payload(tmp_path):
    storage = LocalFilesystemStorage(tmp_path / "uploads")
    payload = b"\x00\x00\x00 ftypisom" + b"identical-bytes"

    streamed = storage.save_video_stream(
        _BoundedReadStream(payload),
        "video/mp4",
        camera_id="esp32cam-02",
        captured_at=datetime(2026, 6, 1, 12, 0, tzinfo=timezone.utc),
        max_bytes=1024 * 1024,
        chunk_size=4,
    )

    assert streamed.sha256 == hashlib.sha256(payload).hexdigest()


def test_save_video_stream_rejects_empty_stream(tmp_path):
    storage = LocalFilesystemStorage(tmp_path / "uploads")

    with pytest.raises(ValueError, match="No data received"):
        storage.save_video_stream(
            _BoundedReadStream(b""),
            "video/mp4",
            camera_id="esp32cam-01",
            max_bytes=1024,
        )

    assert list(storage.upload_dir.rglob("*")) == []


def test_save_video_stream_rejects_non_mp4_payload_and_cleans_temporary_file(tmp_path):
    storage = LocalFilesystemStorage(tmp_path / "uploads")

    with pytest.raises(ValueError, match="Unsupported video payload"):
        storage.save_video_stream(
            _BoundedReadStream(b"not an mp4"),
            "video/mp4",
            camera_id="esp32cam-01",
            max_bytes=1024,
        )

    assert list(storage.upload_dir.rglob("*")) == []


def test_save_video_stream_rejects_payload_over_max_bytes_and_cleans_up(tmp_path):
    storage = LocalFilesystemStorage(tmp_path / "uploads")
    payload = b"\x00\x00\x00 ftypisom" + (b"x" * 100)

    with pytest.raises(ValueError, match="Payload too large"):
        storage.save_video_stream(
            _BoundedReadStream(payload),
            "video/mp4",
            camera_id="esp32cam-01",
            max_bytes=16,
            chunk_size=8,
        )

    assert list(storage.upload_dir.rglob("*")) == []


def test_save_video_stream_rejects_unsafe_camera_id_before_reading_stream(tmp_path):
    storage = LocalFilesystemStorage(tmp_path / "uploads")
    stream = _BoundedReadStream(b"\x00\x00\x00 ftypisom")

    with pytest.raises(ValueError, match="Invalid camera_id"):
        storage.save_video_stream(
            stream,
            "video/mp4",
            camera_id="../cam",
            max_bytes=1024,
        )

    # The stream must not have been touched: validation happens up front.
    assert stream.read_sizes == []
    assert list(storage.upload_dir.rglob("*")) == []


def test_save_video_stream_rejects_negative_duration_before_reading_stream(tmp_path):
    storage = LocalFilesystemStorage(tmp_path / "uploads")
    stream = _BoundedReadStream(b"\x00\x00\x00 ftypisom")

    with pytest.raises(ValueError, match="duration_ms must be non-negative"):
        storage.save_video_stream(
            stream,
            "video/mp4",
            camera_id="esp32cam-01",
            duration_ms=-1,
            max_bytes=1024,
        )

    assert stream.read_sizes == []
    assert list(storage.upload_dir.rglob("*")) == []
