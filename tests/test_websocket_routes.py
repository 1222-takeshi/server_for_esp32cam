from __future__ import annotations

import pytest

from script.app import create_app
from script.websocket_routes import _check_camera_upload_auth


def _rule_paths(app) -> set[str]:
    return {rule.rule for rule in app.url_map.iter_rules()}


def test_create_app_registers_websocket_hub_extension(app):
    assert "websocket_hub" in app.extensions


def test_create_app_registers_snapshot_websocket_routes(app):
    rules = _rule_paths(app)

    assert "/ws/snapshots" in rules
    assert "/ws/cameras/<camera_id>/snapshots" in rules
    assert "/ws/cameras/<camera_id>/upload" in rules


# --- Per-device API key enforcement on /ws/cameras/<camera_id>/upload ---
# Regression test for #211: a key valid for one camera must not be usable to
# upload as a different camera_id over the websocket upload route.


@pytest.fixture()
def app_with_per_device_keys(tmp_path):
    return create_app(
        {
            "TESTING": True,
            "UPLOAD_DIR": tmp_path / "uploads",
            "DB_PATH": tmp_path / "esp32cam.sqlite3",
            "INFERENCE_MODE": "sync",
            "DEVICE_API_KEYS": {"cam-a": "key-a", "cam-b": "key-b"},
            "RATELIMIT_ENABLED": False,
        }
    )


def test_camera_upload_rejects_valid_key_for_different_device(app_with_per_device_keys):
    with app_with_per_device_keys.app_context():
        auth_error = _check_camera_upload_auth("cam-b", "key-a")

    assert auth_error == {"type": "error", "message": "Forbidden"}


def test_camera_upload_accepts_own_correct_key(app_with_per_device_keys):
    with app_with_per_device_keys.app_context():
        auth_error = _check_camera_upload_auth("cam-a", "key-a")

    assert auth_error is None


def test_camera_upload_rejects_missing_key_when_configured(app_with_per_device_keys):
    with app_with_per_device_keys.app_context():
        auth_error = _check_camera_upload_auth("cam-a", None)

    assert auth_error == {"type": "error", "message": "Unauthorized"}
