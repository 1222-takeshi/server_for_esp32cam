from __future__ import annotations

from io import BytesIO

import pytest
from PIL import Image

from script.app import create_app


def _make_minimal_jpeg(width: int = 4, height: int = 4) -> bytes:
    buf = BytesIO()
    Image.new("RGB", (width, height), color=(128, 128, 128)).save(buf, format="JPEG")
    return buf.getvalue()


MINIMAL_JPEG = _make_minimal_jpeg()


@pytest.fixture()
def app(tmp_path):
    return create_app(
        {
            "TESTING": True,
            "UPLOAD_DIR": tmp_path / "uploads",
            "DB_PATH": tmp_path / "esp32cam.sqlite3",
            "INFERENCE_MODE": "sync",
            "RATELIMIT_ENABLED": False,
        }
    )


@pytest.fixture()
def client(app):
    return app.test_client()
