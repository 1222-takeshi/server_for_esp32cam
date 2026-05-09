from __future__ import annotations

import json
import os
import time
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
    time.sleep(0.01)
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
        storage.read_image("../secret.jpg")


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


def test_apply_retention_deletes_images_older_than_max_age(tmp_path):
    storage = LocalFilesystemStorage(tmp_path / "uploads")
    captured_at = datetime(2026, 5, 9, 12, 0, tzinfo=timezone.utc)
    old = storage.save_image(b"\xff\xd8old", "image/jpeg", device_id="cam-a", captured_at=captured_at)
    keep = storage.save_image(b"\xff\xd8keep", "image/jpeg", device_id="cam-a", captured_at=captured_at)

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


def test_apply_retention_ignores_stale_candidates_that_were_already_deleted(tmp_path, monkeypatch):
    storage = LocalFilesystemStorage(tmp_path / "uploads")
    captured_at = datetime(2026, 5, 9, 12, 0, tzinfo=timezone.utc)
    stale = storage.save_image(b"\xff\xd8old", "image/jpeg", device_id="cam-a", captured_at=captured_at)
    stale_path = storage.upload_dir / stale.relative_path
    storage._metadata_path(stale_path).unlink()
    stale_path.unlink()
    monkeypatch.setattr(storage, "list_images", lambda: [stale])

    result = storage.apply_retention(
        StorageRetentionPolicy(max_age_seconds=60),
        now=datetime.fromtimestamp(1_800_000_120, tz=timezone.utc),
    )

    assert result.deleted_relative_paths == []
    assert result.deleted_bytes == 0


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
