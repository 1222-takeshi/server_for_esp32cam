from __future__ import annotations

import json
import logging
import threading
from collections.abc import Callable
from queue import Empty, Full, Queue
from typing import Protocol

from script.events import SnapshotEvent, SnapshotEventPublisher

_logger = logging.getLogger(__name__)


class WebSocketLike(Protocol):
    def send(self, message: str) -> None:
        ...


class ConnectionLimitExceeded(Exception):
    pass


class WebSocketHub:
    def __init__(
        self,
        publisher: SnapshotEventPublisher,
        *,
        max_connections: int = 50,
        max_pending_events: int = 256,
        worker_idle_seconds: float = 1.0,
    ) -> None:
        if max_pending_events < 1:
            raise ValueError("max_pending_events must be at least 1")
        if worker_idle_seconds <= 0:
            raise ValueError("worker_idle_seconds must be positive")

        self._publisher = publisher
        self._max_connections = max_connections
        self._connections: list[tuple[WebSocketLike, str | None, bool]] = []
        self._lock = threading.Lock()
        self._worker_lock = threading.Lock()
        self._pending_events: Queue[tuple[str, str | None]] = Queue(maxsize=max_pending_events)
        self._worker_idle_seconds = worker_idle_seconds
        self._worker: threading.Thread | None = None
        self._closing = False
        # Single subscription — pre-encode JSON once per event then fan out.
        self._unsubscribe_hub: Callable[[], None] | None = self._publisher.subscribe(
            self._broadcast
        )

    @property
    def active_connections(self) -> int:
        with self._lock:
            return len(self._connections)

    def _broadcast(self, event: SnapshotEvent) -> None:
        """Queue an event for delivery without blocking the device upload path."""
        json_str = json.dumps(event, separators=(",", ":"))
        camera_id = event.get("camera_id")

        with self._worker_lock:
            if self._closing:
                _logger.debug("Dropping WebSocket event after hub shutdown")
                return
            try:
                self._pending_events.put_nowait((json_str, camera_id))
            except Full:
                _logger.warning(
                    "Dropping WebSocket event because the pending queue is full"
                )
                return
            self._ensure_worker_locked()

    def _ensure_worker_locked(self) -> None:
        if self._worker is not None and self._worker.is_alive():
            return
        self._worker = threading.Thread(
            target=self._worker_loop,
            name="websocket-event-broadcast",
            daemon=True,
        )
        self._worker.start()

    def _worker_loop(self) -> None:
        while True:
            try:
                json_str, camera_id = self._pending_events.get(timeout=self._worker_idle_seconds)
            except Empty:
                with self._worker_lock:
                    if self._closing or self._pending_events.empty():
                        self._worker = None
                        return
                continue

            try:
                self._deliver(json_str, camera_id)
            finally:
                self._pending_events.task_done()

    def _deliver(self, json_str: str, camera_id: str | None) -> None:
        with self._lock:
            connections = list(self._connections)

        failed: list[tuple[WebSocketLike, str | None, bool]] = []
        for ws, ws_camera_id, receive_events in connections:
            if not receive_events:
                continue
            if ws_camera_id is not None and ws_camera_id != camera_id:
                continue
            try:
                ws.send(json_str)
            except Exception:  # noqa: BLE001 - isolate a broken connection, drop it below
                failed.append((ws, ws_camera_id, receive_events))

        if failed:
            failed_ws = {id(ws) for ws, _, _ in failed}
            with self._lock:
                self._connections = [
                    (ws, cid, receive_events)
                    for ws, cid, receive_events in self._connections
                    if id(ws) not in failed_ws
                ]

    def flush(self) -> None:
        """Wait until events queued before this call have been delivered."""
        self._pending_events.join()

    def close(self, *, wait: bool = True) -> None:
        """Stop accepting events and optionally drain the pending queue."""
        with self._worker_lock:
            if self._closing:
                return
            self._closing = True
            worker = self._worker
            unsubscribe = self._unsubscribe_hub
            self._unsubscribe_hub = None

        if unsubscribe is not None:
            unsubscribe()

        if wait:
            self.flush()
        if worker is not None and worker is not threading.current_thread():
            worker.join(timeout=self._worker_idle_seconds + 1.0)

    def connect(
        self,
        websocket: WebSocketLike,
        *,
        camera_id: str | None = None,
        receive_events: bool = True,
    ) -> Callable[[], None]:
        with self._lock:
            if len(self._connections) >= self._max_connections:
                raise ConnectionLimitExceeded(
                    f"WebSocket connection limit reached ({self._max_connections})"
                )
            self._connections.append((websocket, camera_id, receive_events))

        def disconnect() -> None:
            with self._lock:
                self._connections = [
                    (ws, cid, events)
                    for ws, cid, events in self._connections
                    if ws is not websocket
                ]

        return disconnect
