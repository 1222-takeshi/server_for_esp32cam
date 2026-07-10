from __future__ import annotations

import pytest

from script.app import create_app
from tests.test_routes import MINIMAL_JPEG, UPLOAD_HEADERS


@pytest.fixture()
def throttled_app(tmp_path):
    return create_app(
        {
            "TESTING": True,
            "UPLOAD_DIR": tmp_path / "uploads",
            "DB_PATH": tmp_path / "esp32cam.sqlite3",
            "INFERENCE_MODE": "sync",
            "RATELIMIT_ENABLED": False,
            "STORAGE_RETENTION_MAX_BYTES": 1,
            "STORAGE_RETENTION_MIN_INTERVAL_SECONDS": 3600,
        }
    )


class _CountingStorage:
    """Wraps the real storage and counts apply_retention scans."""

    def __init__(self, inner):
        self._inner = inner
        self.scan_count = 0

    def apply_retention(self, *args, **kwargs):
        self.scan_count += 1
        return self._inner.apply_retention(*args, **kwargs)

    def __getattr__(self, name):
        return getattr(self._inner, name)


def _upload(client) -> None:
    response = client.post("/upload", data=MINIMAL_JPEG, headers=UPLOAD_HEADERS)
    assert response.status_code == 200


def test_retention_scan_runs_at_most_once_per_interval(throttled_app):
    counting = _CountingStorage(throttled_app.extensions["storage"])
    throttled_app.extensions["storage"] = counting
    client = throttled_app.test_client()

    _upload(client)
    _upload(client)
    _upload(client)

    assert counting.scan_count == 1


def test_retention_scan_runs_every_upload_when_interval_zero(tmp_path):
    app = create_app(
        {
            "TESTING": True,
            "UPLOAD_DIR": tmp_path / "uploads",
            "DB_PATH": tmp_path / "esp32cam.sqlite3",
            "INFERENCE_MODE": "sync",
            "RATELIMIT_ENABLED": False,
            "STORAGE_RETENTION_MAX_BYTES": 1,
            "STORAGE_RETENTION_MIN_INTERVAL_SECONDS": 0,
        }
    )
    counting = _CountingStorage(app.extensions["storage"])
    app.extensions["storage"] = counting
    client = app.test_client()

    _upload(client)
    _upload(client)

    assert counting.scan_count == 2
