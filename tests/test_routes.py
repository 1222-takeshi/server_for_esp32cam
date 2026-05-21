from __future__ import annotations

from datetime import datetime
import time

UPLOAD_HEADERS = {
    "Content-Type": "image/jpeg",
    "X-Device-Id": "esp32cam-front-door",
    "X-Captured-At": "2026-03-11T12:34:56Z",
}


class FakePersonDetector:
    def __init__(self, detections):
        self._detections = detections

    def detect(self, _image_bytes: bytes):
        return self._detections


class BrokenDetector:
    def detect(self, _image_bytes: bytes):
        raise RuntimeError("model unavailable")


def test_healthz_returns_ok(client):
    response = client.get("/healthz")

    assert response.status_code == 200
    assert response.get_json() == {"status": "ok"}


def test_upload_accepts_jpeg_and_returns_metadata(client):
    payload = b"\xff\xd8\xff\xe0fake-jpeg"

    response = client.post(
        "/upload",
        data=payload,
        headers=UPLOAD_HEADERS,
    )

    assert response.status_code == 200
    body = response.get_json()
    assert body["message"] == "File uploaded successfully"
    assert body["filename"].endswith(".jpg")
    assert body["size"] == len(payload)
    assert body["device_id"] == "esp32cam-front-door"
    assert body["captured_at"] == "2026-03-11T12:34:56+00:00"
    assert datetime.fromisoformat(body["stored_at"]).tzinfo is not None


def test_upload_rejects_empty_payload(client):
    response = client.post("/upload", data=b"", headers=UPLOAD_HEADERS)

    assert response.status_code == 400
    assert response.get_json()["message"] == "No data received"


def test_upload_rejects_unsupported_content_type(client):
    response = client.post(
        "/upload",
        data=b"png",
        headers={
            "Content-Type": "image/png",
            "X-Device-Id": "esp32cam-front-door",
            "X-Captured-At": "2026-03-11T12:34:56Z",
        },
    )

    assert response.status_code == 415
    assert response.get_json()["message"] == "Unsupported content type"


def test_upload_rejects_invalid_jpeg_payload(client):
    response = client.post("/upload", data=b"not-jpeg", headers=UPLOAD_HEADERS)

    assert response.status_code == 400
    assert response.get_json()["message"] == "Invalid JPEG payload"


def test_upload_rejects_missing_device_metadata(client):
    response = client.post(
        "/upload",
        data=b"\xff\xd8jpeg",
        headers={"Content-Type": "image/jpeg"},
    )

    assert response.status_code == 400
    assert response.get_json()["message"] == "Missing device metadata"


def test_upload_rejects_too_long_device_id(client):
    response = client.post(
        "/upload",
        data=b"\xff\xd8jpeg",
        headers={
            "Content-Type": "image/jpeg",
            "X-Device-Id": "x" * 129,
            "X-Captured-At": "2026-03-11T12:34:56Z",
        },
    )

    assert response.status_code == 400
    assert response.get_json()["message"] == "device_id is too long"


def test_upload_rejects_invalid_captured_at_metadata(client):
    response = client.post(
        "/upload",
        data=b"\xff\xd8jpeg",
        headers={
            "Content-Type": "image/jpeg",
            "X-Device-Id": "esp32cam-front-door",
            "X-Captured-At": "not-a-timestamp",
        },
    )

    assert response.status_code == 400
    assert response.get_json()["message"] == "Invalid captured_at metadata"


def test_upload_rejects_timezone_naive_captured_at_metadata(client):
    response = client.post(
        "/upload",
        data=b"\xff\xd8jpeg",
        headers={
            "Content-Type": "image/jpeg",
            "X-Device-Id": "esp32cam-front-door",
            "X-Captured-At": "2026-03-11T12:34:56",
        },
    )

    assert response.status_code == 400
    assert response.get_json()["message"] == "Invalid captured_at metadata"


def test_upload_normalizes_non_utc_captured_at_metadata_to_utc(client):
    response = client.post(
        "/upload",
        data=b"\xff\xd8jpeg",
        headers={
            "Content-Type": "image/jpeg",
            "X-Device-Id": "esp32cam-front-door",
            "X-Captured-At": "2026-03-11T21:34:56+09:00",
        },
    )

    assert response.status_code == 200
    assert response.get_json()["captured_at"] == "2026-03-11T12:34:56+00:00"


