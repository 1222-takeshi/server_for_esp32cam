from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass
from datetime import datetime, timezone
import threading


@dataclass
class CameraUploadMetrics:
    camera_id: str
    connections: int = 0
    active_connections: int = 0
    disconnects: int = 0
    frames_received: int = 0
    bytes_received: int = 0
    errors: int = 0
    connected_at: datetime | None = None
    last_disconnect_at: datetime | None = None
    last_frame_at: datetime | None = None
    last_error: str | None = None
    last_error_at: datetime | None = None

    def to_dict(self) -> dict[str, object]:
        return {
            "camera_id": self.camera_id,
            "connections": self.connections,
            "active_connections": self.active_connections,
            "disconnects": self.disconnects,
            "frames_received": self.frames_received,
            "bytes_received": self.bytes_received,
            "errors": self.errors,
            "connected_at": self.connected_at.isoformat() if self.connected_at else None,
            "last_disconnect_at": (
                self.last_disconnect_at.isoformat() if self.last_disconnect_at else None
            ),
            "last_frame_at": self.last_frame_at.isoformat() if self.last_frame_at else None,
            "last_error": self.last_error,
            "last_error_at": self.last_error_at.isoformat() if self.last_error_at else None,
        }


class UploadMetricsRegistry:
    def __init__(self, max_tracked_cameras: int = 500) -> None:
        self._lock = threading.RLock()
        self._metrics: OrderedDict[str, CameraUploadMetrics] = OrderedDict()
        self._max_tracked_cameras = max_tracked_cameras

    def _get(self, camera_id: str) -> CameraUploadMetrics:
        metrics = self._metrics.get(camera_id)
        if metrics is None:
            metrics = CameraUploadMetrics(camera_id=camera_id)
            self._metrics[camera_id] = metrics
            if self._max_tracked_cameras > 0 and len(self._metrics) > self._max_tracked_cameras:
                # Evict the least-recently-active camera_id so a churn of
                # spoofed/rotated device ids can't grow this dict unbounded.
                self._metrics.popitem(last=False)
        else:
            self._metrics.move_to_end(camera_id)
        return metrics

    def record_connection(self, camera_id: str, *, at: datetime | None = None) -> None:
        timestamp = at or datetime.now(tz=timezone.utc)
        with self._lock:
            metrics = self._get(camera_id)
            metrics.connections += 1
            metrics.active_connections += 1
            metrics.connected_at = timestamp

    def record_disconnect(self, camera_id: str, *, at: datetime | None = None) -> None:
        timestamp = at or datetime.now(tz=timezone.utc)
        with self._lock:
            metrics = self._get(camera_id)
            metrics.disconnects += 1
            metrics.active_connections = max(metrics.active_connections - 1, 0)
            metrics.last_disconnect_at = timestamp

    def record_frame(self, camera_id: str, size: int, *, at: datetime | None = None) -> None:
        timestamp = at or datetime.now(tz=timezone.utc)
        with self._lock:
            metrics = self._get(camera_id)
            metrics.frames_received += 1
            metrics.bytes_received += size
            metrics.last_frame_at = timestamp

    def record_error(self, camera_id: str, message: str, *, at: datetime | None = None) -> None:
        timestamp = at or datetime.now(tz=timezone.utc)
        with self._lock:
            metrics = self._get(camera_id)
            metrics.errors += 1
            metrics.last_error = message
            metrics.last_error_at = timestamp

    def snapshot(self, camera_id: str) -> dict[str, object]:
        with self._lock:
            metrics = self._metrics.get(camera_id)
            if metrics is None:
                return CameraUploadMetrics(camera_id=camera_id).to_dict()
            return metrics.to_dict()
