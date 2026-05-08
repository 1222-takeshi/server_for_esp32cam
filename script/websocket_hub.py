from __future__ import annotations

from collections.abc import Callable
import json
from typing import Protocol

from script.events import SnapshotEvent, SnapshotEventPublisher


class WebSocketLike(Protocol):
    def send(self, message: str) -> None:
        ...


class WebSocketHub:
    def __init__(self, publisher: SnapshotEventPublisher) -> None:
        self._publisher = publisher

    def connect(self, websocket: WebSocketLike, *, camera_id: str | None = None) -> Callable[[], None]:
        unsubscribe: Callable[[], None] | None = None

        def send_event(event: SnapshotEvent) -> None:
            nonlocal unsubscribe
            try:
                websocket.send(json.dumps(event, separators=(",", ":")))
            except Exception:
                if unsubscribe is not None:
                    unsubscribe()
                raise

        unsubscribe = self._publisher.subscribe(send_event, camera_id=camera_id)
        return unsubscribe
