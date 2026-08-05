from __future__ import annotations

import pytest

from script import config as config_module
from script.auth import check_device_api_key, check_device_api_key_for_device
from script.app import create_app
from tests.conftest import MINIMAL_JPEG


@pytest.fixture()
def app_with_device_key(tmp_path):
    return create_app(
        {
            "TESTING": True,
            "UPLOAD_DIR": tmp_path / "uploads",
            "DB_PATH": tmp_path / "esp32cam.sqlite3",
            "INFERENCE_MODE": "sync",
            "DEVICE_API_KEY": "test-device-key",
            "SHARED_KEY_DEVICE_ID": "cam-a",
        }
    )


@pytest.fixture()
def app_with_admin_token(tmp_path):
    return create_app(
        {
            "TESTING": True,
            "UPLOAD_DIR": tmp_path / "uploads",
            "DB_PATH": tmp_path / "esp32cam.sqlite3",
            "INFERENCE_MODE": "sync",
            "ADMIN_TOKEN": "test-admin-token",
        }
    )


# --- Device API key (upload) ---

def test_upload_requires_device_key_when_configured(app_with_device_key):
    client = app_with_device_key.test_client()
    response = client.post(
        "/upload",
        data=b"\xff\xd8fake",
        headers={"Content-Type": "image/jpeg", "X-Captured-At": "2026-01-01T00:00:00Z"},
    )
    assert response.status_code == 401
    assert response.get_json()["message"] == "Unauthorized"


def test_upload_accepts_correct_device_key(app_with_device_key):
    client = app_with_device_key.test_client()
    response = client.post(
        "/upload",
        data=b"\xff\xd8fake",
        headers={
            "Content-Type": "image/jpeg",
            "X-Captured-At": "2026-01-01T00:00:00Z",
            "X-Device-Id": "cam-a",
            "X-API-Key": "test-device-key",
        },
    )
    # Auth passes — next validation is JPEG check (400), not 401
    assert response.status_code != 401


def test_shared_key_rejects_different_device_id(app_with_device_key):
    client = app_with_device_key.test_client()
    response = client.post(
        "/upload",
        data=MINIMAL_JPEG,
        headers={
            "Content-Type": "image/jpeg",
            "X-Captured-At": "2026-01-01T00:00:00Z",
            "X-Device-Id": "cam-b",
            "X-API-Key": "test-device-key",
        },
    )

    assert response.status_code == 403


def test_shared_key_without_bound_device_id_fails_closed(tmp_path):
    app = create_app(
        {
            "TESTING": True,
            "UPLOAD_DIR": tmp_path / "uploads",
            "DB_PATH": tmp_path / "esp32cam.sqlite3",
            "INFERENCE_MODE": "sync",
            "DEVICE_API_KEY": "test-device-key",
            "RATELIMIT_ENABLED": False,
        }
    )
    response = app.test_client().post(
        "/upload",
        data=MINIMAL_JPEG,
        headers={
            "Content-Type": "image/jpeg",
            "X-Captured-At": "2026-01-01T00:00:00Z",
            "X-Device-Id": "cam-a",
            "X-API-Key": "test-device-key",
        },
    )

    assert response.status_code == 403


def test_per_device_keys_take_precedence_over_shared_key(tmp_path):
    app = create_app(
        {
            "TESTING": True,
            "UPLOAD_DIR": tmp_path / "uploads",
            "DB_PATH": tmp_path / "esp32cam.sqlite3",
            "INFERENCE_MODE": "sync",
            "DEVICE_API_KEY": "shared-key",
            "SHARED_KEY_DEVICE_ID": "cam-a",
            "DEVICE_API_KEYS": {"cam-a": "per-device-key"},
        }
    )

    with app.app_context():
        assert not check_device_api_key_for_device("cam-a", "shared-key")
        assert check_device_api_key_for_device("cam-a", "per-device-key")