def test_files_page_lists_latest_upload_first(client):
    first = client.post(
        "/upload",
        data=b"\xff\xd8first-image",
        headers={
            "Content-Type": "image/jpeg",
            "X-Device-Id": "esp32cam-front-door",
            "X-Captured-At": "2026-03-11T12:34:56Z",
        },
    ).get_json()
    time.sleep(0.01)
    second = client.post(
        "/upload",
        data=b"\xff\xd8second-image",
        headers={
            "Content-Type": "image/jpeg",
            "X-Device-Id": "esp32cam-garden",
            "X-Captured-At": "2026-03-11T12:35:56Z",
        },
    ).get_json()

    response = client.get("/files")
    page = response.get_data(as_text=True)

    assert response.status_code == 200
    assert second["filename"] in page
    assert first["filename"] in page
    assert "esp32cam-garden" in page
    assert "2026-03-11 21:35:56 JST" in page
    assert "2026-03-11T12:35:56+00:00" not in page
    assert page.index(second["filename"]) < page.index(first["filename"])


def test_index_page_shows_camera_dashboard_metadata(client):
    client.post(
        "/upload",
        data=b"\xff\xd8front-door",
        headers=UPLOAD_HEADERS,
    )
    client.post(
        "/upload",
        data=b"\xff\xd8garden",
        headers={
            "Content-Type": "image/jpeg",
            "X-Device-Id": "esp32cam-garden",
            "X-Captured-At": "2026-03-11T12:36:56Z",
        },
    )

    response = client.get("/")
    page = response.get_data(as_text=True)

    assert response.status_code == 200
    assert "esp32cam-front-door" in page
    assert "esp32cam-garden" in page
    assert "Camera Dashboard" in page


def test_index_page_marks_snapshot_stats_for_live_updates(client):
    client.post(
        "/upload",
        data=b"\xff\xd8front-door",
        headers=UPLOAD_HEADERS,
    )

    response = client.get("/")
    page = response.get_data(as_text=True)

    assert response.status_code == 200
    assert 'id="stat-snapshots"' in page
    assert 'id="stat-online"' in page
    assert 'id="cam-status-badge-esp32cam_front_door"' in page
    assert 'id="cam-status-text-esp32cam_front_door"' in page
    assert 'id="cam-interval-esp32cam_front_door"' in page
    assert 'id="cam-frame-size-esp32cam_front_door"' in page
    assert "2026-03-11 21:34:56 JST" in page
    assert "2026-03-11 12:34:56 UTC" not in page
    assert "incrementStatSnapshotCount" in page
    assert "markCameraHealthy" in page
    assert "updateCameraTiming" in page
    assert "updateFrameSize" in page
    assert "scheduleHealthChecks" in page


def test_camera_detail_page_marks_snapshot_count_for_live_updates(client):
    client.post(
        "/upload",
        data=b"\xff\xd8front-door",
        headers=UPLOAD_HEADERS,
    )

    response = client.get("/cameras/esp32cam-front-door")
    page = response.get_data(as_text=True)

    assert response.status_code == 200
    assert 'id="snapshot-count"' in page
    assert 'id="camera-last-seen"' in page
    assert 'id="camera-live-interval"' in page
    assert 'id="camera-frame-size"' in page
    assert "2026-03-11 21:34:56 JST" in page
    assert "2026-03-11 12:34:56 UTC" not in page
    assert "incrementSnapshotCount" in page
    assert "updateCameraTiming" in page
    assert "updateFrameSize" in page
    assert "scheduleHealthChecks" in page


def test_camera_detail_page_filters_snapshots_by_camera(client):
    front = client.post(
        "/upload",
        data=b"\xff\xd8front",
        headers=UPLOAD_HEADERS,
    ).get_json()
    garden = client.post(
        "/upload",
        data=b"\xff\xd8garden",
        headers={
            "Content-Type": "image/jpeg",
            "X-Device-Id": "esp32cam-garden",
            "X-Captured-At": "2026-03-11T12:36:56Z",
        },
    ).get_json()

    response = client.get("/cameras/esp32cam-front-door")
    page = response.get_data(as_text=True)

    assert response.status_code == 200
    assert front["filename"] in page
    assert garden["filename"] not in page


