"""Contract tests: verify Flask routes match OpenAPI 3.1 spec.

Each test asserts:
  - The route exists (not 404 due to routing)
  - The response Content-Type is application/json
  - The response body contains the keys defined in the spec schema

Authentication is tested separately from schema shape.
"""
from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from tests.conftest import MINIMAL_JPEG

SPEC_PATH = Path(__file__).resolve().parent.parent / "docs" / "openapi.yaml"


@pytest.fixture(scope="session")
def openapi_spec() -> dict:
    return yaml.safe_load(SPEC_PATH.read_text(encoding="utf-8"))


def _is_json(response) -> bool:
    return "application/json" in response.content_type


# ---------------------------------------------------------------------------
# Health endpoints
# ---------------------------------------------------------------------------


def test_contract_healthz_returns_status_ok(client):
    r = client.get("/healthz")
    assert r.status_code == 200
    assert _is_json(r)
    body = r.get_json()
    assert "status" in body
    assert body["status"] == "ok"


def test_contract_healthz_ready_returns_status_field(client):
    r = client.get("/healthz/ready")
    assert r.status_code in (200, 503)
    assert _is_json(r)
    body = r.get_json()
    assert "status" in body


# ---------------------------------------------------------------------------
# Upload endpoint
# ---------------------------------------------------------------------------


def test_contract_upload_success_has_message(client):
    r = client.post(
        "/upload",
        data=MINIMAL_JPEG,
        content_type="image/jpeg",
        headers={"X-Device-Id": "contract-cam", "X-Captured-At": "2026-01-01T00:00:00Z"},
    )
    assert r.status_code == 200
    assert _is_json(r)
    body = r.get_json()
    assert "message" in body or "snapshot_id" in body


def test_contract_upload_invalid_jpeg_returns_400(client):
    r = client.post(
        "/upload",
        data=b"not-a-jpeg",
        content_type="image/jpeg",
        headers={"X-Device-Id": "contract-cam", "X-Captured-At": "2026-01-01T00:00:00Z"},
    )
    assert r.status_code == 400
    assert _is_json(r)
    body = r.get_json()
    assert "message" in body


def test_contract_upload_wrong_content_type_returns_4xx(client):
    r = client.post(
        "/upload",
        data=MINIMAL_JPEG,
        content_type="application/octet-stream",
        headers={"X-Device-Id": "contract-cam", "X-Captured-At": "2026-01-01T00:00:00Z"},
    )
    assert r.status_code in (400, 415)
    assert _is_json(r)


def test_contract_upload_with_api_key_set_rejects_missing_key(app):
    app.config["DEVICE_API_KEY"] = "secret-key"
    client = app.test_client()
    r = client.post(
        "/upload",
        data=MINIMAL_JPEG,
        content_type="image/jpeg",
        headers={"X-Device-Id": "contract-cam", "X-Captured-At": "2026-01-01T00:00:00Z"},
    )
    assert r.status_code == 401
    assert _is_json(r)
    body = r.get_json()
    assert "message" in body
    app.config.pop("DEVICE_API_KEY")


# ---------------------------------------------------------------------------
# Cameras list endpoint
# ---------------------------------------------------------------------------


def test_contract_cameras_list_returns_list(client):
    r = client.get("/api/cameras")
    assert r.status_code == 200
    assert _is_json(r)
    body = r.get_json()
    assert "cameras" in body
    assert isinstance(body["cameras"], list)


# ---------------------------------------------------------------------------
# Camera detail endpoints (auth required)
# ---------------------------------------------------------------------------


def test_contract_delete_camera_without_auth_returns_401(app):
    app.config["ADMIN_TOKEN"] = "admin-secret"
    c = app.test_client()
    r = c.delete("/api/cameras/nonexistent")
    assert r.status_code == 401
    assert _is_json(r)
    body = r.get_json()
    assert "message" in body
    app.config.pop("ADMIN_TOKEN")


def test_contract_delete_camera_with_auth_not_found_returns_404(app):
    app.config["ADMIN_TOKEN"] = "admin-secret"
    c = app.test_client()
    r = c.delete(
        "/api/cameras/nonexistent",
        headers={"Authorization": "Bearer admin-secret"},
    )
    assert r.status_code == 404
    assert _is_json(r)
    body = r.get_json()
    assert "message" in body
    app.config.pop("ADMIN_TOKEN")