# --- check_device_api_key() coarse pre-check / value-set cache (#205 / #226) ---


def test_check_device_api_key_accepts_any_configured_per_device_key(tmp_path):
    app = create_app(
        {
            "TESTING": True,
            "UPLOAD_DIR": tmp_path / "uploads",
            "DB_PATH": tmp_path / "esp32cam.sqlite3",
            "INFERENCE_MODE": "sync",
            "DEVICE_API_KEYS": {"cam-a": "key-a", "cam-b": "key-b"},
        }
    )
    with app.app_context():
        assert check_device_api_key("key-a")
        assert check_device_api_key("key-b")
        assert not check_device_api_key("key-c")
        assert not check_device_api_key(None)
        assert not check_device_api_key("")


def test_check_device_api_key_cache_survives_repeated_calls(tmp_path):
    # Regression guard for the frozenset cache: calling check_device_api_key()
    # multiple times against the same DEVICE_API_KEYS dict object must keep
    # returning results consistent with that dict, not a stale snapshot.
    app = create_app(
        {
            "TESTING": True,
            "UPLOAD_DIR": tmp_path / "uploads",
            "DB_PATH": tmp_path / "esp32cam.sqlite3",
            "INFERENCE_MODE": "sync",
            "DEVICE_API_KEYS": {"cam-a": "key-a"},
        }
    )
    with app.app_context():
        for _ in range(3):
            assert check_device_api_key("key-a")
            assert not check_device_api_key("key-b")


def test_check_device_api_key_cache_invalidates_on_config_reassignment(tmp_path):
    # DEVICE_API_KEYS can be swapped out for a new dict object after app
    # creation (tests do this directly; see test_remote_control_api.py). The
    # cached value-set must not leak the old dict's keys nor miss the new
    # dict's keys once that reassignment happens.
    app = create_app(
        {
            "TESTING": True,
            "UPLOAD_DIR": tmp_path / "uploads",
            "DB_PATH": tmp_path / "esp32cam.sqlite3",
            "INFERENCE_MODE": "sync",
            "DEVICE_API_KEYS": {"cam-a": "old-key"},
        }
    )
    with app.app_context():
        assert check_device_api_key("old-key")

        app.config["DEVICE_API_KEYS"] = {"cam-a": "new-key"}

        assert check_device_api_key("new-key")
        assert not check_device_api_key("old-key")


def test_upload_rejects_wrong_device_key(app_with_device_key):
    client = app_with_device_key.test_client()
    response = client.post(
        "/upload",
        data=b"\xff\xd8fake",
        headers={
            "Content-Type": "image/jpeg",
            "X-Captured-At": "2026-01-01T00:00:00Z",
            "X-API-Key": "wrong-key",
        },
    )
    assert response.status_code == 401


def test_upload_rejects_valid_key_for_different_device(tmp_path):
    app = create_app(
        {
            "TESTING": True,
            "UPLOAD_DIR": tmp_path / "uploads",
            "DB_PATH": tmp_path / "esp32cam.sqlite3",
            "INFERENCE_MODE": "sync",
            "DEVICE_API_KEYS": {"cam-a": "key-a", "cam-b": "key-b"},
            "RATELIMIT_ENABLED": False,
        }
    )
    response = app.test_client().post(
        "/upload",
        data=MINIMAL_JPEG,
        headers={
            "Content-Type": "image/jpeg",
            "X-Captured-At": "2026-01-01T00:00:00Z",
            "X-Device-Id": "cam-a",
            "X-API-Key": "key-b",
        },
    )
    assert response.status_code == 403


def test_device_api_keys_env_rejects_invalid_json(monkeypatch):
    monkeypatch.setenv("ESP32CAM_DEVICE_API_KEYS", "not-json")
    with pytest.raises(ValueError, match="ESP32CAM_DEVICE_API_KEYS"):
        config_module._load_device_api_keys()


