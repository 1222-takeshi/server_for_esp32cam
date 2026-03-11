from __future__ import annotations

import time

import pytest

from script.storage import LocalFilesystemStorage


def test_storage_creates_upload_directory(tmp_path):
    upload_dir = tmp_path / "uploads"

    LocalFilesystemStorage(upload_dir)

    assert upload_dir.exists()
    assert upload_dir.is_dir()


def test_storage_generates_unique_names(tmp_path):
    storage = LocalFilesystemStorage(tmp_path / "uploads")

    first = storage.save_image(b"a", "image/jpeg")
    second = storage.save_image(b"b", "image/jpeg")

    assert first.name != second.name


def test_storage_lists_latest_first(tmp_path):
    storage = LocalFilesystemStorage(tmp_path / "uploads")
    first = storage.save_image(b"a", "image/jpeg")
    time.sleep(0.01)
    second = storage.save_image(b"b", "image/jpeg")

    files = storage.list_images()

    assert [file.name for file in files] == [second.name, first.name]


def test_storage_rejects_traversal(tmp_path):
    storage = LocalFilesystemStorage(tmp_path / "uploads")

    with pytest.raises(FileNotFoundError):
        storage.read_image("../secret.jpg")


def test_storage_lists_only_jpeg_files(tmp_path):
    storage = LocalFilesystemStorage(tmp_path / "uploads")
    storage.save_image(b"\xff\xd8jpeg", "image/jpeg")
    (tmp_path / "uploads" / "notes.txt").write_text("ignore me")

    files = storage.list_images()

    assert len(files) == 1
