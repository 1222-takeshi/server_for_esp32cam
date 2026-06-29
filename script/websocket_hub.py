from __future__ import annotations

import json
import threading
from collections.abc import Callable
from typing import Protocol

from script.events import SnapshotEvent, SnapshotEventPublisher


class WebSocketLike(Protocol):
    def send(self, message: str) -> None:
        ...


class ConnectionLimitExceeded(Exception):
    pass


class WebSocketHub:
    def __init__(self, publisher: SnapshotEventPublisher, *, max_connections: int = 50) -> None:
        self._publisher = publisher
        self._max_connections = max_connections
        self._connections: list[tuple[WebSocketLike, str | None]] = []
        self._lock = threading.Lock()
        # Single subscription — pre-encode JSON once per event then fan out.
        self._unsubscribe_hub = self._publisher.subscribe(self._broadcast)

    @property
    def active_connections(self) -> int:
        with self._lock:
            return len(self._connections)

    def _broadcast(self, event: SnapshotEvent) -> None:
        """Encode event once, then deliver to every matching connection."""
        json_str = json.dumps(event, separators=(",", ":"))
        camera_id = event.get("camera_id")

        with self._lock:
            connections = list(self._connections)

        failed: list[tuple[WebSocketLike, str | None]] = []
        for ws, ws_camera_id in connections:
            if ws_camera_id is not None and ws_camera_id != camera_id:
                continue
            try:
                ws.send(json_str)
            except Exception:
                failed.append((ws, ws_camera_id))

        if failed:
            failed_ws = {id(ws) for ws, _ in failed}
            with self._lock:
                self._connections = [
                    (ws, cid) for ws, cid in self._connections if id(ws) not in failed_ws
                ]

    def connect(
        self, websocket: WebSocketLike, *, camera_id: str | None = None
    ) -> Callable[[], None]:
        with self._lock:
            if len(self._connections) >= self._max_connections:
                raise ConnectionLimitExceeded(
                    f"WebSocket connection limit reached ({self._max_connections})"
                )
            self._connections.append((websocket, camera_id))

        def disconnect() -> None:
            with self._lock:
                self._connections = [
                    (ws, cid) for ws, cid in self._connections if ws is not websocket
                ]

        return disconnect