def test_device_api_keys_env_rejects_non_object(monkeypatch):
    monkeypatch.setenv("ESP32CAM_DEVICE_API_KEYS", '["key-a"]')
    with pytest.raises(ValueError, match="JSON object"):
        config_module._load_device_api_keys()


def test_device_api_keys_env_rejects_empty_mapping_entries(monkeypatch):
    monkeypatch.setenv("ESP32CAM_DEVICE_API_KEYS", '{"cam-a": ""}')
    with pytest.raises(ValueError, match="empty"):
        config_module._load_device_api_keys()


def test_upload_passes_when_no_key_configured(client):
    # Default fixture has no DEVICE_API_KEY set → auth is disabled
    response = client.post(
        "/upload",
        data=b"\xff\xd8fake",
        headers={"Content-Type": "image/jpeg", "X-Captured-At": "2026-01-01T00:00:00Z"},
    )
    assert response.status_code != 401


# --- Device API key (status_update) ---

def test_status_update_requires_device_key_when_configured(app_with_device_key):
    client = app_with_device_key.test_client()
    response = client.post("/api/status_update", json={"device_id": "cam-a"})
    assert response.status_code == 401


def test_status_update_accepts_correct_device_key(app_with_device_key):
    client = app_with_device_key.test_client()
    response = client.post(
        "/api/status_update",
        json={"device_id": "cam-a"},
        headers={"X-API-Key": "test-device-key"},
    )
    assert response.status_code == 200


def test_status_update_rejects_shared_key_for_different_device(app_with_device_key):
    response = app_with_device_key.test_client().post(
        "/api/status_update",
        json={"device_id": "cam-b"},
        headers={"X-API-Key": "test-device-key"},
    )

    assert response.status_code == 403


def test_status_update_rejects_non_object_or_non_string_device_id(app_with_device_key):
    client = app_with_device_key.test_client()

    assert client.post("/api/status_update", json=["cam-a"], headers={"X-API-Key": "test-device-key"}).status_code == 400
    assert client.post(
        "/api/status_update",
        json={"device_id": 123},
        headers={"X-API-Key": "test-device-key"},
    ).status_code == 400


def test_status_update_rejects_valid_key_for_different_device(tmp_path):
    app = create_app(
        {
            "TESTING": True,
            "UPLOAD_DIR": tmp_path / "uploads",
            "DB_PATH": tmp_path / "esp32cam.sqlite3",
            "INFERENCE_MODE": "sync",
            "DEVICE_API_KEYS": {"cam-a": "key-a", "cam-b": "key-b"},
        }
    )
    response = app.test_client().post(
        "/api/status_update",
        json={"device_id": "cam-a"},
        headers={"X-API-Key": "key-b"},
    )
    assert response.status_code == 403


# --- Device API key (log_error) ---

def test_log_error_requires_device_key_when_configured(app_with_device_key):
    client = app_with_device_key.test_client()
    response = client.post("/api/log_error", json={"device_id": "cam-a", "message": "err"})
    assert response.status_code == 401


def test_log_error_rejects_valid_key_for_different_device(tmp_path):
    app = create_app(
        {
            "TESTING": True,
            "UPLOAD_DIR": tmp_path / "uploads",
            "DB_PATH": tmp_path / "esp32cam.sqlite3",
            "INFERENCE_MODE": "sync",
            "DEVICE_API_KEYS": {"cam-a": "key-a", "cam-b": "key-b"},
        }
    )
    response = app.test_client().post(
        "/api/log_error",
        json={"device_id": "cam-a", "message": "err"},
        headers={"X-API-Key": "key-b"},
    )
    assert response.status_code == 403


def test_log_error_rejects_shared_key_for_different_device(app_with_device_key):
    response = app_with_device_key.test_client().post(
        "/api/log_error",
        json={"device_id": "cam-b", "message": "spoofed"},
        headers={"X-API-Key": "test-device-key"},
    )

    assert response.status_code == 403


