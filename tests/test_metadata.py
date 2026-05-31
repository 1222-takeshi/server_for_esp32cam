from __future__ import annotations

import pytest
from datetime import datetime, timedelta, timezone

from script.detector import Detection
from script.metadata import ALLOWED_LOG_LEVELS, DesiredConfigVersionConflict, SQLiteMetadataStore
from script.storage import StoredImage, StoredVideo


def _stored_image(
    *,
    name: str,
    relative_path: str,
    camera_id: str | None,
    captured_at: datetime | None,
    stored_at: datetime,
) -> StoredImage:
    return StoredImage(
        name=name,
        relative_path=relative_path,
        size=123,
        created_at=stored_at,
        device_id=camera_id,
        captured_at=captured_at,
    )


def test_metadata_store_registers_camera_and_snapshot(tmp_path):
    store = SQLiteMetadataStore(tmp_path / "state.sqlite3")
    stored_at = datetime(2026, 3, 11, 12, 0, tzinfo=timezone.utc)

    snapshot = store.record_snapshot(
        _stored_image(
            name="image_a.jpg",
            relative_path="cam-a/images/image_a.jpg",
            camera_id="cam-a",
            captured_at=stored_at,
            stored_at=stored_at,
        )
    )

    camera = store.get_camera("cam-a")

    assert camera is not None
    assert snapshot.camera_id == "cam-a"
    assert snapshot.inference_status == "pending"


def test_metadata_store_lists_camera_summaries_with_status(tmp_path):
    store = SQLiteMetadataStore(tmp_path / "state.sqlite3")
    now = datetime.now(tz=timezone.utc)

    store.record_snapshot(
        _stored_image(
            name="image_a.jpg",
            relative_path="cam-a/images/image_a.jpg",
            camera_id="cam-a",
            captured_at=now,
            stored_at=now,
        )
    )
    store.record_snapshot(
        _stored_image(
            name="image_b.jpg",
            relative_path="cam-b/images/image_b.jpg",
            camera_id="cam-b",
            captured_at=now - timedelta(hours=1),
            stored_at=now - timedelta(hours=1),
        )
    )

    summaries = store.list_camera_summaries()
    summary_by_camera = {item.camera.camera_id: item for item in summaries}

    assert summary_by_camera["cam-a"].status == "healthy"
    assert summary_by_camera["cam-b"].status == "offline"


def test_metadata_store_syncs_legacy_snapshot_without_camera(tmp_path):
    store = SQLiteMetadataStore(tmp_path / "state.sqlite3")
    stored_at = datetime(2026, 3, 11, 12, 0, tzinfo=timezone.utc)

    store.sync_storage_index(
        [
            _stored_image(
                name="legacy.jpg",
                relative_path="legacy.jpg",
                camera_id=None,
                captured_at=None,
                stored_at=stored_at,
            )
        ]
    )

    snapshots = store.list_recent_snapshots()

    assert snapshots[0].filename == "legacy.jpg"
    assert snapshots[0].camera_id is None
    assert snapshots[0].inference_status == "succeeded"


def test_metadata_store_sync_preserves_camera_first_and_last_seen_at(tmp_path):
    store = SQLiteMetadataStore(tmp_path / "state.sqlite3")
    older = datetime(2026, 3, 11, 12, 0, tzinfo=timezone.utc)
    newer = datetime(2026, 3, 11, 12, 5, tzinfo=timezone.utc)

    store.sync_storage_index(
        [
            _stored_image(
                name="newer.jpg",
                relative_path="cam-a/images/newer.jpg",
                camera_id="cam-a",
                captured_at=newer,
                stored_at=newer,
            ),
            _stored_image(
                name="older.jpg",
                relative_path="cam-a/images/older.jpg",
                camera_id="cam-a",
                captured_at=older,
                stored_at=older,
            ),
        ]
    )

    camera = store.get_camera("cam-a")

    assert camera is not None
    assert camera.first_seen_at == older
    assert camera.last_seen_at == newer


def test_metadata_store_deletes_snapshots_by_relative_path(tmp_path):
    store = SQLiteMetadataStore(tmp_path / "state.sqlite3")
    stored_at = datetime(2026, 3, 11, 12, 0, tzinfo=timezone.utc)
    delete_me = store.record_snapshot(
        _stored_image(
            name="delete.jpg",
            relative_path="cam-a/images/delete.jpg",
            camera_id="cam-a",
            captured_at=stored_at,
            stored_at=stored_at,
        )
    )
    keep_me = store.record_snapshot(
        _stored_image(
            name="keep.jpg",
            relative_path="cam-a/images/keep.jpg",
            camera_id="cam-a",
            captured_at=stored_at,
            stored_at=stored_at,
        )
    )

    deleted = store.delete_snapshots_by_relative_paths([delete_me.relative_path])

    snapshots = store.list_camera_snapshots("cam-a")
    assert deleted == 1
    assert [snapshot.id for snapshot in snapshots] == [keep_me.id]


