from __future__ import annotations

import pytest

from script.app import create_app


@pytest.fixture()
def app(tmp_path):
    return create_app(
        {
            "TESTING": True,
            "UPLOAD_DIR": tmp_path / "uploads",
        }
    )


@pytest.fixture()
def client(app):
    return app.test_client()
