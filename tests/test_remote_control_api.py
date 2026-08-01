from __future__ import annotations


ADMIN_TOKEN = "admin-token-for-tests"
DEVICE_KEYS = {"living-room-cam": "living-key", "garage-cam": "garage-key"}


def _admin_headers() -> dict[str, str]:
    return {"Authorization": f"Bearer {ADMIN_TOKEN}"}


def _device_headers(camera_id: str = "living-room-cam") -> dict[str, str]:
    return {"X-API-Key": DEVICE_KEYS[camera_id]}


def _enable_remote_auth(app) -> None:
    app.config["ADMIN_TOKEN"] = ADMIN_TOKEN
    app.config["DEVICE_API_KEYS"] = DEVICE_KEYS.copy()


def _valid_config(**overrides: object) -> dict[str, object]:
    config: dict[str, object] = {
        "capture_interval_seconds": 300,
        "frame_size": "SVGA",
        "jpeg_quality": 12,
        "vflip": False,
        "hmirror": True,
        "restart_requested": False,
        "ota_update_requested": False,
        "ota_window_seconds": 120,
    }
    config.update(overrides)
    return config


def test_admin_put_and_get_desired_config(app):
    _enable_remote_auth(app)
    client = app.test_client()

    put = client.put(
        "/api/cameras/living-room-cam/desired-config",
        json={"config": _valid_config()},
        headers=_admin_headers(),
    )

    assert put.status_code == 200
    created = put.get_json()
    assert created["camera_id"] == "living-room-cam"
    assert created["version"] == 1
    assert created["config"]["frame_size"] == "SVGA"
    assert created["last_acknowledged_version"] is None

    get = client.get("/api/cameras/living-room-cam/desired-config", headers=_admin_headers())

    assert get.status_code == 200
    body = get.get_json()
    assert body["version"] == 1
    assert body["config"]["capture_interval_seconds"] == 300
    assert body["updated_at"].endswith("+00:00")


def test_admin_put_rejects_expected_version_conflict(app):
    _enable_remote_auth(app)
    client = app.test_client()
    client.put(
        "/api/cameras/living-room-cam/desired-config",
        json={"config": _valid_config()},
        headers=_admin_headers(),
    )

    conflict = client.put(
        "/api/cameras/living-room-cam/desired-config",
        json={"expected_version": 99, "config": _valid_config(frame_size="VGA")},
        headers=_admin_headers(),
    )

    assert conflict.status_code == 409
    assert "expected_version" in conflict.get_json()["message"]


def test_admin_put_rejects_invalid_or_unknown_config_keys(app):
    _enable_remote_auth(app)
    client = app.test_client()

    invalid = client.put(
        "/api/cameras/living-room-cam/desired-config",
        json={"config": _valid_config(capture_interval_seconds=10)},
        headers=_admin_headers(),
    )
    unknown = client.put(
        "/api/cameras/living-room-cam/desired-config",
        json={"config": {**_valid_config(), "shell_command": "reboot"}},
        headers=_admin_headers(),
    )

    assert invalid.status_code == 400
    assert "capture_interval_seconds" in invalid.get_json()["message"]
    assert unknown.status_code == 400
    assert "shell_command" in unknown.get_json()["message"]


def test_device_get_desired_config_requires_matching_device_key(app):
    _enable_remote_auth(app)
    client = app.test_client()
    client.put(
        "/api/cameras/living-room-cam/desired-config",
        json={"config": _valid_config(frame_size="VGA")},
        headers=_admin_headers(),
    )

    ok = client.get(
        "/api/devices/living-room-cam/desired-config?current_version=0",
        headers=_device_headers("living-room-cam"),
    )
    wrong = client.get(
        "/api/devices/living-room-cam/desired-config?current_version=0",
        headers=_device_headers("garage-cam"),
    )

    assert ok.status_code == 200
    assert ok.get_json()["device_id"] == "living-room-cam"
    assert ok.get_json()["changed"] is True
    assert ok.get_json()["config"]["frame_size"] == "VGA"
    assert wrong.status_code == 403


