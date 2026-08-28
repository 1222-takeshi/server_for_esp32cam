"""Unit tests for the retention-scan gate (issue #196) and the SQLite-backed
retention scan itself (#200 / #225 / #246 / #260).

Retention candidates are sourced from the SQLite index
(`SQLiteMetadataStore.list_snapshot_retention_candidates()` /
`list_video_retention_candidates()`) instead of a filesystem `rglob()` walk,
so `RetentionScanGate` exists to avoid paying even that indexed-query cost on
every single upload. These tests cover the gate's scan/skip schedule in
isolation, that the `apply_*_upload_retention` wrappers honor it (skipping
the metadata-store query entirely when the gate declines), and an end-to-end
route-level test that retention is still eventually enforced even though
intermediate uploads skip the scan.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch
from uuid import uuid4

from script.retention import (
    RetentionScanGate,
    RetentionScheduler,
    apply_upload_retention,
    apply_video_upload_retention,
)
from script.app import create_app, start_retention_scheduler
from script.config import gunicorn_preload_requested, gunicorn_worker_count
from script.storage import StorageRetentionPolicy, StoredImage, StoredVideo
from tests.conftest import MINIMAL_JPEG

import pytest


@pytest.fixture()
def app_factory(tmp_path):
    """Build a fresh app (with its own upload dir/DB) per call, so each test
    case gets an isolated `RetentionScanGate` instance."""

    def _build(**config_overrides):
        unique = uuid4().hex
        config = {
            "TESTING": True,
            "UPLOAD_DIR": tmp_path / f"uploads-{unique}",
            "DB_PATH": tmp_path / f"esp32cam-{unique}.sqlite3",
            "INFERENCE_MODE": "sync",
            "RATELIMIT_ENABLED": False,
        }
        config.update(config_overrides)
        return create_app(config)

    return _build


# ---------------------------------------------------------------------------
# RetentionScanGate
# ---------------------------------------------------------------------------


def test_gate_default_interval_scans_every_call():
    gate = RetentionScanGate()
    assert [gate.should_scan() for _ in range(5)] == [True, True, True, True, True]


def test_gate_interval_one_scans_every_call():
    gate = RetentionScanGate(interval=1)
    assert [gate.should_scan() for _ in range(5)] == [True, True, True, True, True]


def test_gate_non_positive_interval_treated_as_one():
    for interval in (0, -5):
        gate = RetentionScanGate(interval=interval)
        assert [gate.should_scan() for _ in range(3)] == [True, True, True]


def test_gate_scans_first_call_then_every_nth_call():
    # interval=3: scan on call 1 (always), skip 2-3, scan on call 4, skip 5-6,
    # scan on call 7, ...
    gate = RetentionScanGate(interval=3)
    results = [gate.should_scan() for _ in range(7)]
    assert results == [True, False, False, True, False, False, True]


def test_gate_is_thread_safe_and_scans_exactly_once_per_interval():
    import threading

    gate = RetentionScanGate(interval=10)
    scan_count = 0
    lock = threading.Lock()

    def worker():
        nonlocal scan_count
        if gate.should_scan():
            with lock:
                scan_count += 1

    threads = [threading.Thread(target=worker) for _ in range(100)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    # 100 calls at interval=10 must scan exactly 10 times, no more, no less,
    # regardless of thread interleaving.
    assert scan_count == 10


# ---------------------------------------------------------------------------
# apply_upload_retention / apply_video_upload_retention wiring
# ---------------------------------------------------------------------------


def _stored_image() -> StoredImage:
    return StoredImage(
        name="image_1.jpg",
        relative_path="cam-a/image_1.jpg",
        size=10,
        created_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        device_id="cam-a",
        captured_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
    )


def _stored_video() -> StoredVideo:
    return StoredVideo(
        name="video_1.mp4",
        relative_path="cam-a/video_1.mp4",
        size=10,
        camera_id="cam-a",
        captured_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        stored_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        duration_ms=None,
        content_type="video/mp4",
        sha256="0" * 64,
    )


def test_apply_upload_retention_skips_metadata_query_when_gate_declines(app):
    fake_metadata_store = MagicMock()
    app.extensions["metadata_store"] = fake_metadata_store
    app.extensions["storage_retention_scan_gate"] = MagicMock(should_scan=MagicMock(return_value=False))

    with app.app_context():
        result = apply_upload_retention(_stored_image())

    assert result == []
    fake_metadata_store.list_snapshot_retention_candidates.assert_not_called()


def test_apply_upload_retention_runs_metadata_query_when_gate_allows(app):
    fake_metadata_store = MagicMock()
    fake_metadata_store.list_snapshot_retention_candidates.return_value = ([], 0)
    app.extensions["metadata_store"] = fake_metadata_store
    app.extensions["storage_retention_scan_gate"] = MagicMock(should_scan=MagicMock(return_value=True))

    with app.app_context():
        apply_upload_retention(_stored_image())

    fake_metadata_store.list_snapshot_retention_candidates.assert_called_once_with(
        exclude_relative_paths={_stored_image().relative_path}
    )


def test_apply_upload_retention_disabled_policy_never_consults_gate(app):
    # Both limits unset -> policy.enabled is False.
    app.extensions["storage_retention_policy"] = StorageRetentionPolicy()
    gate = MagicMock(should_scan=MagicMock(return_value=True))
    app.extensions["storage_retention_scan_gate"] = gate

    with app.app_context():
        result = apply_upload_retention(_stored_image())

    assert result == []
    gate.should_scan.assert_not_called()


def test_apply_video_upload_retention_skips_metadata_query_when_gate_declines(app):
    app.extensions["video_retention_policy"] = StorageRetentionPolicy(max_bytes=1000)
    fake_metadata_store = MagicMock()
    app.extensions["metadata_store"] = fake_metadata_store
    app.extensions["video_retention_scan_gate"] = MagicMock(should_scan=MagicMock(return_value=False))

    with app.app_context():
        result = apply_video_upload_retention(_stored_video())

    assert result == []
    fake_metadata_store.list_video_retention_candidates.assert_not_called()


def test_apply_video_upload_retention_runs_metadata_query_when_gate_allows(app):
    app.extensions["video_retention_policy"] = StorageRetentionPolicy(max_bytes=1000)
    fake_metadata_store = MagicMock()
    fake_metadata_store.list_video_retention_candidates.return_value = ([], 0)
    app.extensions["metadata_store"] = fake_metadata_store
    app.extensions["video_retention_scan_gate"] = MagicMock(should_scan=MagicMock(return_value=True))

    with app.app_context():
        apply_video_upload_retention(_stored_video())

    fake_metadata_store.list_video_retention_candidates.assert_called_once_with(
        exclude_relative_paths={_stored_video().relative_path}
    )


def test_scheduler_runs_image_and_video_retention_without_an_upload(app_factory):
    app = app_factory()
    app.extensions["storage_retention_policy"] = StorageRetentionPolicy(max_age_seconds=1)
    app.extensions["video_retention_policy"] = StorageRetentionPolicy(max_age_seconds=1)

    metadata_store = MagicMock()
    metadata_store.list_snapshot_retention_candidates.return_value = (
        [("cam-a/old.jpg", 10, datetime(2020, 1, 1, tzinfo=timezone.utc))],
        10,
    )
    metadata_store.list_video_retention_candidates.return_value = (
        [("cam-a/old.mp4", 20, datetime(2020, 1, 1, tzinfo=timezone.utc))],
        20,
    )
    app.extensions["metadata_store"] = metadata_store
    storage = MagicMock()
    storage.delete_image_by_relative_path.return_value = 10
    storage.delete_video_by_relative_path.return_value = 20
    app.extensions["storage"] = storage

    scheduler = RetentionScheduler(app, interval_seconds=3600)
    deleted_images, deleted_videos = scheduler.run_once()

    assert deleted_images == ["cam-a/old.jpg"]
    assert deleted_videos == ["cam-a/old.mp4"]
    metadata_store.delete_snapshots_by_relative_paths.assert_called_once_with(["cam-a/old.jpg"])
    metadata_store.delete_videos_by_relative_paths.assert_called_once_with(["cam-a/old.mp4"])


def test_scheduler_logs_failure_and_continues_loop(app_factory, caplog):
    import logging

    app = app_factory()
    scheduler = RetentionScheduler(app, interval_seconds=3600)
    calls = 0

    def fail_once():
        nonlocal calls
        calls += 1
        scheduler._stop_event.set()
        raise RuntimeError("retention failed")

    scheduler.run_once = fail_once
    with caplog.at_level(logging.ERROR):
        scheduler._run_loop()

    assert calls == 1
    assert "Scheduled retention failed" in caplog.text


def test_scheduler_does_not_start_in_testing_mode(app_factory):
    app = app_factory(RETENTION_SCHEDULER_INTERVAL_SECONDS=1)

    scheduler = app.extensions["retention_scheduler"]

    assert scheduler.is_running is False


def test_scheduler_start_and_stop_runs_a_cycle(app_factory):
    import threading

    app = app_factory()
    scheduler = RetentionScheduler(app, interval_seconds=0.01)
    cycle_finished = threading.Event()

    def run_once():
        cycle_finished.set()
        scheduler.stop()
        return [], []

    scheduler.run_once = run_once

    assert scheduler.start() is True
    assert cycle_finished.wait(timeout=1) is True
    scheduler.stop()
    assert scheduler.is_running is False


def test_scheduler_zero_interval_is_disabled(app_factory):
    app = app_factory()
    scheduler = RetentionScheduler(app, interval_seconds=0)

    assert scheduler.start() is False
    assert scheduler.is_running is False


def test_scheduler_can_be_deferred_until_worker_initialization(app_factory):
    app = app_factory(TESTING=False, RETENTION_SCHEDULER_AUTO_START=False)
    scheduler = app.extensions["retention_scheduler"]

    assert scheduler.is_running is False
    assert start_retention_scheduler(app) is True

    scheduler.stop()
    assert scheduler.is_running is False


def test_start_retention_scheduler_skips_lock_once_already_running(app_factory):
    # Regression test: start_retention_scheduler() is called from
    # _before_request on every request when RETENTION_SCHEDULER_AUTO_START
    # is disabled. Once the scheduler is running, it must short-circuit on
    # the lock-free is_running check instead of re-invoking scheduler.start()
    # (which re-acquires _state_lock) on every subsequent request.
    app = app_factory(TESTING=False, RETENTION_SCHEDULER_AUTO_START=False)
    scheduler = app.extensions["retention_scheduler"]

    assert start_retention_scheduler(app) is True
    assert scheduler.is_running is True

    start_calls = []
    scheduler.start = lambda: start_calls.append(True) or True

    assert start_retention_scheduler(app) is False
    assert start_calls == [], "start() must not be re-invoked once already running"

    scheduler.stop()


def test_deferred_scheduler_starts_on_first_worker_request(app_factory):
    app = app_factory(TESTING=False, RETENTION_SCHEDULER_AUTO_START=False)
    scheduler = app.extensions["retention_scheduler"]

    assert scheduler.is_running is False
    response = app.test_client().get("/healthz/ready")

    assert response.status_code == 200
    assert scheduler.is_running is True
    scheduler.stop()


def test_scheduler_info_logs_are_enabled(app_factory):
    import logging

    app_factory()
    retention_logger = logging.getLogger("script.retention")

    assert retention_logger.isEnabledFor(logging.INFO) is True
    assert any(
        getattr(handler, "_esp32cam_retention_handler", False)
        for handler in retention_logger.handlers
    )


def test_gunicorn_preload_detection_is_explicit():
    assert gunicorn_preload_requested(["/venv/bin/gunicorn", "--preload"]) is True
    assert gunicorn_preload_requested(
        ["/venv/bin/gunicorn"], command_args="--workers 1 --preload"
    ) is True
    assert gunicorn_preload_requested(["/venv/bin/gunicorn", "--workers", "1"]) is False
    assert gunicorn_preload_requested(["pytest", "--preload"]) is False


def test_gunicorn_worker_count_parses_common_worker_flags():
    assert gunicorn_worker_count(["/venv/bin/gunicorn", "--workers", "4"]) == 4
    assert gunicorn_worker_count(["/venv/bin/gunicorn", "--workers=3"]) == 3
    assert gunicorn_worker_count(["/venv/bin/gunicorn", "-w", "2"]) == 2
    assert gunicorn_worker_count(["/venv/bin/gunicorn", "-w2"]) == 2
    assert gunicorn_worker_count(
        ["/venv/bin/gunicorn", "--workers", "4"], command_args="--workers 2"
    ) == 4
    assert gunicorn_worker_count(["pytest", "--workers", "4"]) is None


def test_memory_rate_limit_storage_warns_for_multiple_workers(app_factory, caplog):
    with patch("script.app.gunicorn_worker_count", return_value=2):
        app_factory(RATELIMIT_STORAGE_URI="memory://")

    assert any("multiple Gunicorn workers" in record.message for record in caplog.records)


@pytest.mark.parametrize("worker_count, storage_uri", [(1, "memory://"), (2, "redis://localhost")])
def test_memory_rate_limit_warning_is_limited_to_unsafe_configuration(
    app_factory, caplog, worker_count, storage_uri
):
    with patch("script.app.gunicorn_worker_count", return_value=worker_count):
        app_factory(RATELIMIT_STORAGE_URI=storage_uri)

    assert not any("multiple Gunicorn workers" in record.message for record in caplog.records)


def test_scheduler_logs_lifecycle_and_scan_result(app_factory, caplog):
    import logging
    import threading

    app = app_factory()
    scheduler = RetentionScheduler(app, interval_seconds=3600)
    scan_finished = threading.Event()
    original_run_once = scheduler.run_once

    def run_once():
        result = original_run_once()
        scan_finished.set()
        scheduler._stop_event.set()
        return result

    scheduler.run_once = run_once

    with caplog.at_level(logging.INFO):
        assert scheduler.start() is True
        assert scan_finished.wait(timeout=1) is True
        scheduler.stop()

    assert "Storage retention scheduler started (interval_seconds=3600.0)" in caplog.text
    assert "Scheduled retention completed (images_deleted=0, videos_deleted=0)" in caplog.text
    assert "Storage retention scheduler stopped" in caplog.text


# ---------------------------------------------------------------------------
# End-to-end: retention is skipped on intermediate uploads but still
# eventually enforced once the interval boundary is reached.
# ---------------------------------------------------------------------------


def _upload(client, *, device_id: str, captured_at: str, body: bytes = MINIMAL_JPEG):
    return client.post(
        "/upload",
        data=body,
        headers={
            "Content-Type": "image/jpeg",
            "X-Device-Id": device_id,
            "X-Captured-At": captured_at,
        },
    )


def test_retention_scan_interval_skips_then_eventually_enforces(app_factory):
    # RetentionScanGate(interval=2) scans on calls 1, 3, 5, ... (always scans
    # on the first call, then every 2nd call after that).
    app = app_factory(
        STORAGE_RETENTION_MAX_BYTES=1000,
        STORAGE_RETENTION_SCAN_INTERVAL_UPLOADS=2,
    )
    client = app.test_client()

    # Upload 1 (call 1, scanned): nothing to evict yet, storage is under budget.
    resp1 = _upload(client, device_id="cam-ret", captured_at="2026-01-01T00:00:00Z")
    assert resp1.status_code == 200
    snap1 = resp1.get_json()

    # Upload 2 (call 2, skipped): two images together exceed the 1000-byte
    # budget, which would normally evict snap1 -- but the scan is skipped,
    # so snap1 must still be present.
    resp2 = _upload(client, device_id="cam-ret", captured_at="2026-01-01T01:00:00Z")
    assert resp2.status_code == 200
    assert client.get(f"/files/{snap1['filename']}").status_code == 200

    # Upload 3 (call 3, scanned): interval boundary reached -> the full scan
    # runs and catches up on the retention backlog, evicting the oldest file
    # that the skipped scan in step 2 left behind.
    resp3 = _upload(client, device_id="cam-ret", captured_at="2026-01-01T02:00:00Z")
    assert resp3.status_code == 200
    snap3 = resp3.get_json()

    assert client.get(f"/files/{snap1['filename']}").status_code == 404
    assert client.get(f"/files/{snap3['filename']}").status_code == 200


def test_retention_scan_interval_default_matches_prior_every_upload_behavior(app_factory):
    # Default (no STORAGE_RETENTION_SCAN_INTERVAL_UPLOADS override) must
    # behave exactly like before this change: scan on every upload.
    app = app_factory(STORAGE_RETENTION_MAX_BYTES=1000)
    client = app.test_client()

    resp1 = _upload(client, device_id="cam-ret", captured_at="2026-01-01T00:00:00Z")
    snap1 = resp1.get_json()

    resp2 = _upload(client, device_id="cam-ret", captured_at="2026-01-01T01:00:00Z")
    snap2 = resp2.get_json()

    assert client.get(f"/files/{snap2['filename']}").status_code == 200
    assert client.get(f"/files/{snap1['filename']}").status_code == 404


# ---------------------------------------------------------------------------
# Retention no longer walks the filesystem (#200 / #225 / #246 / #260):
# candidates now come from the SQLite index, not `Path.rglob()`.
# ---------------------------------------------------------------------------


def test_upload_retention_does_not_walk_the_filesystem(app_factory, monkeypatch):
    from pathlib import Path

    # Build the app (and its startup sync_storage_index() rglob) *before*
    # patching, since that one-time startup sync is intentionally
    # filesystem-based; only the per-upload retention path must avoid it.
    app = app_factory(STORAGE_RETENTION_MAX_BYTES=1000)
    client = app.test_client()

    def _fail_rglob(self, *args, **kwargs):
        raise AssertionError(
            "retention must source candidates from SQLite, not Path.rglob()"
        )

    monkeypatch.setattr(Path, "rglob", _fail_rglob)

    resp1 = _upload(client, device_id="cam-ret", captured_at="2026-01-01T00:00:00Z")
    assert resp1.status_code == 200
    snap1 = resp1.get_json()

    resp2 = _upload(client, device_id="cam-ret", captured_at="2026-01-01T01:00:00Z")
    assert resp2.status_code == 200
    snap2 = resp2.get_json()

    # Retention still ran and evicted the oldest snapshot -- it just did so
    # without ever calling Path.rglob().
    assert client.get(f"/files/{snap2['filename']}").status_code == 200
    assert client.get(f"/files/{snap1['filename']}").status_code == 404


# ---------------------------------------------------------------------------
# Disk usage check gate (issue #208): _check_disk_usage() ran an unconditional
# shutil.disk_usage() syscall on every upload; it's gated by the same
# RetentionScanGate mechanism used above.
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# _run_retention_scan(): a candidate sourced from the SQLite index can point
# at a file that's already missing from disk (removed out-of-band, or left
# behind by a prior partial failure). The scan must still report it as
# evicted so the caller purges the now-orphaned metadata row -- otherwise
# the same missing-file candidate is silently re-evaluated (and silently
# skipped) on every future scan, permanently drifting the index from disk
# reality.
# ---------------------------------------------------------------------------


def test_run_retention_scan_reports_missing_file_candidates_as_deleted():
    from script.retention import _run_retention_scan

    policy = StorageRetentionPolicy(max_bytes=1)
    candidates = [
        ("cam-a/missing.jpg", 100, datetime(2026, 1, 1, tzinfo=timezone.utc)),
        ("cam-a/present.jpg", 50, datetime(2026, 1, 2, tzinfo=timezone.utc)),
    ]

    def delete_file(relative_path: str) -> int:
        if relative_path == "cam-a/missing.jpg":
            raise FileNotFoundError(relative_path)
        return 50

    deleted, deleted_bytes = _run_retention_scan(
        policy=policy,
        candidates=candidates,
        total_bytes=150,
        delete_file=delete_file,
    )

    # Both candidates are reported as deleted (so the caller purges both
    # metadata rows), but deleted_bytes only counts the file actually freed.
    assert deleted == ["cam-a/missing.jpg", "cam-a/present.jpg"]
    assert deleted_bytes == 50


def test_run_retention_scan_stops_after_ordered_candidates_are_not_expired():
    from script.retention import _run_retention_scan

    now = datetime.now(tz=timezone.utc)

    def candidates():
        yield "cam-a/old.jpg", 100, now - timedelta(hours=1)
        yield "cam-a/current.jpg", 50, now
        raise AssertionError("retention scan should stop at the first non-expired row")

    deleted, deleted_bytes = _run_retention_scan(
        policy=StorageRetentionPolicy(max_age_seconds=60),
        candidates=candidates(),
        total_bytes=150,
        delete_file=lambda _relative_path: 100,
    )

    assert deleted == ["cam-a/old.jpg"]
    assert deleted_bytes == 100


def test_apply_retention_uses_age_filter_when_byte_limit_is_disabled(app_factory):
    from script.retention import _apply_retention

    app = app_factory()
    iter_candidates = MagicMock(return_value=[])
    list_candidates = MagicMock()

    with app.app_context():
        result = _apply_retention(
            policy=StorageRetentionPolicy(max_age_seconds=60, max_bytes=0),
            gate=None,
            exclude_relative_paths=set(),
            list_candidates=list_candidates,
            delete_file=MagicMock(),
            delete_metadata=MagicMock(return_value=0),
            log_prefix="Storage",
            honor_upload_gate=False,
            iter_candidates=iter_candidates,
            total_bytes_reader=MagicMock(return_value=0),
        )

    assert result == []
    list_candidates.assert_not_called()
    assert iter_candidates.call_args.kwargs["older_than"] is not None


def test_disk_usage_check_default_interval_runs_every_upload(app_factory):
    import script.routes as routes_module

    app = app_factory()
    client = app.test_client()

    with patch("script.routes.shutil.disk_usage") as disk_usage:
        disk_usage.return_value = type("disk_usage", (), {"total": 100, "used": 1, "free": 99})()
        _upload(client, device_id="cam-disk", captured_at="2026-01-01T00:00:00Z")
        # `_check_disk_usage()` reuses the same short-TTL cache as `/metrics`
        # (see `_cached_disk_usage`); clear it so the second gate-permitted
        # call actually re-scans instead of serving a still-fresh cache
        # entry, keeping this test focused on the upload-count gate.
        routes_module._disk_usage_cache.clear()
        _upload(client, device_id="cam-disk", captured_at="2026-01-01T01:00:00Z")

    assert disk_usage.call_count == 2


def test_disk_usage_check_interval_skips_intermediate_uploads(app_factory):
    import script.routes as routes_module

    app = app_factory(DISK_USAGE_CHECK_INTERVAL_UPLOADS=2)
    client = app.test_client()

    with patch("script.routes.shutil.disk_usage") as disk_usage:
        disk_usage.return_value = type("disk_usage", (), {"total": 100, "used": 1, "free": 99})()
        _upload(client, device_id="cam-disk", captured_at="2026-01-01T00:00:00Z")
        _upload(client, device_id="cam-disk", captured_at="2026-01-01T01:00:00Z")
        # Call 2 is skipped by the gate itself (never reaches the cache), so
        # only clear the TTL cache ahead of call 3's gate-permitted scan.
        routes_module._disk_usage_cache.clear()
        _upload(client, device_id="cam-disk", captured_at="2026-01-01T02:00:00Z")

    # Scans on calls 1 and 3 (interval boundary), skips call 2.
    assert disk_usage.call_count == 2
