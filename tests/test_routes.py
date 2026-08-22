from __future__ import annotations

from datetime import datetime, timezone
import gzip
from io import BytesIO
import uuid
from unittest.mock import patch

from flask import request

from script.app import create_app
from tests.conftest import MINIMAL_JPEG

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


class SequenceDetector:
    def __init__(self, responses):
        self._responses = list(responses)

    def detect(self, _image_bytes: bytes):
        response = self._responses.pop(0)
        if isinstance(response, BaseException):
            raise response
        return response


def test_healthz_returns_ok(client):
    response = client.get("/healthz")

    assert response.status_code == 200
    assert response.get_json() == {"status": "ok"}


def test_healthz_ready_returns_ready(client):
    usage = type("usage", (), {"free": 9_000, "total": 10_000, "used": 1_000})()
    with patch("script.routes.shutil.disk_usage", return_value=usage):
        response = client.get("/healthz/ready")

    assert response.status_code == 200
    body = response.get_json()
    assert body["status"] == "ready"
    assert body["db"] == "ok"
    assert body["storage"] == "ok"
    assert body["disk_free_mb"] >= 0
    assert body["inference_worker"] == "running"


def test_healthz_ready_db_failure(app):
    with app.test_client() as c:
        with patch("script.routes._metadata_store") as mock_store:
            mock_store.return_value.ping.side_effect = Exception("db error")
            response = c.get("/healthz/ready")

    assert response.status_code == 503
    body = response.get_json()
    assert body["status"] == "degraded"
    assert body["db"] == "error"


def test_healthz_ready_storage_failure(app):
    with app.test_client() as c:
        with patch("script.routes.Path.write_bytes", side_effect=OSError("disk full")):
            response = c.get("/healthz/ready")

    assert response.status_code == 503
    body = response.get_json()
    assert body["status"] == "degraded"
    assert body["storage"] == "error"


def test_healthz_ready_low_disk(client):
    usage = type("usage", (), {"free": 100, "total": 10_000, "used": 9_900})()
    with patch("script.routes.shutil.disk_usage", return_value=usage):
        response = client.get("/healthz/ready")

    assert response.status_code == 503
    body = response.get_json()
    assert body["status"] == "degraded"
    assert body["disk_free_mb"] == 0


def test_healthz_ready_disk_usage_is_cached_within_ttl(app):
    # Regression test: healthz_ready() must reuse _cached_disk_usage() like
    # /metrics does, instead of calling shutil.disk_usage() directly on every
    # request.
    usage = type("usage", (), {"free": 9_000, "total": 10_000, "used": 1_000})()
    with app.test_client() as c:
        with patch("script.routes.shutil.disk_usage", return_value=usage) as mock_disk_usage:
            first = c.get("/healthz/ready")
            second = c.get("/healthz/ready")

    assert first.status_code == 200
    assert second.status_code == 200
    assert first.get_json()["disk_free_mb"] == second.get_json()["disk_free_mb"]
    assert mock_disk_usage.call_count == 1


def test_healthz_ready_storage_probe_is_cached_within_ttl(app):
    # Regression test for #343: the write/read/delete probe should not run
    # on every single call -- a second request within the TTL window must
    # reuse the cached result instead of touching disk again.
    with app.test_client() as c:
        with patch("script.routes.uuid.uuid4", wraps=uuid.uuid4) as mock_uuid4:
            first = c.get("/healthz/ready")
            second = c.get("/healthz/ready")

    assert first.status_code == 200
    assert second.status_code == 200
    assert first.get_json()["storage"] == "ok"
    assert second.get_json()["storage"] == "ok"
    assert mock_uuid4.call_count == 1


def test_healthz_ready_storage_probe_refreshes_after_ttl(app):
    # Each /healthz/ready call now takes two time.monotonic() readings --
    # one for the storage probe cache, one for the disk usage cache -- so
    # the mocked clock needs a matching value per reading, in call order.
    with app.test_client() as c:
        with patch("script.routes.time.monotonic", side_effect=[0.0, 0.0, 1.0, 1.0, 10.0, 10.0]):
            with patch("script.routes.uuid.uuid4", wraps=uuid.uuid4) as mock_uuid4:
                c.get("/healthz/ready")
                c.get("/healthz/ready")
                c.get("/healthz/ready")

    # First call probes (cache miss), second reuses the cache (still fresh
    # at t=1), third probes again once the TTL has elapsed (t=10).
    assert mock_uuid4.call_count == 2


def test_healthz_ready_storage_failure_is_cached_within_ttl(app):
    with app.test_client() as c:
        with patch("script.routes.Path.write_bytes", side_effect=OSError("disk full")) as mock_write:
            first = c.get("/healthz/ready")
            second = c.get("/healthz/ready")

    assert first.status_code == 503
    assert second.status_code == 503
    assert first.get_json()["storage"] == "error"
    assert second.get_json()["storage"] == "error"
    assert mock_write.call_count == 1


def test_healthz_ready_inference_worker_stopped(app):
    with app.test_client() as c:
        inference_svc = app.extensions["inference_service"]
        original_mode = inference_svc.mode
        inference_svc.mode = "async"
        inference_svc._worker = None
        try:
            response = c.get("/healthz/ready")
        finally:
            inference_svc.mode = original_mode
            inference_svc._worker = None

    assert response.status_code == 503
    body = response.get_json()
    assert body["status"] == "degraded"
    assert body["inference_worker"] == "stopped"


def test_upload_accepts_jpeg_and_returns_metadata(client):
    payload = MINIMAL_JPEG

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
    assert response.get_json()["message"] == "Invalid or corrupted JPEG payload"