def test_api_cameras_returns_camera_summaries(client):
    client.post(
        "/upload",
        data=b"\xff\xd8payload",
        headers=UPLOAD_HEADERS,
    )

    response = client.get("/api/cameras")
    body = response.get_json()

    assert response.status_code == 200
    assert body["cameras"][0]["camera_id"] == "esp32cam-front-door"
    assert body["cameras"][0]["status"] == "healthy"


def test_api_camera_upload_metrics_returns_diagnostics(app, client):
    app.extensions["upload_metrics"].record_connection("esp32cam-front-door")
    app.extensions["upload_metrics"].record_frame("esp32cam-front-door", 12345)
    app.extensions["upload_metrics"].record_disconnect("esp32cam-front-door")

    response = client.get("/api/cameras/esp32cam-front-door/upload-metrics")
    body = response.get_json()

    assert response.status_code == 200
    assert body["camera_id"] == "esp32cam-front-door"
    assert body["connections"] == 1
    assert body["active_connections"] == 0
    assert body["disconnects"] == 1
    assert body["frames_received"] == 1
    assert body["bytes_received"] == 12345
    assert body["last_frame_at"] is not None


def test_api_camera_snapshots_returns_camera_scoped_snapshots(client):
    client.post(
        "/upload",
        data=b"\xff\xd8front",
        headers=UPLOAD_HEADERS,
    )
    client.post(
        "/upload",
        data=b"\xff\xd8garden",
        headers={
            "Content-Type": "image/jpeg",
            "X-Device-Id": "esp32cam-garden",
            "X-Captured-At": "2026-03-11T12:36:56Z",
        },
    )

    response = client.get("/api/cameras/esp32cam-front-door/snapshots")
    body = response.get_json()

    assert response.status_code == 200
    assert body["camera_id"] == "esp32cam-front-door"
    assert len(body["snapshots"]) == 1
    assert body["snapshots"][0]["camera_id"] == "esp32cam-front-door"


def test_get_file_returns_uploaded_image_bytes(client):
    upload = client.post(
        "/upload",
        data=b"\xff\xd8payload",
        headers=UPLOAD_HEADERS,
    ).get_json()

    response = client.get(f"/files/{upload['filename']}")

    assert response.status_code == 200
    assert response.data == b"\xff\xd8payload"
    assert response.mimetype == "image/jpeg"


def test_get_file_rejects_metadata_sidecar_requests(client):
    upload = client.post(
        "/upload",
        data=b"\xff\xd8payload",
        headers=UPLOAD_HEADERS,
    ).get_json()

    response = client.get(f"/files/{upload['filename']}.json")

    assert response.status_code == 404
    assert response.get_json()["message"] == "File not found"


def test_get_file_returns_404_for_missing_file(client):
    response = client.get("/files/missing.jpg")

    assert response.status_code == 404
    assert response.get_json()["message"] == "File not found"


def test_upload_returns_500_when_storage_write_fails(app, client, monkeypatch):
    def fail_save_image(_data, _content_type, *, device_id, captured_at):
        raise OSError("disk full")

    monkeypatch.setattr(app.extensions["storage"], "save_image", fail_save_image)

    response = client.post("/upload", data=b"\xff\xd8broken", headers=UPLOAD_HEADERS)

    assert response.status_code == 500
    assert response.get_json()["message"] == "File upload failed"


def test_upload_returns_413_for_large_payload(app, client):
    app.config["MAX_CONTENT_LENGTH"] = 8

    response = client.post(
        "/upload",
        data=b"\xff\xd8toolarge",
        headers=UPLOAD_HEADERS,
    )

    assert response.status_code == 413
    assert response.get_json()["message"] == "Payload too large"


def test_upload_records_person_detection_when_detector_finds_person(app, client):
    from script.detector import Detection

    app.extensions["inference_service"].detector = FakePersonDetector(
        [Detection(label="person", score=0.9, x1=0.1, y1=0.2, x2=0.4, y2=0.8)]
    )

    client.post(
        "/upload",
        data=b"\xff\xd8payload",
        headers=UPLOAD_HEADERS,
    )

    response = client.get("/api/detections")
    body = response.get_json()

    assert response.status_code == 200
    assert len(body["detections"]) == 1
    assert body["detections"][0]["label"] == "person"


