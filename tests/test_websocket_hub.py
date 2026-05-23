from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest

from script.events import SnapshotEventPublisher
from script.metadata import SnapshotRecord
from script.websocket_hub import ConnectionLimitExceeded, WebSocketHub


class FakeWebSocket:
    def __init__(self, *, fail_after: int | None = None):
        self.messages: list[str] = []
        self._fail_after = fail_after

    def send(self, message: str) -> None:
        if self._fail_after is not None and len(self.messages) >= self._fail_after:
            raise RuntimeError("websocket disconnected")
        self.messages.append(message)


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


def test_websocket_hub_sends_snapshot_events_as_json_to_connected_socket():
    publisher = SnapshotEventPublisher()
    hub = WebSocketHub(publisher)
    ws = FakeWebSocket()

    hub.connect(ws)
    publisher.publish_snapshot_created(_snapshot())

    assert len(ws.messages) == 1
    event = json.loads(ws.messages[0])
    assert event["type"] == "snapshot.created"
    assert event["camera_id"] == "esp32cam-front-door"
    assert event["snapshot"]["url"] == "/files/image_20260508T010203000000_abcd.jpg"


def test_websocket_hub_filters_events_by_camera_id():
    publisher = SnapshotEventPublisher()
    hub = WebSocketHub(publisher)
    front_ws = FakeWebSocket()
    garden_ws = FakeWebSocket()

    hub.connect(front_ws, camera_id="esp32cam-front-door")
    hub.connect(garden_ws, camera_id="esp32cam-garden")
    publisher.publish_snapshot_created(_snapshot("esp32cam-front-door"))

    assert len(front_ws.messages) == 1
    assert garden_ws.messages == []


def test_websocket_hub_disconnect_stops_future_delivery():
    publisher = SnapshotEventPublisher()
    hub = WebSocketHub(publisher)
    ws = FakeWebSocket()

    disconnect = hub.connect(ws)
    disconnect()
    publisher.publish_snapshot_created(_snapshot())

    assert ws.messages == []


def test_websocket_hub_removes_socket_when_send_fails():
    publisher = SnapshotEventPublisher()
    hub = WebSocketHub(publisher)
    broken_ws = FakeWebSocket(fail_after=0)
    healthy_ws = FakeWebSocket()

    hub.connect(broken_ws)
    hub.connect(healthy_ws)
    publisher.publish_snapshot_created(_snapshot())
    publisher.publish_snapshot_created(_snapshot())

    assert broken_ws.messages == []
    assert len(healthy_ws.messages) == 2


def test_websocket_hub_rejects_connection_when_limit_reached():
    publisher = SnapshotEventPublisher()
    hub = WebSocketHub(publisher, max_connections=2)

    hub.connect(FakeWebSocket())
    hub.connect(FakeWebSocket())

    with pytest.raises(ConnectionLimitExceeded):
        hub.connect(FakeWebSocket())


def test_websocket_hub_frees_slot_on_disconnect():
    publisher = SnapshotEventPublisher()
    hub = WebSocketHub(publisher, max_connections=1)

    disconnect = hub.connect(FakeWebSocket())
    disconnect()

    # After disconnect the slot is free and a new connection should succeed
    hub.connect(FakeWebSocket())


def test_websocket_hub_tracks_active_connections():
    publisher = SnapshotEventPublisher()
    hub = WebSocketHub(publisher)

    assert hub.active_connections == 0
    d1 = hub.connect(FakeWebSocket())
    assert hub.active_connections == 1
    d2 = hub.connect(FakeWebSocket())
    assert hub.active_connections == 2
    d1()
    assert hub.active_connections == 1
    d2()
    assert hub.active_connections == 0
