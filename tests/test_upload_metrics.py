from __future__ import annotations

from script.upload_metrics import UploadMetricsRegistry


def test_upload_metrics_snapshot_does_not_create_metrics_for_unknown_camera():
    registry = UploadMetricsRegistry()

    snapshot = registry.snapshot("unknown-camera")

    assert snapshot["camera_id"] == "unknown-camera"
    assert snapshot["connections"] == 0


def test_upload_metrics_evicts_least_recently_active_camera_beyond_cap():
    registry = UploadMetricsRegistry(max_tracked_cameras=2)

    registry.record_connection("camera-a")
    registry.record_connection("camera-b")
    registry.record_connection("camera-c")

    assert registry.snapshot("camera-a")["connections"] == 0
    assert registry.snapshot("camera-b")["connections"] == 1
    assert registry.snapshot("camera-c")["connections"] == 1


def test_upload_metrics_touching_a_camera_protects_it_from_eviction():
    registry = UploadMetricsRegistry(max_tracked_cameras=2)

    registry.record_connection("camera-a")
    registry.record_connection("camera-b")
    registry.record_frame("camera-a", 1024)  # re-touch camera-a, making camera-b the LRU entry
    registry.record_connection("camera-c")

    assert registry.snapshot("camera-a")["frames_received"] == 1
    assert registry.snapshot("camera-b")["connections"] == 0
    assert registry.snapshot("camera-c")["connections"] == 1


def test_upload_metrics_unbounded_when_max_tracked_cameras_is_zero():
    registry = UploadMetricsRegistry(max_tracked_cameras=0)

    for i in range(10):
        registry.record_connection(f"camera-{i}")

    assert registry.snapshot("camera-0")["connections"] == 1
    assert registry.snapshot("camera-9")["connections"] == 1