def test_upload_rejects_oversized_jpeg(client):
    from io import BytesIO
    from PIL import Image

    buf = BytesIO()
    Image.new("RGB", (4097, 1)).save(buf, format="JPEG")
    oversized = buf.getvalue()

    response = client.post("/upload", data=oversized, headers=UPLOAD_HEADERS)

    assert response.status_code == 400
    assert "dimensions exceed maximum" in response.get_json()["message"]


def test_upload_rejects_missing_device_metadata(client):
    response = client.post(
        "/upload",
        data=MINIMAL_JPEG,
        headers={"Content-Type": "image/jpeg"},
    )

    assert response.status_code == 400
    assert response.get_json()["message"] == "Missing device metadata"


def test_upload_rejects_too_long_device_id(client):
    response = client.post(
        "/upload",
        data=MINIMAL_JPEG,
        headers={
            "Content-Type": "image/jpeg",
            "X-Device-Id": "x" * 129,
            "X-Captured-At": "2026-03-11T12:34:56Z",
        },
    )

    assert response.status_code == 400
    assert (
        response.get_json()["message"]
        == "device_id contains invalid characters or exceeds maximum length"
    )


def test_upload_rejects_invalid_device_id_chars(client):
    response = client.post(
        "/upload",
        data=MINIMAL_JPEG,
        headers={
            "Content-Type": "image/jpeg",
            "X-Device-Id": "cam; DROP TABLE cameras--",
            "X-Captured-At": "2026-03-11T12:34:56Z",
        },
    )

    assert response.status_code == 400
    assert (
        response.get_json()["message"]
        == "device_id contains invalid characters or exceeds maximum length"
    )


def test_uploaded_device_id_between_64_and_128_chars_is_queryable_as_camera_id(client):
    # Regression test for #255: device_id validation (/upload) accepted up
    # to 128 chars while camera_id validation (video/stream endpoints, e.g.
    # /api/cameras/<camera_id>/videos) capped at 64. A device that
    # successfully uploaded a snapshot with a 65-128 char device_id was then
    # reported as "Camera not found" by every camera_id-keyed endpoint. Both
    # limits are now unified at 128 (script/validation.py).
    long_device_id = "y" * 100
    upload_response = client.post(
        "/upload",
        data=MINIMAL_JPEG,
        headers={
            "Content-Type": "image/jpeg",
            "X-Device-Id": long_device_id,
            "X-Captured-At": "2026-03-11T12:34:56Z",
        },
    )
    assert upload_response.status_code == 200

    videos_response = client.get(f"/api/cameras/{long_device_id}/videos")

    assert videos_response.status_code == 200
    assert videos_response.get_json()["camera_id"] == long_device_id