def test_record_device_log_stores_log(tmp_path):
    store = SQLiteMetadataStore(tmp_path / "state.sqlite3")
    store.record_heartbeat("cam-a")

    log = store.record_device_log("cam-a", level="error", message="sensor fault")

    assert log.camera_id == "cam-a"
    assert log.level == "error"
    assert log.message == "sensor fault"
    assert log.logged_at.tzinfo == timezone.utc


def test_record_device_log_rejects_invalid_level(tmp_path):
    store = SQLiteMetadataStore(tmp_path / "state.sqlite3")
    store.record_heartbeat("cam-a")

    with pytest.raises(ValueError, match="Invalid log level"):
        store.record_device_log("cam-a", level="verbose", message="test")


def test_record_device_log_uses_explicit_logged_at(tmp_path):
    store = SQLiteMetadataStore(tmp_path / "state.sqlite3")
    store.record_heartbeat("cam-a")
    explicit_time = datetime(2026, 1, 15, 10, 0, 0, tzinfo=timezone.utc)

    log = store.record_device_log("cam-a", level="info", message="boot", logged_at=explicit_time)

    assert log.logged_at == explicit_time


def test_record_device_log_covers_all_allowed_levels(tmp_path):
    store = SQLiteMetadataStore(tmp_path / "state.sqlite3")
    store.record_heartbeat("cam-a")

    for level in ALLOWED_LOG_LEVELS:
        log = store.record_device_log("cam-a", level=level, message=f"{level} message")
        assert log.level == level


def test_list_device_logs_returns_empty_for_unknown_camera(tmp_path):
    store = SQLiteMetadataStore(tmp_path / "state.sqlite3")

    logs = store.list_device_logs("nonexistent-cam")

    assert logs == []


def test_list_device_logs_returns_logs_in_descending_order(tmp_path):
    store = SQLiteMetadataStore(tmp_path / "state.sqlite3")
    store.record_heartbeat("cam-a")
    t1 = datetime(2026, 1, 1, 10, 0, tzinfo=timezone.utc)
    t2 = datetime(2026, 1, 1, 11, 0, tzinfo=timezone.utc)
    t3 = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)

    store.record_device_log("cam-a", level="info", message="first", logged_at=t1)
    store.record_device_log("cam-a", level="warning", message="second", logged_at=t2)
    store.record_device_log("cam-a", level="error", message="third", logged_at=t3)

    logs = store.list_device_logs("cam-a")

    assert [log.message for log in logs] == ["third", "second", "first"]


def test_list_device_logs_respects_limit(tmp_path):
    store = SQLiteMetadataStore(tmp_path / "state.sqlite3")
    store.record_heartbeat("cam-a")

    for i in range(10):
        store.record_device_log("cam-a", level="info", message=f"log {i}")

    logs = store.list_device_logs("cam-a", limit=3)

    assert len(logs) == 3


def test_list_camera_summaries_returns_empty_for_empty_store(tmp_path):
    store = SQLiteMetadataStore(tmp_path / "state.sqlite3")

    summaries = store.list_camera_summaries()

    assert summaries == []


def test_list_camera_summaries_latest_detection_at_is_set_when_detection_exists(tmp_path):
    store = SQLiteMetadataStore(tmp_path / "state.sqlite3")
    now = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)
    snapshot = store.record_snapshot(
        _stored_image(
            name="img.jpg",
            relative_path="cam-a/images/img.jpg",
            camera_id="cam-a",
            captured_at=now,
            stored_at=now,
        )
    )
    store.mark_snapshot_result(
        snapshot.id,
        detections=[Detection(label="person", score=0.9, x1=0.0, y1=0.0, x2=1.0, y2=1.0)],
    )

    summaries = store.list_camera_summaries()
    summary = next(s for s in summaries if s.camera.camera_id == "cam-a")

    assert summary.latest_detection_at is not None
    assert summary.latest_detection_at == now


