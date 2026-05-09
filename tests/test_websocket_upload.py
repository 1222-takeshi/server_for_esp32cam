from __future__ import annotations

from datetime import datetime, timezone

from script.websocket_routes import handle_binary_snapshot_upload


JPEG_BYTES = b"\xff\xd8\xff\xe0fake-jpeg\xff\xd9"


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
        "message": "Invalid JPEG payload",
    }