def test_log_error_rejects_non_string_message(app_with_device_key):
    response = app_with_device_key.test_client().post(
        "/api/log_error",
        json={"device_id": "cam-a", "message": ["spoofed"]},
        headers={"X-API-Key": "test-device-key"},
    )

    assert response.status_code == 400


# --- Admin token (delete camera) ---

def test_delete_camera_requires_admin_token_when_configured(app_with_admin_token):
    with app_with_admin_token.app_context():
        app_with_admin_token.extensions["metadata_store"].record_heartbeat("cam-x")

    client = app_with_admin_token.test_client()
    response = client.delete("/api/cameras/cam-x")
    assert response.status_code == 401
    assert response.headers.get("WWW-Authenticate", "").startswith("Bearer")


def test_delete_camera_accepts_correct_admin_token(app_with_admin_token):
    with app_with_admin_token.app_context():
        app_with_admin_token.extensions["metadata_store"].record_heartbeat("cam-x")

    client = app_with_admin_token.test_client()
    response = client.delete(
        "/api/cameras/cam-x",
        headers={"Authorization": "Bearer test-admin-token"},
    )
    assert response.status_code == 200


def test_delete_camera_rejects_wrong_admin_token(app_with_admin_token):
    client = app_with_admin_token.test_client()
    response = client.delete(
        "/api/cameras/cam-x",
        headers={"Authorization": "Bearer wrong-token"},
    )
    assert response.status_code == 401


# --- Admin token (patch camera) ---

def test_patch_camera_requires_admin_token_when_configured(app_with_admin_token):
    client = app_with_admin_token.test_client()
    response = client.patch("/api/cameras/cam-x", json={"display_name": "New"})
    assert response.status_code == 401


def test_patch_camera_accepts_correct_admin_token(app_with_admin_token):
    with app_with_admin_token.app_context():
        app_with_admin_token.extensions["metadata_store"].record_heartbeat("cam-x")

    client = app_with_admin_token.test_client()
    response = client.patch(
        "/api/cameras/cam-x",
        json={"display_name": "My Camera"},
        headers={"Authorization": "Bearer test-admin-token"},
    )
    assert response.status_code == 200


# --- /api/cameras/<id>/logs protection ---

def test_camera_logs_requires_admin_token_when_configured(app_with_admin_token):
    with app_with_admin_token.app_context():
        app_with_admin_token.extensions["metadata_store"].record_heartbeat("cam-x")

    client = app_with_admin_token.test_client()
    response = client.get("/api/cameras/cam-x/logs")
    assert response.status_code == 401


def test_camera_logs_accepts_correct_admin_token(app_with_admin_token):
    with app_with_admin_token.app_context():
        app_with_admin_token.extensions["metadata_store"].record_heartbeat("cam-x")

    client = app_with_admin_token.test_client()
    response = client.get(
        "/api/cameras/cam-x/logs",
        headers={"Authorization": "Bearer test-admin-token"},
    )
    assert response.status_code == 200


# --- validate_auth_config ---

def test_validate_auth_config_strips_leading_trailing_whitespace(tmp_path):
    app = create_app(
        {
            "TESTING": True,
            "UPLOAD_DIR": tmp_path / "uploads",
            "DB_PATH": tmp_path / "esp32cam.sqlite3",
            "INFERENCE_MODE": "sync",
            "DEVICE_API_KEY": "  my-key  ",
        }
    )
    assert app.config["DEVICE_API_KEY"] == "my-key"


def test_validate_auth_config_rejects_whitespace_only_key(tmp_path):
    with pytest.raises(ValueError, match="DEVICE_API_KEY"):
        create_app(
            {
                "TESTING": True,
                "UPLOAD_DIR": tmp_path / "uploads",
                "DB_PATH": tmp_path / "esp32cam.sqlite3",
                "INFERENCE_MODE": "sync",
                "DEVICE_API_KEY": "   ",
            }
        )