def test_upload_rejects_invalid_captured_at_metadata(client):
    response = client.post(
        "/upload",
        data=MINIMAL_JPEG,
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
        data=MINIMAL_JPEG,
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
        data=MINIMAL_JPEG,
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
        data=MINIMAL_JPEG,
        headers={
            "Content-Type": "image/jpeg",
            "X-Device-Id": "esp32cam-front-door",
            "X-Captured-At": "2026-03-11T12:34:56Z",
        },
    ).get_json()
    second = client.post(
        "/upload",
        data=MINIMAL_JPEG,
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
        data=MINIMAL_JPEG,
        headers=UPLOAD_HEADERS,
    )
    client.post(
        "/upload",
        data=MINIMAL_JPEG,
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


def test_camera_pages_support_bounded_pagination(client):
    client.post(
        "/upload",
        data=MINIMAL_JPEG,
        headers={**UPLOAD_HEADERS, "X-Captured-At": "2026-03-11T12:34:56Z"},
    )
    client.post(
        "/upload",
        data=MINIMAL_JPEG,
        headers={
            "Content-Type": "image/jpeg",
            "X-Device-Id": "esp32cam-garden",
            "X-Captured-At": "2026-03-11T12:35:56Z",
        },
    )

    dashboard = client.get("/?limit=1&offset=1")
    cameras = client.get("/cameras?limit=1&offset=1")
    boundary = client.get("/cameras?limit=1&offset=99")

    assert dashboard.status_code == 200
    assert cameras.status_code == 200
    assert boundary.status_code == 200
    assert "esp32cam-front-door" in dashboard.get_data(as_text=True)
    assert "esp32cam-garden" not in dashboard.get_data(as_text=True)
    assert "Showing 2–2 of 2 cameras" in cameras.get_data(as_text=True)
    assert "Showing 2–2 of 2 cameras" in boundary.get_data(as_text=True)


def test_api_cameras_returns_pagination_envelope(client):
    client.post(
        "/upload",
        data=MINIMAL_JPEG,
        headers=UPLOAD_HEADERS,
    )
    client.post(
        "/upload",
        data=MINIMAL_JPEG,
        headers={
            "Content-Type": "image/jpeg",
            "X-Device-Id": "esp32cam-garden",
            "X-Captured-At": "2026-03-11T12:35:56Z",
        },
    )

    response = client.get("/api/cameras?limit=1&offset=1")
    body = response.get_json()

    assert response.status_code == 200
    assert body["total"] == 2
    assert body["limit"] == 1
    assert body["offset"] == 1
    assert len(body["cameras"]) == 1


def test_api_cameras_without_pagination_preserves_all_cameras(client):
    client.post(
        "/upload",
        data=MINIMAL_JPEG,
        headers=UPLOAD_HEADERS,
    )
    client.post(
        "/upload",
        data=MINIMAL_JPEG,
        headers={
            "Content-Type": "image/jpeg",
            "X-Device-Id": "esp32cam-garden",
            "X-Captured-At": "2026-03-11T12:35:56Z",
        },
    )

    response = client.get("/api/cameras")
    body = response.get_json()

    assert response.status_code == 200
    assert body["limit"] is None
    assert body["offset"] == 0
    assert len(body["cameras"]) == 2


def test_api_cameras_rejects_invalid_pagination(client):
    response = client.get("/api/cameras?limit=0")

    assert response.status_code == 400


def test_index_page_marks_snapshot_stats_for_live_updates(client):
    client.post(
        "/upload",
        data=MINIMAL_JPEG,
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
        data=MINIMAL_JPEG,
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
        data=MINIMAL_JPEG,
        headers=UPLOAD_HEADERS,
    ).get_json()
    garden = client.post(
        "/upload",
        data=MINIMAL_JPEG,
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
        data=MINIMAL_JPEG,
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
        data=MINIMAL_JPEG,
        headers=UPLOAD_HEADERS,
    )
    client.post(
        "/upload",
        data=MINIMAL_JPEG,
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
        data=MINIMAL_JPEG,
        headers=UPLOAD_HEADERS,
    ).get_json()

    response = client.get(f"/files/{upload['filename']}")

    assert response.status_code == 200
    assert response.data == MINIMAL_JPEG
    assert response.mimetype == "image/jpeg"


def test_get_file_supports_range_requests(client):
    upload = client.post(
        "/upload",
        data=MINIMAL_JPEG,
        headers=UPLOAD_HEADERS,
    ).get_json()

    response = client.get(
        f"/files/{upload['filename']}",
        headers={"Range": "bytes=0-3"},
    )

    assert response.status_code == 206
    assert response.data == MINIMAL_JPEG[:4]
    assert response.content_range.units == "bytes"


def test_get_file_rejects_metadata_sidecar_requests(client):
    upload = client.post(
        "/upload",
        data=MINIMAL_JPEG,
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

    response = client.post("/upload", data=MINIMAL_JPEG, headers=UPLOAD_HEADERS)

    assert response.status_code == 500
    assert response.get_json()["message"] == "File upload failed"


def test_upload_returns_413_for_large_payload(app, client):
    app.config["SNAPSHOT_MAX_CONTENT_LENGTH"] = 8

    response = client.post(
        "/upload",
        data=b"\xff\xd8toolarge",
        headers=UPLOAD_HEADERS,
    )

    assert response.status_code == 413
    assert response.get_json()["message"] == "Payload too large"


def test_upload_sets_snapshot_limit_before_body_read(app, client):
    app.config["MAX_CONTENT_LENGTH"] = 999
    app.config["SNAPSHOT_MAX_CONTENT_LENGTH"] = 8
    observed = {}

    @app.before_request
    def observe_request_limit():
        if request.endpoint == "server.upload_file":
            observed["max_content_length"] = request.max_content_length

    response = client.post("/upload", data=b"0123456789", headers=UPLOAD_HEADERS)

    assert response.status_code == 413
    assert observed["max_content_length"] == 9


def test_upload_rejects_oversized_terminated_stream_without_content_length(app, client):
    app.config["SNAPSHOT_MAX_CONTENT_LENGTH"] = 8

    response = client.open(
        "/upload",
        method="POST",
        input_stream=BytesIO(b"0123456789"),
        headers=UPLOAD_HEADERS,
        environ_overrides={"CONTENT_LENGTH": None, "wsgi.input_terminated": True},
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
        data=MINIMAL_JPEG,
        headers=UPLOAD_HEADERS,
    )

    response = client.get("/api/detections")
    body = response.get_json()

    assert response.status_code == 200
    assert len(body["detections"]) == 1
    assert body["detections"][0]["label"] == "person"


def test_tracking_adds_stable_track_id_to_snapshot_and_detection_apis(app, client):
    from script.detector import Detection
    from script.tracking import ObjectTracker

    app.extensions["inference_service"].tracker = ObjectTracker(iou_threshold=0.3)
    app.extensions["inference_service"].detector = FakePersonDetector(
        [Detection(label="person", score=0.9, x1=0.1, y1=0.2, x2=0.4, y2=0.8)]
    )

    for captured_at in ("2026-03-11T12:34:56Z", "2026-03-11T12:35:56Z"):
        headers = {**UPLOAD_HEADERS, "X-Captured-At": captured_at}
        response = client.post("/upload", data=MINIMAL_JPEG, headers=headers)
        assert response.status_code == 200

    detections = client.get("/api/detections").get_json()["detections"]
    snapshots = client.get("/api/cameras/esp32cam-front-door/snapshots").get_json()["snapshots"]

    assert [d["track_id"] for d in detections] == [1, 1]
    assert [s["detections"][0]["track_id"] for s in snapshots] == [1, 1]


def test_skipped_inference_advances_tracking_missed_frame_state(app, client):
    from script.detector import Detection, DetectionSkipped
    from script.tracking import ObjectTracker

    detection = Detection(label="person", score=0.9, x1=0.1, y1=0.2, x2=0.4, y2=0.8)
    app.extensions["inference_service"].tracker = ObjectTracker(max_missed_frames=0)
    app.extensions["inference_service"].detector = SequenceDetector(
        [[detection], DetectionSkipped("temporarily skipped"), [detection]]
    )

    for index in range(3):
        response = client.post(
            "/upload",
            data=MINIMAL_JPEG,
            headers={**UPLOAD_HEADERS, "X-Captured-At": f"2026-03-11T12:3{index}:56Z"},
        )
        assert response.status_code == 200

    detections = client.get("/api/detections").get_json()["detections"]

    assert [d["track_id"] for d in detections] == [2, 1]


def test_upload_keeps_detected_snapshot_when_detection_only_mode_enabled(app, client):
    from script.detector import Detection

    app.extensions["inference_service"].keep_only_detected_snapshots = True
    app.extensions["inference_service"].detector = FakePersonDetector(
        [Detection(label="person", score=0.9, x1=0.1, y1=0.2, x2=0.4, y2=0.8)]
    )

    response = client.post(
        "/upload",
        data=MINIMAL_JPEG,
        headers=UPLOAD_HEADERS,
    )
    body = response.get_json()

    assert response.status_code == 200
    assert client.get(f"/files/{body['filename']}").status_code == 200
    snapshots = app.extensions["metadata_store"].list_camera_snapshots(
        "esp32cam-front-door", limit=10
    )
    assert len(snapshots) == 1
    assert len(snapshots[0].detections) == 1


def test_upload_discards_undetected_snapshot_when_detection_only_mode_enabled(app, client):
    app.extensions["inference_service"].keep_only_detected_snapshots = True
    app.extensions["inference_service"].detector = FakePersonDetector([])

    response = client.post(
        "/upload",
        data=MINIMAL_JPEG,
        headers=UPLOAD_HEADERS,
    )
    body = response.get_json()

    assert response.status_code == 200
    assert client.get(f"/files/{body['filename']}").status_code == 404
    snapshots = app.extensions["metadata_store"].list_camera_snapshots(
        "esp32cam-front-door", limit=10
    )
    assert snapshots == []


def test_upload_still_returns_200_when_detector_fails(app, client):
    app.extensions["inference_service"].keep_only_detected_snapshots = True
    app.extensions["inference_service"].detector = BrokenDetector()

    response = client.post(
        "/upload",
        data=MINIMAL_JPEG,
        headers=UPLOAD_HEADERS,
    )
    body = response.get_json()
    snapshots = app.extensions["metadata_store"].list_camera_snapshots(
        "esp32cam-front-door", limit=1
    )

    assert response.status_code == 200
    assert client.get(f"/files/{body['filename']}").status_code == 200
    assert snapshots[0].inference_status == "failed"
    assert snapshots[0].inference_error == "model unavailable"


def test_upload_publishes_snapshot_created_event(app, client):
    received: list[dict[str, object]] = []
    app.extensions["snapshot_event_publisher"].subscribe(received.append)

    response = client.post(
        "/upload",
        data=MINIMAL_JPEG,
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
    assert event.raw_image == MINIMAL_JPEG


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
    assert (
        response.get_json()["message"]
        == "device_id contains invalid characters or exceeds maximum length"
    )


def test_status_update_rejects_invalid_device_id_chars(client):
    response = client.post(
        "/api/status_update",
        json={"device_id": "cam/../etc/passwd"},
        content_type="application/json",
    )

    assert response.status_code == 400
    assert (
        response.get_json()["message"]
        == "device_id contains invalid characters or exceeds maximum length"
    )


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
    client.post(
        "/api/status_update", json={"device_id": "cam-default"}, content_type="application/json"
    )
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
    assert (
        response.get_json()["message"]
        == "device_id contains invalid characters or exceeds maximum length"
    )


def test_log_error_rejects_invalid_device_id_chars(client):
    response = client.post(
        "/api/log_error",
        json={"device_id": "cam<script>alert(1)</script>", "message": "test"},
        content_type="application/json",
    )

    assert response.status_code == 400
    assert (
        response.get_json()["message"]
        == "device_id contains invalid characters or exceeds maximum length"
    )


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


def test_log_error_is_rate_limited(tmp_path):
    app = create_app(
        {
            "TESTING": True,
            "UPLOAD_DIR": tmp_path / "uploads",
            "DB_PATH": tmp_path / "esp32cam.sqlite3",
            "INFERENCE_MODE": "sync",
            "LOG_ERROR_RATE_LIMIT": "1 per hour",
            # Explicit True (not just "unset"): other tests in this module
            # create apps with RATELIMIT_ENABLED=False, and Flask-Limiter's
            # `enabled` flag lives on the shared module-level `limiter`
            # object (script.routes.limiter), so an unset value here would
            # inherit whatever the most recently-initialized app left behind.
            "RATELIMIT_ENABLED": True,
        }
    )
    log_client = app.test_client()
    payload = {"device_id": "cam-rate-limited", "level": "error", "message": "boom"}

    first = log_client.post("/api/log_error", json=payload, content_type="application/json")
    second = log_client.post("/api/log_error", json=payload, content_type="application/json")

    assert first.status_code == 201
    assert second.status_code == 429

    with app.app_context():
        logs = app.extensions["metadata_store"].list_device_logs("cam-rate-limited")
    assert len(logs) == 1


def test_log_error_rate_limit_is_configurable(tmp_path):
    app = create_app(
        {
            "TESTING": True,
            "UPLOAD_DIR": tmp_path / "uploads",
            "DB_PATH": tmp_path / "esp32cam.sqlite3",
            "INFERENCE_MODE": "sync",
            "LOG_ERROR_RATE_LIMIT": "2 per hour",
            "RATELIMIT_ENABLED": True,
        }
    )
    log_client = app.test_client()
    payload = {"device_id": "cam-two-allowed", "level": "error", "message": "boom"}

    first = log_client.post("/api/log_error", json=payload, content_type="application/json")
    second = log_client.post("/api/log_error", json=payload, content_type="application/json")
    third = log_client.post("/api/log_error", json=payload, content_type="application/json")

    assert first.status_code == 201
    assert second.status_code == 201
    assert third.status_code == 429


def test_api_camera_logs_returns_logs_for_camera(client):
    client.post(
        "/api/status_update", json={"device_id": "cam-logs"}, content_type="application/json"
    )
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
    client.post(
        "/api/status_update", json={"device_id": "cam-patch"}, content_type="application/json"
    )

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
    client.post(
        "/api/status_update", json={"device_id": "cam-patch"}, content_type="application/json"
    )

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
    client.post(
        "/api/status_update", json={"device_id": "cam-patch"}, content_type="application/json"
    )

    response = client.patch(
        "/api/cameras/cam-patch",
        json={"display_name": ""},
        content_type="application/json",
    )

    assert response.status_code == 400
    assert "display_name" in response.get_json()["message"]


def test_patch_camera_rejects_invalid_interval(client):
    client.post(
        "/api/status_update", json={"device_id": "cam-patch"}, content_type="application/json"
    )

    response = client.patch(
        "/api/cameras/cam-patch",
        json={"expected_interval_seconds": -1},
        content_type="application/json",
    )

    assert response.status_code == 400
    assert "expected_interval_seconds" in response.get_json()["message"]


def test_patch_camera_rejects_boolean_interval(client):
    client.post(
        "/api/status_update", json={"device_id": "cam-patch"}, content_type="application/json"
    )

    response = client.patch(
        "/api/cameras/cam-patch",
        json={"expected_interval_seconds": True},
        content_type="application/json",
    )

    assert response.status_code == 400
    assert "expected_interval_seconds" in response.get_json()["message"]


def test_patch_camera_with_no_fields_is_idempotent(client):
    client.post(
        "/api/status_update", json={"device_id": "cam-patch"}, content_type="application/json"
    )
    client.patch(
        "/api/cameras/cam-patch", json={"display_name": "元の名前"}, content_type="application/json"
    )

    response = client.patch("/api/cameras/cam-patch", json={}, content_type="application/json")

    assert response.status_code == 200
    assert response.get_json()["display_name"] == "元の名前"


def test_patch_camera_updates_both_fields(client):
    client.post(
        "/api/status_update", json={"device_id": "cam-patch"}, content_type="application/json"
    )

    response = client.patch(
        "/api/cameras/cam-patch",
        json={"display_name": "駐車場カメラ", "expected_interval_seconds": 120},
        content_type="application/json",
    )

    assert response.status_code == 200
    body = response.get_json()
    assert body["display_name"] == "駐車場カメラ"
    assert body["expected_interval_seconds"] == 120


def test_patch_camera_rejects_whitespace_only_display_name(client):
    client.post(
        "/api/status_update", json={"device_id": "cam-patch"}, content_type="application/json"
    )

    response = client.patch(
        "/api/cameras/cam-patch",
        json={"display_name": "   "},
        content_type="application/json",
    )

    assert response.status_code == 400
    assert "display_name" in response.get_json()["message"]


def test_delete_camera_removes_camera(client):
    client.post(
        "/api/status_update", json={"device_id": "cam-delete"}, content_type="application/json"
    )

    response = client.delete("/api/cameras/cam-delete")

    assert response.status_code == 200
    body = response.get_json()
    assert body["message"] == "Camera deleted"
    assert body["camera_id"] == "cam-delete"
    assert body["storage_deleted"] is False


def test_delete_camera_returns_404_for_unknown_camera(client):
    response = client.delete("/api/cameras/nonexistent-cam")

    assert response.status_code == 404


def test_delete_camera_removes_associated_snapshots(client):
    client.post(
        "/upload",
        data=MINIMAL_JPEG,
        headers={
            "Content-Type": "image/jpeg",
            "X-Device-Id": "cam-delete-snap",
            "X-Captured-At": "2026-01-01T12:00:00Z",
        },
    )

    client.delete("/api/cameras/cam-delete-snap")

    snapshots_response = client.get("/api/cameras/cam-delete-snap/snapshots")
    assert snapshots_response.status_code == 404


def test_delete_camera_with_delete_files_removes_storage(client, app):
    client.post(
        "/upload",
        data=MINIMAL_JPEG,
        headers={
            "Content-Type": "image/jpeg",
            "X-Device-Id": "cam-delete-files",
            "X-Captured-At": "2026-01-01T12:00:00Z",
        },
    )
    upload_dir = app.extensions["storage"].upload_dir
    camera_dir = upload_dir / "cam-delete-files"
    assert camera_dir.exists()

    response = client.delete("/api/cameras/cam-delete-files?delete_files=true")

    assert response.status_code == 200
    body = response.get_json()
    assert body["storage_deleted"] is True
    assert not camera_dir.exists()


def test_delete_camera_removes_associated_device_logs(client):
    client.post(
        "/api/log_error",
        json={"device_id": "cam-delete-logs", "message": "test log"},
        content_type="application/json",
    )

    client.delete("/api/cameras/cam-delete-logs")

    logs_response = client.get("/api/cameras/cam-delete-logs/logs")
    assert logs_response.status_code == 404


def test_upload_logs_disk_warning_at_85_percent(client, caplog):
    fake_usage = type("disk_usage", (), {"total": 100, "used": 86, "free": 14})()
    with patch("script.routes.shutil.disk_usage", return_value=fake_usage):
        import logging

        with caplog.at_level(logging.WARNING, logger="script.routes"):
            client.post(
                "/upload",
                data=MINIMAL_JPEG,
                content_type="image/jpeg",
                headers={"X-Device-Id": "cam-disk", "X-Captured-At": "2026-01-01T00:00:00Z"},
            )
    assert any("Disk usage high" in r.message for r in caplog.records)


# ---------------------------------------------------------------------------
# Prometheus metrics endpoint
# ---------------------------------------------------------------------------


def test_metrics_endpoint_returns_prometheus_text(client):
    client.post(
        "/upload",
        data=MINIMAL_JPEG,
        headers={
            "Content-Type": "image/jpeg",
            "X-Device-Id": "cam-metrics",
            "X-Captured-At": "2026-03-11T12:00:00Z",
        },
    )

    response = client.get("/metrics")

    assert response.status_code == 200
    assert "text/plain" in response.content_type
    body = response.get_data(as_text=True)
    assert "esp32cam_upload_total" in body
    assert 'camera_id="cam-metrics"' in body


def test_metrics_endpoint_requires_admin_auth(app, client):
    app.config["ADMIN_TOKEN"] = "secret-token"

    response = client.get("/metrics")
    assert response.status_code == 401

    response = client.get("/metrics", headers={"Authorization": "Bearer secret-token"})
    assert response.status_code == 200


def test_metrics_endpoint_includes_http_request_count(client):
    client.get("/healthz")
    client.get("/healthz")

    response = client.get("/metrics")
    body = response.get_data(as_text=True)

    assert "esp32cam_http_requests_total" in body


def test_metrics_endpoint_caches_disk_usage_within_ttl(client):
    import script.routes as routes_module

    routes_module._disk_usage_cache.clear()
    usage = type("usage", (), {"free": 9_000, "total": 10_000, "used": 1_000})()

    with patch("script.routes.shutil.disk_usage", return_value=usage) as mock_disk_usage:
        client.get("/metrics")
        client.get("/metrics")

    assert mock_disk_usage.call_count == 1


def test_upload_logs_disk_error_at_95_percent(client, caplog):
    fake_usage = type("disk_usage", (), {"total": 100, "used": 96, "free": 4})()
    with patch("script.routes.shutil.disk_usage", return_value=fake_usage):
        import logging

        with caplog.at_level(logging.ERROR, logger="script.routes"):
            client.post(
                "/upload",
                data=MINIMAL_JPEG,
                content_type="image/jpeg",
                headers={"X-Device-Id": "cam-disk2", "X-Captured-At": "2026-01-01T00:00:00Z"},
            )
    assert any("Disk usage critical" in r.message for r in caplog.records)


def test_api_camera_snapshots_returns_pagination_envelope(client):
    client.post(
        "/upload",
        data=MINIMAL_JPEG,
        content_type="image/jpeg",
        headers={"X-Device-Id": "cam-page", "X-Captured-At": "2026-01-01T00:00:00Z"},
    )

    response = client.get("/api/cameras/cam-page/snapshots?limit=10&offset=0")
    assert response.status_code == 200
    body = response.get_json()
    assert "total" in body
    assert "limit" in body
    assert "offset" in body
    assert body["limit"] == 10
    assert body["offset"] == 0
    assert len(body["snapshots"]) >= 1


def test_api_camera_snapshots_supports_before_cursor(client):
    for index in range(2):
        response = client.post(
            "/upload",
            data=MINIMAL_JPEG,
            content_type="image/jpeg",
            headers={
                **UPLOAD_HEADERS,
                "X-Captured-At": f"2026-01-01T00:00:0{index}Z",
            },
        )
        assert response.status_code == 200

    first_page = client.get("/api/cameras/esp32cam-front-door/snapshots?limit=1").get_json()
    second_page = client.get(
        f"/api/cameras/esp32cam-front-door/snapshots?limit=1&before={first_page['next_before']}"
    ).get_json()

    assert isinstance(first_page["next_before"], str)
    assert second_page["snapshots"][0]["id"] < first_page["snapshots"][0]["id"]
    assert second_page["offset"] == 0


def test_api_camera_snapshots_rejects_invalid_before_cursor(client):
    client.post(
        "/upload",
        data=MINIMAL_JPEG,
        content_type="image/jpeg",
        headers=UPLOAD_HEADERS,
    )
    response = client.get("/api/cameras/esp32cam-front-door/snapshots?before=not-an-id")

    assert response.status_code == 400


def test_api_camera_snapshots_cursor_handles_backfilled_stored_at(app, client):
    from script.storage import StoredImage

    base = datetime(2026, 1, 1, tzinfo=timezone.utc)
    store = app.extensions["metadata_store"]
    for name, seconds in (("newest.jpg", 10), ("oldest.jpg", 0), ("middle.jpg", 5)):
        stored_at = base.replace(second=seconds)
        store.record_snapshot(
            StoredImage(
                name=name,
                relative_path=f"cam-backfill/images/{name}",
                size=123,
                created_at=stored_at,
                device_id="cam-backfill",
                captured_at=stored_at,
            )
        )

    first_page = client.get("/api/cameras/cam-backfill/snapshots?limit=1").get_json()
    second_page = client.get(
        f"/api/cameras/cam-backfill/snapshots?limit=10&before={first_page['next_before']}"
    ).get_json()

    assert [snapshot["filename"] for snapshot in second_page["snapshots"]] == [
        "middle.jpg",
        "oldest.jpg",
    ]


def test_files_supports_before_cursor(app, client):
    for index in range(2):
        response = client.post(
            "/upload",
            data=MINIMAL_JPEG,
            content_type="image/jpeg",
            headers={
                **UPLOAD_HEADERS,
                "X-Captured-At": f"2026-01-01T00:00:0{index}Z",
            },
        )
        assert response.status_code == 200

    snapshots = app.extensions["metadata_store"].list_recent_snapshots()
    cursor = client.get(
        "/api/cameras/esp32cam-front-door/snapshots?limit=1"
    ).get_json()["next_before"]
    response = client.get(f"/files?before={cursor}")

    assert response.status_code == 200
    assert snapshots[1].filename.encode() in response.data


def test_api_camera_snapshots_invalid_limit_returns_400(client):
    client.post(
        "/upload",
        data=MINIMAL_JPEG,
        content_type="image/jpeg",
        headers={"X-Device-Id": "cam-page2", "X-Captured-At": "2026-01-01T00:00:00Z"},
    )
    response = client.get("/api/cameras/cam-page2/snapshots?limit=abc")
    assert response.status_code == 400


def test_api_detections_returns_pagination_envelope(client):
    response = client.get("/api/detections?limit=20&offset=0")
    assert response.status_code == 200
    body = response.get_json()
    assert "total" in body
    assert "limit" in body
    assert "offset" in body
    assert body["limit"] == 20


def test_api_detections_supports_before_cursor(app, client):
    from script.detector import Detection

    app.extensions["inference_service"].detector = FakePersonDetector(
        [Detection(label="person", score=0.9, x1=0, y1=0, x2=1, y2=1)]
    )
    for index in range(2):
        response = client.post(
            "/upload",
            data=MINIMAL_JPEG,
            content_type="image/jpeg",
            headers={
                **UPLOAD_HEADERS,
                "X-Captured-At": f"2026-01-01T00:00:0{index}Z",
            },
        )
        assert response.status_code == 200

    first_page = client.get("/api/detections?limit=1").get_json()
    second_page = client.get(
        f"/api/detections?limit=1&before={first_page['next_before']}"
    ).get_json()

    assert first_page["next_before"]
    assert second_page["detections"][0]["detection_id"] != first_page["detections"][0]["detection_id"]


def test_api_detections_rejects_invalid_before_cursor(client):
    response = client.get("/api/detections?before=not-a-cursor")

    assert response.status_code == 400


def test_api_detections_invalid_confidence_min_returns_400(client):
    response = client.get("/api/detections?confidence_min=not_a_number")
    assert response.status_code == 400


def test_api_detections_confidence_min_out_of_range_returns_400(client):
    response = client.get("/api/detections?confidence_min=1.5")
    assert response.status_code == 400


def test_api_camera_snapshots_export_json(client):
    client.post(
        "/upload",
        data=MINIMAL_JPEG,
        content_type="image/jpeg",
        headers={"X-Device-Id": "cam-export", "X-Captured-At": "2026-01-01T00:00:00Z"},
    )

    response = client.get("/api/cameras/cam-export/snapshots?export=json")

    assert response.status_code == 200
    assert response.content_type == "application/json"
    body = response.get_json()
    assert body["camera_id"] == "cam-export"
    assert "count" in body
    assert "snapshots" in body
    assert body["count"] == len(body["snapshots"])
    assert body["count"] >= 1
    assert "total" not in body


def test_api_detections_export_csv(app):
    from script.detector import Detection

    fake_det = FakePersonDetector(
        [Detection(label="person", score=0.9, x1=0.1, y1=0.1, x2=0.5, y2=0.9)]
    )
    inference = app.extensions["inference_service"]
    original_detector = inference.detector
    inference.detector = fake_det

    with app.test_client() as c:
        c.post(
            "/upload",
            data=MINIMAL_JPEG,
            content_type="image/jpeg",
            headers={"X-Device-Id": "cam-csv", "X-Captured-At": "2026-01-01T00:00:00Z"},
        )
        response = c.get("/api/detections?export=csv")

    inference.detector = original_detector

    assert response.status_code == 200
    assert "text/csv" in response.content_type
    assert "attachment" in response.headers.get("Content-Disposition", "")
    lines = response.data.decode().splitlines()
    assert lines[0] == "camera_id,filename,captured_at,stored_at,label,score,track_id,x1,y1,x2,y2"
    assert len(lines) >= 2


def test_api_detections_export_csv_empty(client):
    response = client.get("/api/detections?export=csv")

    assert response.status_code == 200
    assert "text/csv" in response.content_type
    lines = response.data.decode().splitlines()
    assert lines[0] == "camera_id,filename,captured_at,stored_at,label,score,track_id,x1,y1,x2,y2"
    assert len(lines) == 1


def test_api_camera_snapshots_export_json_requires_auth(tmp_path):
    from script.app import create_app

    secure_app = create_app(
        {
            "TESTING": True,
            "UPLOAD_DIR": tmp_path / "uploads",
            "DB_PATH": tmp_path / "db.sqlite3",
            "INFERENCE_MODE": "sync",
            "ADMIN_TOKEN": "secret-token",
        }
    )
    with secure_app.test_client() as c:
        c.post(
            "/upload",
            data=MINIMAL_JPEG,
            content_type="image/jpeg",
            headers={"X-Device-Id": "cam1", "X-Captured-At": "2026-01-01T00:00:00Z"},
        )
        response = c.get("/api/cameras/cam1/snapshots?export=json")
        assert response.status_code == 401

        response = c.get(
            "/api/cameras/cam1/snapshots?export=json",
            headers={"Authorization": "Bearer secret-token"},
        )
        assert response.status_code == 200


def test_api_detections_export_csv_requires_auth(tmp_path):
    from script.app import create_app

    secure_app = create_app(
        {
            "TESTING": True,
            "UPLOAD_DIR": tmp_path / "uploads",
            "DB_PATH": tmp_path / "db.sqlite3",
            "INFERENCE_MODE": "sync",
            "ADMIN_TOKEN": "secret-token",
        }
    )
    with secure_app.test_client() as c:
        response = c.get("/api/detections?export=csv")
        assert response.status_code == 401

        response = c.get(
            "/api/detections?export=csv",
            headers={"Authorization": "Bearer secret-token"},
        )
        assert response.status_code == 200
        assert "text/csv" in response.content_type


def test_camera_detail_includes_recent_logs(client):
    client.post(
        "/upload",
        data=MINIMAL_JPEG,
        content_type="image/jpeg",
        headers={"X-Device-Id": "cam-log-detail", "X-Captured-At": "2026-01-01T00:00:00Z"},
    )
    client.post(
        "/api/log_error",
        json={"device_id": "cam-log-detail", "level": "error", "message": "sensor fail"},
        content_type="application/json",
    )

    response = client.get("/cameras/cam-log-detail")

    assert response.status_code == 200
    assert b"Recent Logs" in response.data
    assert b"sensor fail" in response.data
    assert b"error" in response.data


def test_camera_detail_shows_no_logs_message(client):
    client.post(
        "/upload",
        data=MINIMAL_JPEG,
        content_type="image/jpeg",
        headers={"X-Device-Id": "cam-nologs", "X-Captured-At": "2026-01-01T00:00:00Z"},
    )

    response = client.get("/cameras/cam-nologs")

    assert response.status_code == 200
    assert b"Recent Logs" in response.data
    assert b"No logs yet" in response.data


def test_index_shows_error_warning_for_camera_with_recent_errors(client):
    client.post(
        "/upload",
        data=MINIMAL_JPEG,
        content_type="image/jpeg",
        headers={"X-Device-Id": "cam-warn", "X-Captured-At": "2026-01-01T00:00:00Z"},
    )
    client.post(
        "/api/log_error",
        json={"device_id": "cam-warn", "level": "error", "message": "critical failure"},
        content_type="application/json",
    )

    response = client.get("/")

    assert response.status_code == 200
    assert b"Error log in the last hour" in response.data


# ---------------------------------------------------------------------------
# OpenAPI / Swagger UI
# ---------------------------------------------------------------------------


def test_api_openapi_yaml_serves_spec(client):
    response = client.get("/api/openapi.yaml")
    assert response.status_code == 200
    assert "application/yaml" in response.content_type
    body = response.get_data(as_text=True)
    assert "openapi:" in body
    assert "/upload" in body


def test_api_openapi_yaml_caches_spec_text(client):
    from script.routes import _openapi_spec_text

    _openapi_spec_text.cache_clear()

    client.get("/api/openapi.yaml")
    client.get("/api/openapi.yaml")

    assert _openapi_spec_text.cache_info().hits >= 1


def test_api_docs_serves_swagger_ui(client):
    response = client.get("/api/docs")
    assert response.status_code == 200
    assert "text/html" in response.content_type
    assert b"swagger-ui" in response.data


def test_api_docs_does_not_load_assets_from_a_third_party_cdn(client):
    # Regression test for #316: /api/docs previously loaded swagger-ui-dist
    # from unpkg.com at an unpinned "@5" version with no SRI. It must now be
    # fully self-hosted under /static/vendor/swagger-ui/.
    response = client.get("/api/docs")
    page = response.get_data(as_text=True)

    assert "unpkg.com" not in page
    assert "://" not in page
    assert "/static/vendor/swagger-ui/swagger-ui.css" in page
    assert "/static/vendor/swagger-ui/swagger-ui-bundle.js" in page
    assert "/static/vendor/swagger-ui/swagger-ui-standalone-preset.js" in page
    # The standalone preset must be referenced as its own top-level global
    # (as swagger-ui-standalone-preset.js actually defines it), not as a
    # (nonexistent) property of SwaggerUIBundle.
    assert "SwaggerUIBundle.SwaggerUIStandalonePreset" not in page
    assert "SwaggerUIStandalonePreset" in page


def test_api_docs_vendored_assets_are_served_locally(client):
    for filename, content_type_fragment in [
        ("swagger-ui.css", "css"),
        ("swagger-ui-bundle.js", "javascript"),
        ("swagger-ui-standalone-preset.js", "javascript"),
    ]:
        response = client.get(f"/static/vendor/swagger-ui/{filename}")
        assert response.status_code == 200
        assert content_type_fragment in response.content_type
        assert response.headers.get("Cache-Control") == "public, max-age=31536000, immutable"


def test_index_page_links_to_external_base_css_instead_of_inline_style(client):
    response = client.get("/")
    page = response.get_data(as_text=True)

    assert response.status_code == 200
    assert "<style>" not in page
    assert '<link rel="stylesheet" href="/static/base.css">' in page


def test_dashboard_pages_do_not_load_fonts_from_a_third_party_cdn(client):
    # Regression test for #341: _base.html (shared by every full-page
    # dashboard route) previously loaded Inter/JetBrains Mono from
    # fonts.googleapis.com on every render -- the only remaining external CDN
    # dependency after the Swagger UI vendoring fix (#316, see
    # test_api_docs_does_not_load_assets_from_a_third_party_cdn above). Pages
    # must render entirely from same-origin assets so a LAN/Tailscale-only
    # deployment with no general internet egress isn't penalized by a
    # render-relevant external network round-trip on every page load.
    for path in ("/", "/cameras", "/files"):
        response = client.get(path)
        page = response.get_data(as_text=True)

        assert response.status_code == 200
        assert "fonts.googleapis.com" not in page
        assert "fonts.gstatic.com" not in page


def test_static_base_css_is_served_with_long_lived_cache_headers(client):
    response = client.get("/static/base.css")

    assert response.status_code == 200
    assert "text/css" in response.content_type
    assert len(response.data) > 0
    assert b":root" in response.data
    assert response.headers["Cache-Control"] == "public, max-age=31536000, immutable"


def test_html_response_is_gzip_compressed_when_client_supports_it(client):
    response = client.get("/api/docs", headers={"Accept-Encoding": "gzip"})

    assert response.status_code == 200
    assert response.headers.get("Content-Encoding") == "gzip"
    assert response.headers.get("Vary") == "Accept-Encoding"
    # The test client doesn't auto-decode Content-Encoding (unlike a real
    # browser), so response.data is the raw gzip payload here.
    decompressed = gzip.decompress(response.data)
    assert len(response.data) < len(decompressed)
    assert b"swagger-ui" in decompressed


def test_response_is_not_compressed_when_client_omits_accept_encoding(client):
    response = client.get("/api/docs", headers={"Accept-Encoding": ""})

    assert response.status_code == 200
    assert "Content-Encoding" not in response.headers


def test_small_json_response_is_not_compressed(client):
    # /healthz's body is well under the compression size floor; compressing
    # it would spend CPU for a net-negative bandwidth result.
    response = client.get("/healthz", headers={"Accept-Encoding": "gzip"})

    assert response.status_code == 200
    assert "Content-Encoding" not in response.headers


def test_binary_image_response_is_not_gzip_compressed(client):
    upload = client.post(
        "/upload",
        data=MINIMAL_JPEG,
        headers=UPLOAD_HEADERS,
    ).get_json()

    response = client.get(f"/files/{upload['filename']}", headers={"Accept-Encoding": "gzip"})

    assert response.status_code == 200
    assert "Content-Encoding" not in response.headers
    assert response.data == MINIMAL_JPEG