def test_contract_patch_camera_without_auth_returns_401(app):
    app.config["ADMIN_TOKEN"] = "admin-secret"
    c = app.test_client()
    r = c.patch(
        "/api/cameras/nonexistent",
        json={"display_name": "New Name"},
    )
    assert r.status_code == 401
    body = r.get_json()
    assert "message" in body
    app.config.pop("ADMIN_TOKEN")


def test_contract_patch_camera_with_auth_not_found_returns_404(app):
    app.config["ADMIN_TOKEN"] = "admin-secret"
    c = app.test_client()
    r = c.patch(
        "/api/cameras/nonexistent",
        json={"display_name": "New Name"},
        headers={"Authorization": "Bearer admin-secret"},
    )
    assert r.status_code == 404
    body = r.get_json()
    assert "message" in body
    app.config.pop("ADMIN_TOKEN")


# ---------------------------------------------------------------------------
# Snapshots endpoint
# ---------------------------------------------------------------------------


def test_contract_camera_snapshots_not_found_returns_404(client):
    r = client.get("/api/cameras/no-such-cam/snapshots")
    assert r.status_code == 404
    assert _is_json(r)


def test_contract_camera_snapshots_returns_list_after_upload(client):
    client.post(
        "/upload",
        data=MINIMAL_JPEG,
        content_type="image/jpeg",
        headers={"X-Device-Id": "snap-cam", "X-Captured-At": "2026-01-01T00:00:00Z"},
    )
    r = client.get("/api/cameras/snap-cam/snapshots")
    assert r.status_code == 200
    assert _is_json(r)
    body = r.get_json()
    assert "snapshots" in body
    assert isinstance(body["snapshots"], list)
    assert len(body["snapshots"]) >= 1
    first = body["snapshots"][0]
    assert "snapshot_id" in first or "id" in first


# ---------------------------------------------------------------------------
# Device endpoints (status_update, log_error)
# ---------------------------------------------------------------------------


def test_contract_status_update_success_returns_200(client):
    r = client.post(
        "/api/status_update",
        json={"device_id": "contract-cam", "status": "ok"},
    )
    assert r.status_code == 200
    assert _is_json(r)
    body = r.get_json()
    assert "message" in body


def test_contract_log_error_success_returns_201(client):
    r = client.post(
        "/api/log_error",
        json={"device_id": "contract-cam", "message": "test error message"},
    )
    assert r.status_code == 201
    assert _is_json(r)
    body = r.get_json()
    assert "device_id" in body or "message" in body


# ---------------------------------------------------------------------------
# OpenAPI spec itself is accessible
# ---------------------------------------------------------------------------


def test_contract_openapi_yaml_is_accessible(client):
    r = client.get("/api/openapi.yaml")
    assert r.status_code == 200
    assert "application/yaml" in r.content_type
    body = r.get_data(as_text=True)
    assert "openapi:" in body
    assert "/upload" in body


def test_contract_swagger_ui_is_accessible(client):
    r = client.get("/api/docs")
    assert r.status_code == 200
    assert "text/html" in r.content_type
    assert b"swagger" in r.data.lower()


# ---------------------------------------------------------------------------
# OpenAPI spec file structure validation
# ---------------------------------------------------------------------------


def test_contract_openapi_spec_has_required_paths(openapi_spec):
    paths = set(openapi_spec.get("paths", {}).keys())
    required = {
        "/healthz",
        "/healthz/ready",
        "/upload",
        "/api/cameras",
        "/api/cameras/{camera_id}",
    }
    missing = required - paths
    assert not missing, f"OpenAPI spec missing paths: {missing}"


def test_contract_openapi_spec_upload_has_responses(openapi_spec):
    upload = openapi_spec["paths"]["/upload"]["post"]
    responses = upload["responses"]
    assert "200" in responses
    assert "400" in responses
    assert "401" in responses


def test_contract_openapi_spec_cameras_has_list_response(openapi_spec):
    cameras = openapi_spec["paths"]["/api/cameras"]["get"]
    assert "200" in cameras["responses"]


def test_contract_openapi_spec_camera_id_has_delete_and_patch(openapi_spec):
    cam = openapi_spec["paths"]["/api/cameras/{camera_id}"]
    assert "delete" in cam
    assert "patch" in cam
