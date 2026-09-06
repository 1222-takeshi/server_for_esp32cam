from __future__ import annotations

from script.app import create_app
from script.storage import LocalFilesystemStorage
from tests.conftest import MINIMAL_JPEG


UPLOAD_HEADERS = {
    "Content-Type": "image/jpeg",
    "X-Device-Id": "esp32cam-smoke-test",
    "X-Captured-At": "2026-03-11T12:34:56Z",
}


def test_server_entrypoint_app_serves_index(client):
    response = client.get("/")

    assert response.status_code == 200


def test_upload_requires_request_body(client):
    response = client.post("/upload", data=b"", headers=UPLOAD_HEADERS)

    assert response.status_code == 400
    assert response.get_json()["message"] == "No data received"


def test_upload_rejects_non_jpeg_content_type(client):
    response = client.post(
        "/upload",
        data=b"not an image",
        headers={
            "Content-Type": "text/plain",
            "X-Device-Id": "esp32cam-smoke-test",
            "X-Captured-At": "2026-03-11T12:34:56Z",
        },
    )

    assert response.status_code == 415
    assert response.get_json()["message"] == "Unsupported content type"


def test_upload_accepts_raw_jpeg_payload(client):
    payload = MINIMAL_JPEG

    response = client.post("/upload", data=payload, headers=UPLOAD_HEADERS)

    assert response.status_code == 200
    body = response.get_json()
    assert body["message"] == "File uploaded successfully"
    assert body["device_id"] == "esp32cam-smoke-test"
    assert body["size"] == len(payload)
    assert body["filename"].endswith(".jpg")


def test_upload_marks_inference_skipped_when_detector_is_disabled(client):
    payload = MINIMAL_JPEG

    response = client.post("/upload", data=payload, headers=UPLOAD_HEADERS)

    assert response.status_code == 200

    snapshots_response = client.get("/api/cameras/esp32cam-smoke-test/snapshots")
    assert snapshots_response.status_code == 200
    snapshots = snapshots_response.get_json()["snapshots"]
    assert snapshots[0]["inference_status"] == "skipped"
    assert snapshots[0]["inference_error"] == "Detector is disabled"

    files_response = client.get("/files")
    assert files_response.status_code == 200
    assert b"Inference: skipped" in files_response.data


def test_startup_index_sync_does_not_stat_already_indexed_files(tmp_path, monkeypatch):
    # #383: sync_storage_index()'s startup rglob() used to path.stat() every
    # on-disk file every boot, even files already recorded in SQLite from a
    # prior run. Boot the app twice against the same upload dir/DB: the
    # second boot must not stat() the files the first boot already indexed,
    # only genuinely new ones.
    config = {
        "TESTING": True,
        "UPLOAD_DIR": tmp_path / "uploads",
        "DB_PATH": tmp_path / "esp32cam.sqlite3",
        "INFERENCE_MODE": "sync",
        "RATELIMIT_ENABLED": False,
    }

    first_app = create_app(config)
    first_client = first_app.test_client()
    for i in range(3):
        first_client.post(
            "/upload",
            data=MINIMAL_JPEG,
            headers={
                "Content-Type": "image/jpeg",
                "X-Device-Id": "esp32cam-startup-test",
                "X-Captured-At": f"2026-03-11T12:0{i}:00Z",
            },
        )

    # Drop a genuinely new (legacy, sidecar-less) file directly on disk, the
    # way a pre-existing deployment's files would show up before ever being
    # indexed.
    legacy_path = tmp_path / "uploads" / "legacy.jpg"
    legacy_path.write_bytes(MINIMAL_JPEG)

    stat_calls: list[str] = []
    original = LocalFilesystemStorage._stored_image_fields

    def tracking(self, path, **kwargs):
        stat_calls.append(path.name)
        return original(self, path, **kwargs)

    monkeypatch.setattr(LocalFilesystemStorage, "_stored_image_fields", tracking)

    create_app(config)

    assert stat_calls == ["legacy.jpg"]


def test_upload_marks_inference_failed_when_detector_configuration_is_invalid(tmp_path):
    app = create_app(
        {
            "TESTING": True,
            "UPLOAD_DIR": tmp_path / "uploads",
            "DB_PATH": tmp_path / "esp32cam.sqlite3",
            "INFERENCE_MODE": "sync",
            "DETECTOR": "unsupported_detector",
        }
    )
    client = app.test_client()
    payload = MINIMAL_JPEG

    response = client.post("/upload", data=payload, headers=UPLOAD_HEADERS)

    assert response.status_code == 200
    snapshots_response = client.get("/api/cameras/esp32cam-smoke-test/snapshots")
    assert snapshots_response.status_code == 200
    snapshots = snapshots_response.get_json()["snapshots"]
    assert snapshots[0]["inference_status"] == "failed"
    assert snapshots[0]["inference_error"] == "Unsupported detector: unsupported_detector"
