from __future__ import annotations

from datetime import datetime, timezone

import script.events as events_module
from script.events import SnapshotEventPublisher
from script.metadata import SnapshotRecord


def _snapshot(camera_id: str = "esp32cam-front-door") -> SnapshotRecord:
    return SnapshotRecord(
        id=123,
        filename="image_20260508T010203000000_abcd.jpg",
        relative_path=f"{camera_id}/images/image_20260508T010203000000_abcd.jpg",
        size=10240,
        camera_id=camera_id,
        captured_at=datetime(2026, 5, 8, 1, 2, 3, tzinfo=timezone.utc),
        stored_at=datetime(2026, 5, 8, 1, 2, 4, tzinfo=timezone.utc),
        inference_status="pending",
        inference_error=None,
    )


def test_publish_snapshot_created_delivers_serialized_event_to_subscriber():
    publisher = SnapshotEventPublisher()
    received: list[dict[str, object]] = []
    publisher.subscribe(received.append)

    publisher.publish_snapshot_created(_snapshot())

    assert received == [
        {
            "type": "snapshot.created",
            "camera_id": "esp32cam-front-door",
            "snapshot": {
                "id": 123,
                "filename": "image_20260508T010203000000_abcd.jpg",
                "relative_path": "esp32cam-front-door/images/image_20260508T010203000000_abcd.jpg",
                "url": "/files/image_20260508T010203000000_abcd.jpg",
                "thumbnail_url": "/files/image_20260508T010203000000_abcd.jpg/thumbnail",
                "size": 10240,
                "size_bytes": 10240,
                "captured_at": "2026-05-08T01:02:03+00:00",
                "stored_at": "2026-05-08T01:02:04+00:00",
                "inference_status": "pending",
                "inference_error": None,
            },
        }
    ]


def test_publish_snapshot_created_keeps_frame_bytes_out_of_public_event():
    publisher = SnapshotEventPublisher()
    received: list[dict[str, object]] = []
    publisher.subscribe(received.append)

    publisher.publish_snapshot_created(_snapshot(), image_bytes=b"jpeg-bytes")

    assert received[0]["type"] == "snapshot.created"
    assert received[0].raw_image == b"jpeg-bytes"
    assert "raw_image" not in received[0]


def test_publish_snapshot_created_filters_camera_scoped_subscribers():
    publisher = SnapshotEventPublisher()
    front_events: list[dict[str, object]] = []
    garden_events: list[dict[str, object]] = []
    all_events: list[dict[str, object]] = []

    publisher.subscribe(front_events.append, camera_id="esp32cam-front-door")
    publisher.subscribe(garden_events.append, camera_id="esp32cam-garden")
    publisher.subscribe(all_events.append)

    publisher.publish_snapshot_created(_snapshot("esp32cam-front-door"))

    assert len(front_events) == 1
    assert garden_events == []
    assert len(all_events) == 1


def test_unsubscribe_stops_future_events():
    publisher = SnapshotEventPublisher()
    received: list[dict[str, object]] = []

    unsubscribe = publisher.subscribe(received.append)
    unsubscribe()
    publisher.publish_snapshot_created(_snapshot())

    assert received == []


def test_publish_snapshot_created_skips_event_construction_with_no_subscribers(monkeypatch):
    publisher = SnapshotEventPublisher()
    calls = 0
    original = events_module.snapshot_created_event

    def counting_snapshot_created_event(snapshot):
        nonlocal calls
        calls += 1
        return original(snapshot)

    monkeypatch.setattr(events_module, "snapshot_created_event", counting_snapshot_created_event)

    publisher.publish_snapshot_created(_snapshot())

    assert calls == 0


def test_publish_snapshot_created_still_builds_event_once_subscribed(monkeypatch):
    publisher = SnapshotEventPublisher()
    received: list[dict[str, object]] = []
    calls = 0
    original = events_module.snapshot_created_event

    def counting_snapshot_created_event(snapshot):
        nonlocal calls
        calls += 1
        return original(snapshot)

    monkeypatch.setattr(events_module, "snapshot_created_event", counting_snapshot_created_event)
    publisher.subscribe(received.append)

    publisher.publish_snapshot_created(_snapshot())

    assert calls == 1
    assert len(received) == 1


def test_publish_removes_failing_subscriber_and_continues_delivery():
    publisher = SnapshotEventPublisher()
    received: list[dict[str, object]] = []
    attempts = 0

    def broken_subscriber(_event: dict[str, object]) -> None:
        nonlocal attempts
        attempts += 1
        raise RuntimeError("client disconnected")

    publisher.subscribe(broken_subscriber)
    publisher.subscribe(received.append)

    publisher.publish_snapshot_created(_snapshot())
    publisher.publish_snapshot_created(_snapshot())

    assert attempts == 1
    assert len(received) == 2
