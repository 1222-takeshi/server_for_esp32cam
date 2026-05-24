from __future__ import annotations

import time

from prometheus_client import CollectorRegistry, Counter, Gauge, Histogram

_KNOWN_HTTP_METHODS = frozenset({"GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"})


class AppMetrics:
    """Per-app Prometheus metric set using an isolated CollectorRegistry."""

    def __init__(self) -> None:
        self.registry = CollectorRegistry(auto_describe=False)

        self.upload_total = Counter(
            "esp32cam_upload_total",
            "Total number of snapshots uploaded",
            ["camera_id"],
            registry=self.registry,
        )
        self.inference_queue_length = Gauge(
            "esp32cam_inference_queue_length",
            "Current number of snapshots pending inference",
            registry=self.registry,
        )
        self.storage_used_bytes = Gauge(
            "esp32cam_storage_used_bytes",
            "Bytes used in the upload storage directory",
            registry=self.registry,
        )
        self.camera_last_seen_timestamp = Gauge(
            "esp32cam_camera_last_seen_timestamp",
            "Unix timestamp of the camera's most recent upload",
            ["camera_id"],
            registry=self.registry,
        )
        self.http_requests_total = Counter(
            "esp32cam_http_requests_total",
            "Total HTTP requests processed",
            ["method", "endpoint", "status"],
            registry=self.registry,
        )
        self.http_request_duration_seconds = Histogram(
            "esp32cam_http_request_duration_seconds",
            "HTTP request duration in seconds",
            ["method", "endpoint"],
            registry=self.registry,
        )

    def start_request_timer(self) -> float:
        return time.perf_counter()

    def finish_request(self, start: float, method: str, endpoint: str, status: int) -> None:
        duration = time.perf_counter() - start
        safe_method = method if method in _KNOWN_HTTP_METHODS else "OTHER"
        self.http_request_duration_seconds.labels(
            method=safe_method, endpoint=endpoint
        ).observe(duration)
        self.http_requests_total.labels(
            method=safe_method, endpoint=endpoint, status=str(status)
        ).inc()