def test_shared_key_desired_config_is_bound_to_configured_device(app):
    app.config["DEVICE_API_KEY"] = "shared-key"
    app.config["SHARED_KEY_DEVICE_ID"] = "living-room-cam"
    app.config["ADMIN_TOKEN"] = ADMIN_TOKEN
    client = app.test_client()
    client.put(
        "/api/cameras/living-room-cam/desired-config",
        json={"config": _valid_config(frame_size="VGA")},
        headers=_admin_headers(),
    )

    own = client.get(
        "/api/devices/living-room-cam/desired-config?current_version=0",
        headers={"X-API-Key": "shared-key"},
    )
    other = client.get(
        "/api/devices/garage-cam/desired-config?current_version=0",
        headers={"X-API-Key": "shared-key"},
    )

    assert own.status_code == 200
    assert other.status_code == 403


def test_shared_key_desired_config_ack_is_bound_to_configured_device(app):
    app.config["DEVICE_API_KEY"] = "shared-key"
    app.config["SHARED_KEY_DEVICE_ID"] = "living-room-cam"
    app.config["ADMIN_TOKEN"] = ADMIN_TOKEN
    client = app.test_client()
    created = client.put(
        "/api/cameras/living-room-cam/desired-config",
        json={"config": _valid_config()},
        headers=_admin_headers(),
    ).get_json()
    payload = {"version": created["version"], "applied": True}

    own = client.post(
        "/api/devices/living-room-cam/desired-config/ack",
        json=payload,
        headers={"X-API-Key": "shared-key"},
    )
    other = client.post(
        "/api/devices/garage-cam/desired-config/ack",
        json=payload,
        headers={"X-API-Key": "shared-key"},
    )

    assert own.status_code == 200
    assert other.status_code == 403


def test_device_get_desired_config_reports_unchanged_when_version_matches(app):
    _enable_remote_auth(app)
    client = app.test_client()
    created = client.put(
        "/api/cameras/living-room-cam/desired-config",
        json={"config": _valid_config()},
        headers=_admin_headers(),
    ).get_json()

    response = client.get(
        f"/api/devices/living-room-cam/desired-config?current_version={created['version']}",
        headers=_device_headers("living-room-cam"),
    )

    assert response.status_code == 200
    assert response.get_json()["changed"] is False


def test_device_ack_records_result_and_clears_one_shot_requests(app):
    _enable_remote_auth(app)
    client = app.test_client()
    created = client.put(
        "/api/cameras/living-room-cam/desired-config",
        json={"config": _valid_config(restart_requested=True, ota_update_requested=True)},
        headers=_admin_headers(),
    ).get_json()

    ack = client.post(
        "/api/devices/living-room-cam/desired-config/ack",
        json={
            "version": created["version"],
            "applied": False,
            "message": "ota timeout",
            "firmware_version": "0.3.0",
            "free_heap": 123456,
        },
        headers=_device_headers("living-room-cam"),
    )
    admin_get = client.get("/api/cameras/living-room-cam/desired-config", headers=_admin_headers())

    assert ack.status_code == 200
    ack_body = ack.get_json()
    assert ack_body["last_acknowledged_version"] == created["version"]
    assert ack_body["last_acknowledged_success"] is False
    assert ack_body["config"]["restart_requested"] is False
    assert ack_body["config"]["ota_update_requested"] is False
    assert admin_get.get_json()["last_acknowledged_message"] == "ota timeout"


def test_device_ack_rejects_stale_version_without_clearing_one_shot_requests(app):
    _enable_remote_auth(app)
    client = app.test_client()
    created = client.put(
        "/api/cameras/living-room-cam/desired-config",
        json={"config": _valid_config(restart_requested=True, ota_update_requested=True)},
        headers=_admin_headers(),
    ).get_json()

    stale = client.post(
        "/api/devices/living-room-cam/desired-config/ack",
        json={"version": created["version"] + 1, "applied": True, "message": "wrong version"},
        headers=_device_headers("living-room-cam"),
    )
    admin_get = client.get("/api/cameras/living-room-cam/desired-config", headers=_admin_headers()).get_json()

    assert stale.status_code == 409
    assert stale.get_json()["current_version"] == created["version"]
    assert admin_get["last_acknowledged_version"] is None
    assert admin_get["config"]["restart_requested"] is True
    assert admin_get["config"]["ota_update_requested"] is True


def test_remote_control_endpoints_require_auth(app):
    _enable_remote_auth(app)
    client = app.test_client()

    assert client.get("/api/cameras/living-room-cam/desired-config").status_code == 401
    assert client.put("/api/cameras/living-room-cam/desired-config", json={"config": _valid_config()}).status_code == 401
    assert client.get("/api/devices/living-room-cam/desired-config").status_code == 401
    assert client.post("/api/devices/living-room-cam/desired-config/ack", json={"version": 1, "applied": True}).status_code == 401
