from __future__ import annotations

from datetime import datetime, timezone

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


def test_create_app_indexes_preexisting_images_with_their_stored_metadata(tmp_path):
    upload_dir = tmp_path / "uploads"
    # Pre-populate storage before the app (and its metadata index) exist, to
    # simulate images left on disk by a prior process run.
    pre_seed_storage = LocalFilesystemStorage(upload_dir)
    stored = pre_seed_storage.save_image(
        MINIMAL_JPEG,
        "image/jpeg",
        device_id="esp32cam-preexisting",
        captured_at=datetime(2026, 3, 11, 12, 0, tzinfo=timezone.utc),
    )

    app = create_app(
        {
            "TESTING": True,
            "UPLOAD_DIR": upload_dir,
            "DB_PATH": tmp_path / "esp32cam.sqlite3",
            "INFERENCE_MODE": "sync",
        }
    )
    client = app.test_client()

    response = client.get("/api/cameras/esp32cam-preexisting/snapshots")

    assert response.status_code == 200
    snapshots = response.get_json()["snapshots"]
    assert len(snapshots) == 1
    assert snapshots[0]["filename"] == stored.name
    assert snapshots[0]["captured_at"] == "2026-03-11T12:00:00+00:00"


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
