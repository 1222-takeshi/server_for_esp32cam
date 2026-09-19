from __future__ import annotations

import json
import threading
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


class BlockingWebSocket:
    def __init__(self) -> None:
        self.started = threading.Event()
        self.release = threading.Event()
        self.messages: list[str] = []

    def send(self, message: str) -> None:
        self.started.set()
        self.release.wait(timeout=2)
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
    publisher.publish_snapshot_created(_snapshot(), image_bytes=b"private-frame")
    hub.flush()

    assert len(ws.messages) == 1
    event = json.loads(ws.messages[0])
    assert event["type"] == "snapshot.created"
    assert event["camera_id"] == "esp32cam-front-door"
    assert event["snapshot"]["url"] == "/files/image_20260508T010203000000_abcd.jpg"
    assert "private-frame" not in ws.messages[0]


def test_websocket_hub_broadcast_is_noop_with_no_connections():
    publisher = SnapshotEventPublisher()
    hub = WebSocketHub(publisher)

    publisher.publish_snapshot_created(_snapshot(), image_bytes=b"private-frame")

    assert hub._pending_events.qsize() == 0
    assert hub._worker is None


def test_websocket_hub_filters_events_by_camera_id():
    publisher = SnapshotEventPublisher()
    hub = WebSocketHub(publisher)
    front_ws = FakeWebSocket()
    garden_ws = FakeWebSocket()

    hub.connect(front_ws, camera_id="esp32cam-front-door")
    hub.connect(garden_ws, camera_id="esp32cam-garden")
    publisher.publish_snapshot_created(_snapshot("esp32cam-front-door"))
    hub.flush()

    assert len(front_ws.messages) == 1
    assert garden_ws.messages == []


def test_websocket_hub_can_count_connection_without_broadcasting_to_it():
    publisher = SnapshotEventPublisher()
    hub = WebSocketHub(publisher)
    upload_ws = FakeWebSocket()

    hub.connect(upload_ws, camera_id="esp32cam-front-door", receive_events=False)
    publisher.publish_snapshot_created(_snapshot())
    hub.flush()

    assert hub.active_connections == 1
    assert upload_ws.messages == []


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
    hub.flush()

    assert broken_ws.messages == []
    assert len(healthy_ws.messages) == 2


def test_websocket_hub_does_not_block_publisher_on_slow_websocket():
    publisher = SnapshotEventPublisher()
    hub = WebSocketHub(publisher)
    ws = BlockingWebSocket()
    hub.connect(ws)
    returned = threading.Event()

    def publish() -> None:
        publisher.publish_snapshot_created(_snapshot())
        returned.set()

    publish_thread = threading.Thread(target=publish)
    publish_thread.start()

    assert ws.started.wait(timeout=1)
    assert returned.wait(timeout=0.2)

    ws.release.set()
    publish_thread.join(timeout=1)
    hub.flush()
    hub.close()

    assert not publish_thread.is_alive()
    assert len(ws.messages) == 1


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
