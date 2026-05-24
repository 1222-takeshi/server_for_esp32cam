from __future__ import annotations

from datetime import datetime, timezone

from script.websocket_routes import _handle_binary_upload_connection, handle_binary_snapshot_upload
from tests.conftest import MINIMAL_JPEG


class FakeUploadWebSocket:
    def __init__(self, messages):
        self._messages = list(messages)
        self.sent = []

    def receive(self):
        return self._messages.pop(0)

    def send(self, payload: str) -> None:
        self.sent.append(payload)


JPEG_BYTES = MINIMAL_JPEG


def test_handle_binary_snapshot_upload_stores_frame_and_publishes_event(app):
    events = []
    app.extensions["snapshot_event_publisher"].subscribe(events.append)

    with app.app_context():
        response = handle_binary_snapshot_upload(
            JPEG_BYTES,
            camera_id="esp32cam-01",
            captured_at=datetime(2026, 5, 9, 2, 30, tzinfo=timezone.utc),
        )

    assert response["type"] == "snapshot.stored"
    assert response["camera_id"] == "esp32cam-01"
    assert response["snapshot"]["size"] == len(JPEG_BYTES)
    assert response["snapshot"]["url"].startswith("/files/")

    assert len(events) == 1
    assert events[0]["type"] == "snapshot.created"
    assert events[0]["camera_id"] == "esp32cam-01"


def test_handle_binary_snapshot_upload_rejects_non_jpeg_payload(app):
    with app.app_context():
        response = handle_binary_snapshot_upload(
            b"not-a-jpeg",
            camera_id="esp32cam-01",
            captured_at=datetime(2026, 5, 9, 2, 30, tzinfo=timezone.utc),
        )

    assert response == {
        "type": "snapshot.error",
        "message": "Invalid or corrupted JPEG payload",
    }


def test_upload_metrics_track_invalid_binary_payload_errors(app):
    websocket = FakeUploadWebSocket([b"not-a-jpeg", None])

    with app.app_context():
        _handle_binary_upload_connection(websocket, camera_id="esp32cam-01")
        metrics = app.extensions["upload_metrics"].snapshot("esp32cam-01")

    assert metrics["frames_received"] == 0
    assert metrics["errors"] == 1
    assert metrics["last_error"] == "Invalid or corrupted JPEG payload"


def test_upload_metrics_track_frames_bytes_errors_and_disconnects(app):
    websocket = FakeUploadWebSocket([JPEG_BYTES, "text-control-message", None])

    with app.app_context():
        _handle_binary_upload_connection(websocket, camera_id="esp32cam-01")
        metrics = app.extensions["upload_metrics"].snapshot("esp32cam-01")

    assert metrics["camera_id"] == "esp32cam-01"
    assert metrics["connections"] == 1
    assert metrics["active_connections"] == 0
    assert metrics["disconnects"] == 1
    assert metrics["frames_received"] == 1
    assert metrics["bytes_received"] == len(JPEG_BYTES)
    assert metrics["errors"] == 1
    assert metrics["last_error"] == "Expected binary JPEG payload"
    assert metrics["last_frame_at"] is not None


def test_upload_rejects_oversized_payload(app):
    max_bytes = app.config["MAX_CONTENT_LENGTH"]
    oversized = b"\xff\xd8" + b"x" * max_bytes  # valid JPEG header but too large
    websocket = FakeUploadWebSocket([oversized, None])

    with app.app_context():
        _handle_binary_upload_connection(websocket, camera_id="esp32cam-01")
        metrics = app.extensions["upload_metrics"].snapshot("esp32cam-01")

    import json
    response = json.loads(websocket.sent[0])
    assert response["type"] == "snapshot.error"
    assert response["message"] == "Payload too large"
    assert metrics["errors"] == 1
    assert metrics["frames_received"] == 0
