from __future__ import annotations

from script.upload_metrics import UploadMetricsRegistry


def test_upload_metrics_snapshot_does_not_create_metrics_for_unknown_camera():
    registry = UploadMetricsRegistry()

    snapshot = registry.snapshot("unknown-camera")

    assert snapshot["camera_id"] == "unknown-camera"
    assert snapshot["connections"] == 0
    assert registry.snapshot_all() == []
