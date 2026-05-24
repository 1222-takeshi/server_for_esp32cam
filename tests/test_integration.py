"""E2E integration scenarios covering multi-camera workflows and retention."""
from __future__ import annotations

import pytest

from script.app import create_app


@pytest.fixture()
def app(tmp_path):
    return create_app(
        {
            "TESTING": True,
            "UPLOAD_DIR": tmp_path / "uploads",
            "DB_PATH": tmp_path / "esp32cam.sqlite3",
            "INFERENCE_MODE": "sync",
        }
    )


@pytest.fixture()
def client(app):
    return app.test_client()


@pytest.fixture()
def retention_app(tmp_path):
    return create_app(
        {
            "TESTING": True,
            "UPLOAD_DIR": tmp_path / "uploads",
            "DB_PATH": tmp_path / "esp32cam.sqlite3",
            "INFERENCE_MODE": "sync",
            "STORAGE_RETENTION_MAX_BYTES": 200,
        }
    )


@pytest.fixture()
def retention_client(retention_app):
    return retention_app.test_client()


def _upload(client, *, device_id: str, captured_at: str, body: bytes = b"\xff\xd8img"):
    return client.post(
        "/upload",
        data=body,
        headers={
            "Content-Type": "image/jpeg",
            "X-Device-Id": device_id,
            "X-Captured-At": captured_at,
        },
    )


# ---------------------------------------------------------------------------
# Scenario 1: multiple cameras upload without metadata collision
# ---------------------------------------------------------------------------

def test_multi_camera_uploads_are_isolated(client):
    """Uploads from different cameras produce correctly scoped metadata."""
    _upload(client, device_id="cam-front", captured_at="2026-03-11T10:00:00Z")
    _upload(client, device_id="cam-garden", captured_at="2026-03-11T10:01:00Z")
    _upload(client, device_id="cam-front", captured_at="2026-03-11T10:02:00Z")

    cameras = client.get("/api/cameras").get_json()["cameras"]
    camera_ids = {c["camera_id"] for c in cameras}
    assert camera_ids == {"cam-front", "cam-garden"}

    front_snaps = client.get("/api/cameras/cam-front/snapshots").get_json()
    assert len(front_snaps["snapshots"]) == 2

    garden_snaps = client.get("/api/cameras/cam-garden/snapshots").get_json()
    assert len(garden_snaps["snapshots"]) == 1


def test_multi_camera_uploads_total_count(client):
    """Total snapshot count equals sum of all cameras' uploads."""
    _upload(client, device_id="cam-a", captured_at="2026-03-11T10:00:00Z")
    _upload(client, device_id="cam-b", captured_at="2026-03-11T10:01:00Z")
    _upload(client, device_id="cam-c", captured_at="2026-03-11T10:02:00Z")

    response = client.get("/files")
    assert response.status_code == 200
    page = response.get_data(as_text=True)
    assert "cam-a" in page
    assert "cam-b" in page
    assert "cam-c" in page


# ---------------------------------------------------------------------------
# Scenario 2: camera delete → re-register → upload consistency
# ---------------------------------------------------------------------------

def _camera_ids(client) -> set[str]:
    return {c["camera_id"] for c in client.get("/api/cameras").get_json()["cameras"]}


def test_camera_delete_and_reregister(client):
    """After deleting a camera and re-uploading, a fresh camera record is created."""
    _upload(client, device_id="cam-ephemeral", captured_at="2026-03-11T09:00:00Z")
    assert "cam-ephemeral" in _camera_ids(client)

    delete_resp = client.delete("/api/cameras/cam-ephemeral")
    assert delete_resp.status_code == 200

    assert "cam-ephemeral" not in _camera_ids(client)
    assert client.get("/api/cameras/cam-ephemeral/snapshots").status_code == 404

    _upload(client, device_id="cam-ephemeral", captured_at="2026-03-11T10:00:00Z")
    assert "cam-ephemeral" in _camera_ids(client)

    snaps = client.get("/api/cameras/cam-ephemeral/snapshots").get_json()
    assert len(snaps["snapshots"]) == 1


def test_camera_delete_cascades_snapshots_then_reupload(client):
    """Snapshots deleted with camera don't bleed into the re-registered camera."""
    for i in range(3):
        _upload(
            client,
            device_id="cam-multi",
            captured_at=f"2026-03-11T10:0{i}:00Z",
        )
    assert len(client.get("/api/cameras/cam-multi/snapshots").get_json()["snapshots"]) == 3

    client.delete("/api/cameras/cam-multi?delete_files=false")

    _upload(client, device_id="cam-multi", captured_at="2026-03-11T11:00:00Z")
    snaps = client.get("/api/cameras/cam-multi/snapshots").get_json()
    assert len(snaps["snapshots"]) == 1


# ---------------------------------------------------------------------------
# Scenario 3: retention policy deletes old files, protects latest upload
# ---------------------------------------------------------------------------

def test_retention_deletes_old_images_but_protects_latest(retention_client):
    """When the byte limit is exceeded, old files are deleted; the just-uploaded file survives."""
    resp1 = _upload(
        retention_client,
        device_id="cam-ret",
        captured_at="2026-01-01T00:00:00Z",
        body=b"\xff\xd8" + b"x" * 110,
    )
    assert resp1.status_code == 200
    snap1 = resp1.get_json()

    resp2 = _upload(
        retention_client,
        device_id="cam-ret",
        captured_at="2026-01-01T01:00:00Z",
        body=b"\xff\xd8" + b"y" * 110,
    )
    assert resp2.status_code == 200
    snap2 = resp2.get_json()

    # The newly uploaded file must still be retrievable (retention protects it)
    assert retention_client.get(f"/files/{snap2['filename']}").status_code == 200

    # The older file should have been evicted by the retention policy
    assert retention_client.get(f"/files/{snap1['filename']}").status_code == 404


def test_retention_does_not_delete_when_under_limit(retention_client):
    """A single small upload under the byte limit triggers no deletion."""
    resp = _upload(
        retention_client,
        device_id="cam-small",
        captured_at="2026-03-11T10:00:00Z",
        body=b"\xff\xd8" + b"z" * 10,
    )
    snap = resp.get_json()

    assert retention_client.get(f"/files/{snap['filename']}").status_code == 200
    snaps = retention_client.get("/api/cameras/cam-small/snapshots").get_json()
    assert len(snaps["snapshots"]) == 1
