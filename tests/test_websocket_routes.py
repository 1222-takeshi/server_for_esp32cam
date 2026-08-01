from __future__ import annotations

from contextlib import contextmanager
import json
from threading import Thread

import pytest
from simple_websocket import Client
from simple_websocket.errors import ConnectionClosed
from werkzeug.serving import make_server

from script.app import create_app
from script.websocket_routes import _check_camera_upload_auth


@contextmanager
def _running_websocket_server(app):
    server = make_server("127.0.0.1", 0, app, threaded=True)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"ws://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def _receive_json_message(
    url: str, *, api_key: str | None, send: str | None = None
) -> dict[str, object]:
    headers = {"X-API-Key": api_key} if api_key is not None else {}
    websocket = Client(url, headers=headers)
    try:
        if send is not None:
            websocket.send(send)
        return json.loads(websocket.receive())
    finally:
        try:
            websocket.close()
        except (ConnectionClosed, OSError):
            pass


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


def test_camera_upload_shared_key_is_bound_to_configured_device(tmp_path):
    app = create_app(
        {
            "TESTING": True,
            "UPLOAD_DIR": tmp_path / "uploads",
            "DB_PATH": tmp_path / "esp32cam.sqlite3",
            "INFERENCE_MODE": "sync",
            "DEVICE_API_KEY": "shared-key",
            "SHARED_KEY_DEVICE_ID": "cam-a",
            "RATELIMIT_ENABLED": False,
        }
    )

    with app.app_context():
        own = _check_camera_upload_auth("cam-a", "shared-key")
        other = _check_camera_upload_auth("cam-b", "shared-key")

    assert own is None
    assert other == {"type": "error", "message": "Forbidden"}


def test_registered_camera_upload_validates_camera_id_before_auth(app_with_per_device_keys):
    with _running_websocket_server(app_with_per_device_keys) as base_url:
        response = _receive_json_message(
            f"{base_url}/ws/cameras/bad%24id/upload",
            api_key="key-a",
        )

    assert response == {"type": "error", "message": "Invalid camera_id"}


def test_registered_camera_upload_rejects_wrong_key(app_with_per_device_keys):
    with _running_websocket_server(app_with_per_device_keys) as base_url:
        response = _receive_json_message(
            f"{base_url}/ws/cameras/cam-b/upload",
            api_key="key-a",
        )

    assert response == {"type": "error", "message": "Forbidden"}


def test_registered_camera_upload_rejects_missing_key(app_with_per_device_keys):
    with _running_websocket_server(app_with_per_device_keys) as base_url:
        response = _receive_json_message(
            f"{base_url}/ws/cameras/cam-a/upload",
            api_key=None,
        )

    assert response == {"type": "error", "message": "Unauthorized"}


def test_registered_camera_upload_reports_connection_limit(tmp_path):
    app = create_app(
        {
            "TESTING": True,
            "UPLOAD_DIR": tmp_path / "uploads",
            "DB_PATH": tmp_path / "esp32cam.sqlite3",
            "INFERENCE_MODE": "sync",
            "DEVICE_API_KEYS": {"cam-a": "key-a"},
            "MAX_WS_CONNECTIONS": 0,
            "RATELIMIT_ENABLED": False,
        }
    )

    with _running_websocket_server(app) as base_url:
        response = _receive_json_message(
            f"{base_url}/ws/cameras/cam-a/upload",
            api_key="key-a",
        )

    assert response == {"type": "error", "message": "Too many connections"}


def test_registered_camera_upload_shared_key_allows_only_bound_device(tmp_path):
    app = create_app(
        {
            "TESTING": True,
            "UPLOAD_DIR": tmp_path / "uploads",
            "DB_PATH": tmp_path / "esp32cam.sqlite3",
            "INFERENCE_MODE": "sync",
            "DEVICE_API_KEY": "shared-key",
            "SHARED_KEY_DEVICE_ID": "cam-a",
            "RATELIMIT_ENABLED": False,
        }
    )

    with _running_websocket_server(app) as base_url:
        own = _receive_json_message(
            f"{base_url}/ws/cameras/cam-a/upload",
            api_key="shared-key",
            send="not-binary",
        )
        other = _receive_json_message(
            f"{base_url}/ws/cameras/cam-b/upload",
            api_key="shared-key",
        )

    assert own == {"type": "snapshot.error", "message": "Expected binary JPEG payload"}
    assert other == {"type": "error", "message": "Forbidden"}
