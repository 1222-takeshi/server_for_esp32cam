from __future__ import annotations

from datetime import datetime, timezone
import json

from flask import Flask, current_app
from flask_sock import Sock

from script.events import SnapshotEventPublisher
from script.routes import _looks_like_jpeg, _serialize_snapshot
from script.websocket_hub import WebSocketHub, WebSocketLike


def _keep_connection_open(websocket: WebSocketLike) -> None:
    receive = getattr(websocket, "receive", None)
    if receive is None:
        return

    while receive() is not None:
        continue


def handle_binary_snapshot_upload(
    data: bytes,
    *,
    camera_id: str,
    captured_at: datetime | None = None,
) -> dict[str, object]:
    if not _looks_like_jpeg(data):
        return {"type": "snapshot.error", "message": "Invalid JPEG payload"}

    captured_at = captured_at or datetime.now(timezone.utc)
    stored = current_app.extensions["storage"].save_image(
        data,
        "image/jpeg",
        device_id=camera_id,
        captured_at=captured_at,
    )
    snapshot = current_app.extensions["metadata_store"].record_snapshot(stored)
    current_app.extensions["inference_service"].enqueue_snapshot(snapshot.id)
    current_app.extensions["snapshot_event_publisher"].publish_snapshot_created(snapshot)
    return {
        "type": "snapshot.stored",
        "camera_id": camera_id,
        "snapshot": {
            **_serialize_snapshot(snapshot),
            "url": f"/files/{snapshot.filename}",
        },
    }


def _handle_binary_upload_connection(websocket: WebSocketLike, *, camera_id: str) -> None:
    receive = getattr(websocket, "receive", None)
    if receive is None:
        return

    while True:
        message = receive()
        if message is None:
            return

        if isinstance(message, str):
            response = {"type": "snapshot.error", "message": "Expected binary JPEG payload"}
        else:
            response = handle_binary_snapshot_upload(message, camera_id=camera_id)
        websocket.send(json.dumps(response))


def register_websocket_routes(app: Flask, publisher: SnapshotEventPublisher) -> WebSocketHub:
    hub = WebSocketHub(publisher)
    sock = Sock(app)

    @sock.route("/ws/snapshots")
    def snapshots(websocket):
        disconnect = hub.connect(websocket)
        try:
            _keep_connection_open(websocket)
        finally:
            disconnect()

    @sock.route("/ws/cameras/<camera_id>/snapshots")
    def camera_snapshots(websocket, camera_id: str):
        disconnect = hub.connect(websocket, camera_id=camera_id)
        try:
            _keep_connection_open(websocket)
        finally:
            disconnect()

    @sock.route("/ws/cameras/<camera_id>/upload")
    def camera_upload(websocket, camera_id: str):
        _handle_binary_upload_connection(websocket, camera_id=camera_id)

    app.extensions["sock"] = sock
    app.extensions["websocket_hub"] = hub
    return hub
