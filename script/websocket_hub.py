from __future__ import annotations

from collections.abc import Callable
import json
import threading
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
        self._active_count = 0
        self._lock = threading.Lock()

    @property
    def active_connections(self) -> int:
        return self._active_count

    def connect(self, websocket: WebSocketLike, *, camera_id: str | None = None) -> Callable[[], None]:
        with self._lock:
            if self._active_count >= self._max_connections:
                raise ConnectionLimitExceeded(
                    f"WebSocket connection limit reached ({self._max_connections})"
                )
            self._active_count += 1

        unsubscribe: Callable[[], None] | None = None

        def send_event(event: SnapshotEvent) -> None:
            nonlocal unsubscribe
            try:
                websocket.send(json.dumps(event, separators=(",", ":")))
            except Exception:
                if unsubscribe is not None:
                    unsubscribe()
                raise

        try:
            unsubscribe = self._publisher.subscribe(send_event, camera_id=camera_id)
        except Exception:
            with self._lock:
                self._active_count -= 1
            raise

        def disconnect() -> None:
            with self._lock:
                self._active_count -= 1
            if unsubscribe is not None:
                unsubscribe()

        return disconnect