def test_list_camera_summaries_returns_latest_snapshot_per_camera(tmp_path):
    store = SQLiteMetadataStore(tmp_path / "state.sqlite3")
    t1 = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)
    t2 = datetime(2026, 1, 1, 13, 0, tzinfo=timezone.utc)
    store.record_snapshot(
        _stored_image(
            name="old.jpg",
            relative_path="cam-a/images/old.jpg",
            camera_id="cam-a",
            captured_at=t1,
            stored_at=t1,
        )
    )
    store.record_snapshot(
        _stored_image(
            name="new.jpg",
            relative_path="cam-a/images/new.jpg",
            camera_id="cam-a",
            captured_at=t2,
            stored_at=t2,
        )
    )

    summaries = store.list_camera_summaries()
    summary = next(s for s in summaries if s.camera.camera_id == "cam-a")

    assert summary.latest_snapshot is not None
    assert summary.latest_snapshot.filename == "new.jpg"


def test_list_camera_summaries_camera_without_snapshots(tmp_path):
    store = SQLiteMetadataStore(tmp_path / "state.sqlite3")
    store.record_heartbeat("cam-a")

    summaries = store.list_camera_summaries()
    summary = next(s for s in summaries if s.camera.camera_id == "cam-a")

    assert summary.latest_snapshot is None
    assert summary.latest_detection_at is None


def test_list_camera_summaries_ordered_by_last_seen_at_desc(tmp_path):
    store = SQLiteMetadataStore(tmp_path / "state.sqlite3")
    t_early = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)
    t_late = datetime(2026, 1, 1, 13, 0, tzinfo=timezone.utc)
    store.record_heartbeat("cam-early", seen_at=t_early)
    store.record_heartbeat("cam-late", seen_at=t_late)

    summaries = store.list_camera_summaries()
    camera_ids = [s.camera.camera_id for s in summaries]

    assert camera_ids.index("cam-late") < camera_ids.index("cam-early")


def test_delete_camera_cascades_snapshots(tmp_path):
    store = SQLiteMetadataStore(tmp_path / "state.sqlite3")
    now = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)
    store.record_snapshot(
        _stored_image(
            name="img.jpg",
            relative_path="cam-a/images/img.jpg",
            camera_id="cam-a",
            captured_at=now,
            stored_at=now,
        )
    )

    store.delete_camera("cam-a")

    assert store.get_camera("cam-a") is None
    assert store.list_camera_snapshots("cam-a") == []


def test_migrate_schema_adds_cascade_to_existing_db(tmp_path):
    import sqlite3

    db_path = tmp_path / "legacy.sqlite3"
    conn = sqlite3.connect(db_path)
    conn.executescript(
        """
        PRAGMA foreign_keys = ON;
        CREATE TABLE cameras (
            camera_id TEXT PRIMARY KEY,
            display_name TEXT NOT NULL,
            expected_interval_seconds INTEGER NOT NULL,
            first_seen_at TEXT NOT NULL,
            last_seen_at TEXT NOT NULL,
            is_active INTEGER NOT NULL DEFAULT 1
        );
        CREATE TABLE snapshots (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            filename TEXT NOT NULL UNIQUE,
            relative_path TEXT NOT NULL UNIQUE,
            size INTEGER NOT NULL,
            camera_id TEXT,
            captured_at TEXT,
            stored_at TEXT NOT NULL,
            inference_status TEXT NOT NULL,
            inference_error TEXT,
            FOREIGN KEY(camera_id) REFERENCES cameras(camera_id)
        );
        INSERT INTO cameras VALUES ('cam-x', 'Cam X', 300, '2026-01-01T00:00:00+00:00', '2026-01-01T00:00:00+00:00', 1);
        INSERT INTO snapshots VALUES (NULL, 'img.jpg', 'cam-x/images/img.jpg', 100, 'cam-x', NULL, '2026-01-01T00:00:00+00:00', 'pending', NULL);
        """
    )
    conn.close()

    store = SQLiteMetadataStore(db_path)

    row = store._connection.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='snapshots'"
    ).fetchone()
    assert "ON DELETE CASCADE" in row["sql"].upper()
    assert store.get_camera("cam-x") is not None
    assert len(store.list_camera_snapshots("cam-x")) == 1


def test_metadata_store_enables_wal_mode(tmp_path):
    store = SQLiteMetadataStore(tmp_path / "wal.sqlite3")
    row = store._connection.execute("PRAGMA journal_mode").fetchone()
    assert row[0] == "wal"


def test_desired_config_ack_rejects_stale_version_atomically(tmp_path):
    store = SQLiteMetadataStore(tmp_path / "state.sqlite3")
    first = store.upsert_desired_config(
        "cam-a",
        {
            "capture_interval_seconds": 300,
            "frame_size": "SVGA",
            "jpeg_quality": 12,
            "vflip": False,
            "hmirror": False,
            "restart_requested": True,
            "ota_update_requested": True,
            "ota_window_seconds": 120,
        },
    )
    second = store.upsert_desired_config(
        "cam-a",
        {
            **first.config,
            "frame_size": "VGA",
            "restart_requested": True,
            "ota_update_requested": True,
        },
    )

    with pytest.raises(DesiredConfigVersionConflict) as exc_info:
        store.record_desired_config_ack(
            "cam-a",
            version=first.version,
            applied=True,
            message="stale ack",
        )

    current = store.get_desired_config("cam-a")
    assert exc_info.value.current_version == second.version
    assert current is not None
    assert current.version == second.version
    assert current.last_acknowledged_version is None
    assert current.config["restart_requested"] is True
    assert current.config["ota_update_requested"] is True


