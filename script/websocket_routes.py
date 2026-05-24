from __future__ import annotations

from datetime import datetime, timezone
import json
import time

from flask import Flask, current_app, request
from flask_sock import Sock

from script.auth import check_device_api_key
from script.events import SnapshotEventPublisher
from script.retention import apply_upload_retention
from script.routes import _validate_jpeg_image, _serialize_snapshot, _validate_device_id
from script.websocket_hub import ConnectionLimitExceeded, WebSocketHub, WebSocketLike


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
    jpeg_error = _validate_jpeg_image(data)
    if jpeg_error:
        return {"type": "snapshot.error", "message": jpeg_error}

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
    current_app.extensions["upload_metrics"].record_frame(camera_id, len(data))
    apply_upload_retention(stored)
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

    metrics = current_app.extensions["upload_metrics"]
    metrics.record_connection(camera_id)
    min_interval = current_app.config.get("UPLOAD_WS_MIN_FRAME_INTERVAL_SECONDS", 1.0)
    last_frame_at = 0.0
    try:
        while True:
            message = receive()
            if message is None:
                return

            if isinstance(message, str):
                error_message = "Expected binary JPEG payload"
                metrics.record_error(camera_id, error_message)
                response = {"type": "snapshot.error", "message": error_message}
            elif len(message) > current_app.config["MAX_CONTENT_LENGTH"]:
                error_message = "Payload too large"
                metrics.record_error(camera_id, error_message)
                response = {"type": "snapshot.error", "message": error_message}
            else:
                now = time.monotonic()
                if now - last_frame_at < min_interval:
                    error_message = "Rate limit exceeded"
                    metrics.record_error(camera_id, error_message)
                    response = {"type": "snapshot.error", "message": error_message}
                else:
                    last_frame_at = now
                    response = handle_binary_snapshot_upload(message, camera_id=camera_id)
                    if response.get("type") == "snapshot.error":
                        metrics.record_error(camera_id, str(response.get("message", "Upload failed")))
            websocket.send(json.dumps(response))
    finally:
        metrics.record_disconnect(camera_id)


def register_websocket_routes(
    app: Flask, publisher: SnapshotEventPublisher, *, max_connections: int = 50
) -> WebSocketHub:
    hub = WebSocketHub(publisher, max_connections=max_connections)
    sock = Sock(app)

    @sock.route("/ws/snapshots")
    def snapshots(websocket):
        try:
            disconnect = hub.connect(websocket)
        except ConnectionLimitExceeded:
            websocket.send(json.dumps({"type": "error", "message": "Too many connections"}))
            return
        try:
            _keep_connection_open(websocket)
        finally:
            disconnect()

    @sock.route("/ws/cameras/<camera_id>/snapshots")
    def camera_snapshots(websocket, camera_id: str):
        if _validate_device_id(camera_id):
            websocket.send(json.dumps({"type": "error", "message": "Invalid camera_id"}))
            return
        try:
            disconnect = hub.connect(websocket, camera_id=camera_id)
        except ConnectionLimitExceeded:
            websocket.send(json.dumps({"type": "error", "message": "Too many connections"}))
            return
        try:
            _keep_connection_open(websocket)
        finally:
            disconnect()

    @sock.route("/ws/cameras/<camera_id>/upload")
    def camera_upload(websocket, camera_id: str):
        if not check_device_api_key(request.headers.get("X-API-Key")):
            websocket.send(json.dumps({"type": "error", "message": "Unauthorized"}))
            return
        if _validate_device_id(camera_id):
            websocket.send(json.dumps({"type": "error", "message": "Invalid camera_id"}))
            return
        try:
            disconnect = hub.connect(websocket, camera_id=camera_id)
        except ConnectionLimitExceeded:
            websocket.send(json.dumps({"type": "error", "message": "Too many connections"}))
            return
        try:
            _handle_binary_upload_connection(websocket, camera_id=camera_id)
        finally:
            disconnect()

    app.extensions["sock"] = sock
    app.extensions["websocket_hub"] = hub
    return hub