def test_upload_still_returns_200_when_detector_fails(app, client):
    app.extensions["inference_service"].detector = BrokenDetector()

    response = client.post(
        "/upload",
        data=b"\xff\xd8payload",
        headers=UPLOAD_HEADERS,
    )
    snapshots = app.extensions["metadata_store"].list_camera_snapshots("esp32cam-front-door", limit=1)

    assert response.status_code == 200
    assert snapshots[0].inference_status == "failed"
    assert snapshots[0].inference_error == "model unavailable"


def test_upload_publishes_snapshot_created_event(app, client):
    received: list[dict[str, object]] = []
    app.extensions["snapshot_event_publisher"].subscribe(received.append)

    response = client.post(
        "/upload",
        data=b"\xff\xd8event-payload",
        headers=UPLOAD_HEADERS,
    )

    assert response.status_code == 200
    assert len(received) == 1
    event = received[0]
    body = response.get_json()
    assert event["type"] == "snapshot.created"
    assert event["camera_id"] == "esp32cam-front-door"
    assert event["snapshot"]["filename"] == body["filename"]
    assert event["snapshot"]["url"] == f"/files/{body['filename']}"
    assert event["snapshot"]["size_bytes"] == body["size"]


def test_status_update_records_heartbeat(client):
    response = client.post(
        "/api/status_update",
        json={"device_id": "esp32cam-heartbeat"},
        content_type="application/json",
    )

    assert response.status_code == 200
    body = response.get_json()
    assert body["message"] == "Status updated"
    assert body["camera_id"] == "esp32cam-heartbeat"
    assert "last_seen_at" in body


def test_status_update_rejects_missing_device_id(client):
    response = client.post("/api/status_update", json={}, content_type="application/json")

    assert response.status_code == 400
    assert response.get_json()["message"] == "device_id is required"


def test_status_update_accepts_device_id_at_max_length(client):
    response = client.post(
        "/api/status_update",
        json={"device_id": "x" * 128},
        content_type="application/json",
    )

    assert response.status_code == 200
    assert response.get_json()["message"] == "Status updated"


def test_status_update_rejects_too_long_device_id(client):
    response = client.post(
        "/api/status_update",
        json={"device_id": "x" * 129},
        content_type="application/json",
    )

    assert response.status_code == 400
    assert response.get_json()["message"] == "device_id is too long"


def test_log_error_records_device_log(client):
    client.post(
        "/api/status_update",
        json={"device_id": "esp32cam-log-test"},
        content_type="application/json",
    )
    response = client.post(
        "/api/log_error",
        json={"device_id": "esp32cam-log-test", "level": "error", "message": "OOM crash"},
        content_type="application/json",
    )

    assert response.status_code == 201
    body = response.get_json()
    assert body["camera_id"] == "esp32cam-log-test"
    assert body["level"] == "error"
    assert body["message"] == "OOM crash"
    assert "logged_at" in body


def test_log_error_uses_error_level_by_default(client):
    client.post("/api/status_update", json={"device_id": "cam-default"}, content_type="application/json")
    response = client.post(
        "/api/log_error",
        json={"device_id": "cam-default", "message": "unexpected reboot"},
        content_type="application/json",
    )

    assert response.status_code == 201
    assert response.get_json()["level"] == "error"


def test_log_error_rejects_missing_device_id(client):
    response = client.post(
        "/api/log_error",
        json={"message": "some error"},
        content_type="application/json",
    )

    assert response.status_code == 400
    assert response.get_json()["message"] == "device_id is required"


def test_log_error_rejects_missing_message(client):
    response = client.post(
        "/api/log_error",
        json={"device_id": "cam-x"},
        content_type="application/json",
    )

    assert response.status_code == 400
    assert response.get_json()["message"] == "message is required"


def test_log_error_rejects_too_long_device_id(client):
    response = client.post(
        "/api/log_error",
        json={"device_id": "x" * 129, "message": "test"},
        content_type="application/json",
    )

    assert response.status_code == 400
    assert response.get_json()["message"] == "device_id is too long"


def test_log_error_rejects_too_long_message(client):
    response = client.post(
        "/api/log_error",
        json={"device_id": "cam-z", "message": "x" * 2001},
        content_type="application/json",
    )

    assert response.status_code == 400
    assert response.get_json()["message"] == "message is too long"


