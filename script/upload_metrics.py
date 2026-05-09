from __future__ import annotations

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
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._metrics: dict[str, CameraUploadMetrics] = {}

    def _get(self, camera_id: str) -> CameraUploadMetrics:
        metrics = self._metrics.get(camera_id)
        if metrics is None:
            metrics = CameraUploadMetrics(camera_id=camera_id)
            self._metrics[camera_id] = metrics
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

    def snapshot_all(self) -> list[dict[str, object]]:
        with self._lock:
            return [self._metrics[camera_id].to_dict() for camera_id in sorted(self._metrics)]
