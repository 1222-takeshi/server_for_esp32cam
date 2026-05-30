from __future__ import annotations

import logging

import pytest

from script import config as config_module
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
            "X-Device-Id": "cam-test",
            "X-API-Key": "test-device-key",
        },
    )
    # Auth passes — next validation is JPEG check (400), not 401
    assert response.status_code != 401


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


def test_validate_auth_config_treats_whitespace_only_as_disabled(tmp_path, caplog):
    with caplog.at_level(logging.WARNING, logger="script.auth"):
        app = create_app(
            {
                "TESTING": True,
                "UPLOAD_DIR": tmp_path / "uploads",
                "DB_PATH": tmp_path / "esp32cam.sqlite3",
                "INFERENCE_MODE": "sync",
                "DEVICE_API_KEY": "   ",
            }
        )
    assert app.config["DEVICE_API_KEY"] == ""
    assert any("DEVICE_API_KEY" in r.message for r in caplog.records)
