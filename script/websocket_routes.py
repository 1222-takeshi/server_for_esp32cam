from __future__ import annotations

from flask import Flask
from flask_sock import Sock

from script.events import SnapshotEventPublisher
from script.websocket_hub import WebSocketHub, WebSocketLike


def _keep_connection_open(websocket: WebSocketLike) -> None:
    receive = getattr(websocket, "receive", None)
    if receive is None:
        return

    while receive() is not None:
        continue


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

    app.extensions["sock"] = sock
    app.extensions["websocket_hub"] = hub
    return hub