def _stored_video(
    *,
    name: str,
    relative_path: str,
    camera_id: str,
    captured_at: datetime | None,
    stored_at: datetime,
    duration_ms: int | None = 5000,
    sha256: str = "a" * 64,
) -> StoredVideo:
    return StoredVideo(
        name=name,
        relative_path=relative_path,
        size=456,
        camera_id=camera_id,
        captured_at=captured_at,
        stored_at=stored_at,
        duration_ms=duration_ms,
        content_type="video/mp4",
        sha256=sha256,
    )


def test_metadata_store_creates_videos_table(tmp_path):
    store = SQLiteMetadataStore(tmp_path / "state.sqlite3")

    row = store._connection.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='videos'"
    ).fetchone()

    assert row is not None
    assert "processing_status" in row["sql"]
    assert "ON DELETE CASCADE" in row["sql"].upper()


def test_metadata_store_records_and_lists_videos(tmp_path):
    store = SQLiteMetadataStore(tmp_path / "state.sqlite3")
    older = datetime(2026, 6, 1, 12, 0, tzinfo=timezone.utc)
    newer = datetime(2026, 6, 1, 12, 1, tzinfo=timezone.utc)
    old_video = store.record_video(
        _stored_video(
            name="video_old.mp4",
            relative_path="cam-a/videos/video_old.mp4",
            camera_id="cam-a",
            captured_at=older,
            stored_at=older,
        )
    )
    new_video = store.record_video(
        _stored_video(
            name="video_new.mp4",
            relative_path="cam-a/videos/video_new.mp4",
            camera_id="cam-a",
            captured_at=newer,
            stored_at=newer,
            duration_ms=None,
        )
    )

    videos = store.list_camera_videos("cam-a")

    assert [video.id for video in videos] == [new_video.id, old_video.id]
    assert videos[0].duration_ms is None
    assert videos[0].processing_status == "skipped"
    assert videos[0].content_type == "video/mp4"
    assert videos[0].sha256 == "a" * 64
    assert store.get_camera("cam-a") is not None


def test_metadata_store_deletes_videos_by_relative_path(tmp_path):
    store = SQLiteMetadataStore(tmp_path / "state.sqlite3")
    now = datetime(2026, 6, 1, 12, 0, tzinfo=timezone.utc)
    delete_me = store.record_video(
        _stored_video(
            name="delete.mp4",
            relative_path="cam-a/videos/delete.mp4",
            camera_id="cam-a",
            captured_at=now,
            stored_at=now,
        )
    )
    keep_me = store.record_video(
        _stored_video(
            name="keep.mp4",
            relative_path="cam-a/videos/keep.mp4",
            camera_id="cam-a",
            captured_at=now,
            stored_at=now,
        )
    )

    deleted = store.delete_videos_by_relative_paths([delete_me.relative_path])

    assert deleted == 1
    assert [video.id for video in store.list_camera_videos("cam-a")] == [keep_me.id]


def test_delete_camera_cascades_videos(tmp_path):
    store = SQLiteMetadataStore(tmp_path / "state.sqlite3")
    now = datetime(2026, 6, 1, 12, 0, tzinfo=timezone.utc)
    store.record_video(
        _stored_video(
            name="video.mp4",
            relative_path="cam-a/videos/video.mp4",
            camera_id="cam-a",
            captured_at=now,
            stored_at=now,
        )
    )

    store.delete_camera("cam-a")

    assert store.get_camera("cam-a") is None
    assert store.list_camera_videos("cam-a") == []


def test_metadata_store_gets_video_by_id_and_camera(tmp_path):
    store = SQLiteMetadataStore(tmp_path / "state.sqlite3")
    now = datetime(2026, 6, 1, 12, 0, tzinfo=timezone.utc)
    record = store.record_video(
        _stored_video(
            name="video.mp4",
            relative_path="cam-a/videos/video.mp4",
            camera_id="cam-a",
            captured_at=now,
            stored_at=now,
        )
    )

    assert store.get_video(record.id, camera_id="cam-a") == record
    assert store.get_video(record.id, camera_id="other-cam") is None
    assert store.get_video(record.id + 1, camera_id="cam-a") is None
