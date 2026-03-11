from __future__ import annotations

import json
import time
from datetime import datetime, timezone

import pytest

from script.storage import LocalFilesystemStorage


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


def test_storage_persists_request_metadata(tmp_path):
    storage = LocalFilesystemStorage(tmp_path / "uploads")
    stored = storage.save_image(
        b"\xff\xd8jpeg",
        "image/jpeg",
        device_id="esp32cam-front-door",
        captured_at=datetime(2026, 3, 11, 12, 34, 56, tzinfo=timezone.utc),
    )

    metadata_path = (tmp_path / "uploads" / stored.name).with_suffix(".jpg.json")
    files = storage.list_images()
    payload = json.loads(metadata_path.read_text(encoding="utf-8"))

    assert metadata_path.exists()
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

    assert list((tmp_path / "uploads").iterdir()) == []


def test_storage_lists_legacy_images_without_metadata(tmp_path):
    upload_dir = tmp_path / "uploads"
    storage = LocalFilesystemStorage(upload_dir)
    legacy_path = upload_dir / "legacy.jpg"
    legacy_path.write_bytes(b"\xff\xd8legacy")

    files = storage.list_images()

    assert files[0].name == legacy_path.name
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