def test_log_error_rejects_invalid_level(client):
    client.post("/api/status_update", json={"device_id": "cam-y"}, content_type="application/json")
    response = client.post(
        "/api/log_error",
        json={"device_id": "cam-y", "level": "verbose", "message": "test"},
        content_type="application/json",
    )

    assert response.status_code == 400
    assert "level" in response.get_json()["message"]


def test_api_camera_logs_returns_logs_for_camera(client):
    client.post("/api/status_update", json={"device_id": "cam-logs"}, content_type="application/json")
    client.post(
        "/api/log_error",
        json={"device_id": "cam-logs", "level": "warning", "message": "low memory"},
        content_type="application/json",
    )
    client.post(
        "/api/log_error",
        json={"device_id": "cam-logs", "level": "error", "message": "upload failed"},
        content_type="application/json",
    )

    response = client.get("/api/cameras/cam-logs/logs")

    assert response.status_code == 200
    body = response.get_json()
    assert body["camera_id"] == "cam-logs"
    assert len(body["logs"]) == 2
    assert body["logs"][0]["message"] == "upload failed"


def test_api_camera_logs_returns_404_for_unknown_camera(client):
    response = client.get("/api/cameras/nonexistent-camera/logs")

    assert response.status_code == 404


def test_patch_camera_updates_display_name(client):
    client.post("/api/status_update", json={"device_id": "cam-patch"}, content_type="application/json")

    response = client.patch(
        "/api/cameras/cam-patch",
        json={"display_name": "玄関カメラ"},
        content_type="application/json",
    )

    assert response.status_code == 200
    body = response.get_json()
    assert body["display_name"] == "玄関カメラ"
    assert body["camera_id"] == "cam-patch"


def test_patch_camera_updates_interval(client):
    client.post("/api/status_update", json={"device_id": "cam-patch"}, content_type="application/json")

    response = client.patch(
        "/api/cameras/cam-patch",
        json={"expected_interval_seconds": 60},
        content_type="application/json",
    )

    assert response.status_code == 200
    assert response.get_json()["expected_interval_seconds"] == 60


def test_patch_camera_returns_404_for_unknown_camera(client):
    response = client.patch(
        "/api/cameras/nonexistent",
        json={"display_name": "test"},
        content_type="application/json",
    )

    assert response.status_code == 404


def test_patch_camera_rejects_empty_display_name(client):
    client.post("/api/status_update", json={"device_id": "cam-patch"}, content_type="application/json")

    response = client.patch(
        "/api/cameras/cam-patch",
        json={"display_name": ""},
        content_type="application/json",
    )

    assert response.status_code == 400
    assert "display_name" in response.get_json()["message"]


def test_patch_camera_rejects_invalid_interval(client):
    client.post("/api/status_update", json={"device_id": "cam-patch"}, content_type="application/json")

    response = client.patch(
        "/api/cameras/cam-patch",
        json={"expected_interval_seconds": -1},
        content_type="application/json",
    )

    assert response.status_code == 400
    assert "expected_interval_seconds" in response.get_json()["message"]


def test_patch_camera_rejects_boolean_interval(client):
    client.post("/api/status_update", json={"device_id": "cam-patch"}, content_type="application/json")

    response = client.patch(
        "/api/cameras/cam-patch",
        json={"expected_interval_seconds": True},
        content_type="application/json",
    )

    assert response.status_code == 400
    assert "expected_interval_seconds" in response.get_json()["message"]


def test_patch_camera_with_no_fields_is_idempotent(client):
    client.post("/api/status_update", json={"device_id": "cam-patch"}, content_type="application/json")
    client.patch("/api/cameras/cam-patch", json={"display_name": "元の名前"}, content_type="application/json")

    response = client.patch("/api/cameras/cam-patch", json={}, content_type="application/json")

    assert response.status_code == 200
    assert response.get_json()["display_name"] == "元の名前"


def test_patch_camera_updates_both_fields(client):
    client.post("/api/status_update", json={"device_id": "cam-patch"}, content_type="application/json")

    response = client.patch(
        "/api/cameras/cam-patch",
        json={"display_name": "駐車場カメラ", "expected_interval_seconds": 120},
        content_type="application/json",
    )

    assert response.status_code == 200
    body = response.get_json()
    assert body["display_name"] == "駐車場カメラ"
    assert body["expected_interval_seconds"] == 120
