from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
import threading
from typing import Any

from script.metadata import SnapshotRecord


class SnapshotEvent(dict[str, Any]):
    """JSON-safe event payload with an optional in-process frame side-channel."""

    raw_image: bytes | None = None


SnapshotEventSubscriber = Callable[[SnapshotEvent], None]


@dataclass(frozen=True)
class _Subscription:
    subscriber: SnapshotEventSubscriber
    camera_id: str | None


def snapshot_created_event(snapshot: SnapshotRecord) -> SnapshotEvent:
    return SnapshotEvent(
        {
            "type": "snapshot.created",
            "camera_id": snapshot.camera_id,
            "snapshot": {
                "id": snapshot.id,
                "filename": snapshot.filename,
                "relative_path": snapshot.relative_path,
                "url": f"/files/{snapshot.filename}",
                "thumbnail_url": f"/files/{snapshot.filename}/thumbnail",
                "size": snapshot.size,
                "size_bytes": snapshot.size,
                "captured_at": snapshot.captured_at.isoformat()
                if snapshot.captured_at
                else None,
                "stored_at": snapshot.stored_at.isoformat(),
                "inference_status": snapshot.inference_status,
                "inference_error": snapshot.inference_error,
            },
        }
    )


class SnapshotEventPublisher:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._subscriptions: list[_Subscription] = []

    def subscribe(
        self,
        subscriber: SnapshotEventSubscriber,
        *,
        camera_id: str | None = None,
    ) -> Callable[[], None]:
        subscription = _Subscription(subscriber=subscriber, camera_id=camera_id)
        with self._lock:
            self._subscriptions.append(subscription)

        def unsubscribe() -> None:
            with self._lock:
                self._subscriptions = [item for item in self._subscriptions if item != subscription]

        return unsubscribe

    def publish_snapshot_created(
        self, snapshot: SnapshotRecord, *, image_bytes: bytes | None = None
    ) -> None:
        event = snapshot_created_event(snapshot)
        event.raw_image = image_bytes
        self.publish(event)

    def publish(self, event: SnapshotEvent) -> None:
        camera_id = event.get("camera_id")
        with self._lock:
            subscriptions = list(self._subscriptions)

        failed: list[_Subscription] = []
        for subscription in subscriptions:
            if subscription.camera_id is not None and subscription.camera_id != camera_id:
                continue
            try:
                subscription.subscriber(event)
            except Exception:
                failed.append(subscription)

        if failed:
            with self._lock:
                self._subscriptions = [item for item in self._subscriptions if item not in failed]
