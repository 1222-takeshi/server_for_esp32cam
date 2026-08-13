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


def test_sync_storage_index_does_not_select_entire_snapshots_table(tmp_path):
    store = SQLiteMetadataStore(tmp_path / "state.sqlite3")
    stored_at = datetime(2026, 3, 11, 12, 0, tzinfo=timezone.utc)

    # Seed a large pre-existing snapshots table (bigger than a single sync
    # batch would ever be), to stand in for a long-lived deployment with
    # millions of rows.
    seed_images = [
        _stored_image(
            name=f"seed-{i}.jpg",
            relative_path=f"cam-a/images/seed-{i}.jpg",
            camera_id="cam-a",
            captured_at=stored_at,
            stored_at=stored_at,
        )
        for i in range(1200)
    ]
    store.sync_storage_index(seed_images)
    assert len(store.list_recent_snapshots(limit=10_000)) == 1200

    executed_sql: list[str] = []
    conn = store._get_connection()
    conn.set_trace_callback(executed_sql.append)
    try:
        # Re-sync a mix of already-indexed files and brand new ones, large
        # enough to span multiple lookup chunks.
        resync_images = seed_images[:900] + [
            _stored_image(
                name=f"new-{i}.jpg",
                relative_path=f"cam-a/images/new-{i}.jpg",
                camera_id="cam-a",
                captured_at=stored_at,
                stored_at=stored_at,
            )
            for i in range(50)
        ]
        store.sync_storage_index(resync_images)
    finally:
        conn.set_trace_callback(None)

    # The whole point of the fix: no query should select every row of the
    # snapshots table unconditionally.
    unfiltered_selects = [
        sql
        for sql in executed_sql
        if "SELECT" in sql.upper() and "FROM snapshots" in sql and "WHERE" not in sql.upper()
    ]
    assert unfiltered_selects == []

    snapshots = store.list_recent_snapshots(limit=10_000)
    assert len(snapshots) == 1250
    filenames = {snapshot.filename for snapshot in snapshots}
    assert all(f"seed-{i}.jpg" in filenames for i in range(1200))
    assert all(f"new-{i}.jpg" in filenames for i in range(50))


def test_sync_storage_index_skips_already_indexed_filenames(tmp_path):
    store = SQLiteMetadataStore(tmp_path / "state.sqlite3")
    initial = datetime(2026, 3, 11, 12, 0, tzinfo=timezone.utc)
    brand_new_time = datetime(2026, 3, 11, 12, 30, tzinfo=timezone.utc)
    stale_resync_time = datetime(2026, 3, 11, 13, 0, tzinfo=timezone.utc)

    store.sync_storage_index(
        [
            _stored_image(
                name="existing.jpg",
                relative_path="cam-a/images/existing.jpg",
                camera_id="cam-a",
                captured_at=initial,
                stored_at=initial,
            )
        ]
    )

    # Re-sync the same filename with a much later timestamp, plus one
    # genuinely new file with a timestamp in between. The already-indexed
    # file must be left untouched (not re-inserted/re-registered, so its
    # later timestamp must NOT bump camera.last_seen_at), while the new file
    # must be added and does bump last_seen_at to its own timestamp.
    store.sync_storage_index(
        [
            _stored_image(
                name="existing.jpg",
                relative_path="cam-a/images/existing.jpg",
                camera_id="cam-a",
                captured_at=stale_resync_time,
                stored_at=stale_resync_time,
            ),
            _stored_image(
                name="brand-new.jpg",
                relative_path="cam-a/images/brand-new.jpg",
                camera_id="cam-a",
                captured_at=brand_new_time,
                stored_at=brand_new_time,
            ),
        ]
    )

    snapshots = {s.filename: s for s in store.list_camera_snapshots("cam-a")}
    assert set(snapshots) == {"existing.jpg", "brand-new.jpg"}
    assert snapshots["existing.jpg"].stored_at == initial

    camera = store.get_camera("cam-a")
    assert camera is not None
    assert camera.last_seen_at == brand_new_time


def test_sync_storage_index_hydrates_metadata_only_for_new_images(tmp_path):
    store = SQLiteMetadataStore(tmp_path / "state.sqlite3")
    initial = datetime(2026, 3, 11, 12, 0, tzinfo=timezone.utc)
    new_time = datetime(2026, 3, 11, 12, 30, tzinfo=timezone.utc)

    store.sync_storage_index(
        [
            _stored_image(
                name="existing.jpg",
                relative_path="cam-a/images/existing.jpg",
                camera_id="cam-a",
                captured_at=initial,
                stored_at=initial,
            )
        ]
    )

    # Simulate the caller listing images with include_metadata=False: the
    # StoredImage objects carry no device_id/captured_at, and hydration must
    # be sourced from `hydrate_metadata` instead.
    hydrate_calls: list[str] = []

    def hydrate(relative_path: str) -> tuple[str | None, datetime | None]:
        hydrate_calls.append(relative_path)
        return "cam-a", new_time

    store.sync_storage_index(
        [
            _stored_image(
                name="existing.jpg",
                relative_path="cam-a/images/existing.jpg",
                camera_id=None,
                captured_at=None,
                stored_at=initial,
            ),
            _stored_image(
                name="brand-new.jpg",
                relative_path="cam-a/images/brand-new.jpg",
                camera_id=None,
                captured_at=None,
                stored_at=new_time,
            ),
        ],
        hydrate_metadata=hydrate,
    )

    # hydrate_metadata must only be called for the genuinely new image, not
    # for the already-indexed one.
    assert hydrate_calls == ["cam-a/images/brand-new.jpg"]

    snapshot = next(
        s for s in store.list_camera_snapshots("cam-a") if s.filename == "brand-new.jpg"
    )
    assert snapshot.captured_at == new_time

    camera = store.get_camera("cam-a")
    assert camera is not None
    assert camera.last_seen_at == new_time


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


def test_list_camera_snapshots_paginates_stably_with_duplicate_timestamps(tmp_path):
    # ORDER BY stored_at DESC alone is not a stable sort when multiple rows
    # share the same stored_at (e.g. an upload burst or a backfill via
    # sync_storage_index): without a tie-break, LIMIT/OFFSET pagination can
    # duplicate or skip rows across pages because SQLite's order for ties is
    # unspecified.
    store = SQLiteMetadataStore(tmp_path / "state.sqlite3")
    stored_at = datetime(2026, 3, 11, 12, 0, tzinfo=timezone.utc)
    first = store.record_snapshot(
        _stored_image(
            name="a.jpg",
            relative_path="cam-a/images/a.jpg",
            camera_id="cam-a",
            captured_at=stored_at,
            stored_at=stored_at,
        )
    )
    second = store.record_snapshot(
        _stored_image(
            name="b.jpg",
            relative_path="cam-a/images/b.jpg",
            camera_id="cam-a",
            captured_at=stored_at,
            stored_at=stored_at,
        )
    )

    page1 = store.list_camera_snapshots("cam-a", limit=1, offset=0)
    page2 = store.list_camera_snapshots("cam-a", limit=1, offset=1)

    assert [s.id for s in page1] == [second.id]
    assert [s.id for s in page2] == [first.id]


def test_list_snapshots_supports_id_cursor(tmp_path):
    store = SQLiteMetadataStore(tmp_path / "state.sqlite3")
    stored_at = datetime(2026, 3, 11, 12, 0, tzinfo=timezone.utc)
    snapshots = [
        store.record_snapshot(
            _stored_image(
                name=f"{index}.jpg",
                relative_path=f"cam-a/images/{index}.jpg",
                camera_id="cam-a",
                captured_at=stored_at + timedelta(seconds=index),
                stored_at=stored_at + timedelta(seconds=index),
            )
        )
        for index in range(3)
    ]

    first_page = store.list_camera_snapshots("cam-a", limit=1)
    cursor = (first_page[0].stored_at.isoformat(), first_page[0].id)
    second_page = store.list_camera_snapshots("cam-a", limit=1, before=cursor)
    recent_page = store.list_recent_snapshots(limit=1, before=cursor)

    assert [snapshot.id for snapshot in first_page] == [snapshots[2].id]
    assert [snapshot.id for snapshot in second_page] == [snapshots[1].id]
    assert [snapshot.id for snapshot in recent_page] == [snapshots[1].id]


def test_list_snapshots_cursor_handles_backfilled_stored_at(tmp_path):
    store = SQLiteMetadataStore(tmp_path / "state.sqlite3")
    base = datetime(2026, 3, 11, 12, 0, tzinfo=timezone.utc)
    newest = store.record_snapshot(
        _stored_image(
            name="newest.jpg",
            relative_path="cam-a/images/newest.jpg",
            camera_id="cam-a",
            captured_at=base + timedelta(seconds=10),
            stored_at=base + timedelta(seconds=10),
        )
    )
    oldest = store.record_snapshot(
        _stored_image(
            name="oldest.jpg",
            relative_path="cam-a/images/oldest.jpg",
            camera_id="cam-a",
            captured_at=base,
            stored_at=base,
        )
    )
    middle = store.record_snapshot(
        _stored_image(
            name="middle.jpg",
            relative_path="cam-a/images/middle.jpg",
            camera_id="cam-a",
            captured_at=base + timedelta(seconds=5),
            stored_at=base + timedelta(seconds=5),
        )
    )

    first_page = store.list_recent_snapshots(limit=1)
    cursor = (first_page[0].stored_at.isoformat(), first_page[0].id)
    remaining = store.list_recent_snapshots(limit=10, before=cursor)

    assert first_page[0].id == newest.id
    assert [snapshot.id for snapshot in remaining] == [middle.id, oldest.id]


def test_list_detections_paginates_stably_with_duplicate_timestamps(tmp_path):
    store = SQLiteMetadataStore(tmp_path / "state.sqlite3")
    stored_at = datetime(2026, 3, 11, 12, 0, tzinfo=timezone.utc)
    first = store.record_snapshot(
        _stored_image(
            name="a.jpg",
            relative_path="cam-a/images/a.jpg",
            camera_id="cam-a",
            captured_at=stored_at,
            stored_at=stored_at,
        )
    )
    second = store.record_snapshot(
        _stored_image(
            name="b.jpg",
            relative_path="cam-a/images/b.jpg",
            camera_id="cam-a",
            captured_at=stored_at,
            stored_at=stored_at,
        )
    )
    store.mark_snapshot_result(
        first.id, detections=[Detection(label="person", score=0.9, x1=0, y1=0, x2=1, y2=1)]
    )
    store.mark_snapshot_result(
        second.id, detections=[Detection(label="cat", score=0.8, x1=0, y1=0, x2=1, y2=1)]
    )

    page1 = store.list_detections(camera_id="cam-a", limit=1, offset=0)
    page2 = store.list_detections(camera_id="cam-a", limit=1, offset=1)

    assert [d["filename"] for d in page1] == [second.filename]
    assert [d["filename"] for d in page2] == [first.filename]


def test_list_detections_supports_composite_cursor(tmp_path):
    store = SQLiteMetadataStore(tmp_path / "state.sqlite3")
    stored_at = datetime(2026, 3, 11, 12, 0, tzinfo=timezone.utc)
    snapshots = []
    for index in range(3):
        snapshot = store.record_snapshot(
            _stored_image(
                name=f"{index}.jpg",
                relative_path=f"cam-a/images/{index}.jpg",
                camera_id="cam-a",
                captured_at=stored_at + timedelta(seconds=index),
                stored_at=stored_at + timedelta(seconds=index),
            )
        )
        store.mark_snapshot_result(
            snapshot.id,
            detections=[Detection(label="person", score=0.9, x1=0, y1=0, x2=1, y2=1)],
        )
        snapshots.append(snapshot)

    first_page = store.list_detections(limit=1)
    cursor = (first_page[0]["stored_at"], first_page[0]["snapshot_id"], first_page[0]["detection_id"])
    second_page = store.list_detections(limit=1, before=cursor)

    assert [row["filename"] for row in first_page] == [snapshots[2].filename]
    assert [row["filename"] for row in second_page] == [snapshots[1].filename]


def test_list_pending_snapshot_ids_returns_all_by_default(tmp_path):
    # A previous default of limit=100 silently dropped any pending snapshot
    # beyond the first 100 (oldest-first) from the one-time startup recovery
    # sweep in InferenceService.start(), orphaning them forever since nothing
    # else re-scans for pending work after boot.
    store = SQLiteMetadataStore(tmp_path / "state.sqlite3")
    stored_at = datetime(2026, 3, 11, 12, 0, tzinfo=timezone.utc)
    ids = []
    for i in range(105):
        snapshot = store.record_snapshot(
            _stored_image(
                name=f"img-{i}.jpg",
                relative_path=f"cam-a/images/img-{i}.jpg",
                camera_id="cam-a",
                captured_at=stored_at + timedelta(seconds=i),
                stored_at=stored_at + timedelta(seconds=i),
            )
        )
        ids.append(snapshot.id)

    pending = store.list_pending_snapshot_ids()

    assert len(pending) == 105
    assert set(pending) == set(ids)


def test_list_pending_snapshot_ids_respects_explicit_limit(tmp_path):
    store = SQLiteMetadataStore(tmp_path / "state.sqlite3")
    stored_at = datetime(2026, 3, 11, 12, 0, tzinfo=timezone.utc)
    for i in range(5):
        store.record_snapshot(
            _stored_image(
                name=f"img-{i}.jpg",
                relative_path=f"cam-a/images/img-{i}.jpg",
                camera_id="cam-a",
                captured_at=stored_at + timedelta(seconds=i),
                stored_at=stored_at + timedelta(seconds=i),
            )
        )

    pending = store.list_pending_snapshot_ids(limit=2)

    assert len(pending) == 2


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


def test_list_camera_last_seen_returns_empty_for_empty_store(tmp_path):
    store = SQLiteMetadataStore(tmp_path / "state.sqlite3")

    assert store.list_camera_last_seen() == []


def test_list_camera_last_seen_matches_summaries_last_seen(tmp_path):
    store = SQLiteMetadataStore(tmp_path / "state.sqlite3")
    now = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)
    older = now - timedelta(hours=1)

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
            captured_at=older,
            stored_at=older,
        )
    )

    last_seen = dict(store.list_camera_last_seen())
    summary_last_seen = {
        s.camera.camera_id: s.camera.last_seen_at for s in store.list_camera_summaries()
    }

    assert last_seen == summary_last_seen
    assert last_seen.keys() == {"cam-a", "cam-b"}


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


def test_mark_snapshot_result_stores_multiple_detections_in_one_call(tmp_path):
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
        detections=[
            Detection(
                label="person",
                score=0.9,
                x1=0.0,
                y1=0.0,
                x2=1.0,
                y2=1.0,
                track_id=7,
            ),
            Detection(label="cat", score=0.5, x1=0.1, y1=0.1, x2=0.9, y2=0.9),
            Detection(label="dog", score=0.4, x1=0.2, y1=0.2, x2=0.8, y2=0.8),
        ],
    )

    stored = store.get_snapshot(snapshot.id)

    assert stored is not None
    assert sorted(d.label for d in stored.detections) == ["cat", "dog", "person"]
    assert next(d for d in stored.detections if d.label == "person").track_id == 7

    # Re-marking with no detections must clear the previous ones without erroring
    # on an empty executemany() call.
    store.mark_snapshot_result(snapshot.id, detections=[])

    assert store.get_snapshot(snapshot.id).detections == ()


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


def test_list_camera_summaries_spans_multiple_sqlite_in_clause_chunks(tmp_path):
    # SQLite has a default limit on bound parameters per statement
    # (SQLITE_MAX_VARIABLE_NUMBER). list_camera_summaries() binds every
    # camera_id into IN (...) lookups for the latest snapshot/detection per
    # camera, so a fleet larger than one chunk (_SQLITE_IN_CLAUSE_CHUNK_SIZE
    # == 500) must still return correct, complete results instead of
    # raising sqlite3.OperationalError or silently dropping cameras from
    # the trailing chunk.
    store = SQLiteMetadataStore(tmp_path / "state.sqlite3")
    stored_at = datetime(2026, 3, 11, 12, 0, tzinfo=timezone.utc)
    camera_count = 650

    for i in range(camera_count):
        store.record_heartbeat(f"cam-{i:04d}", seen_at=stored_at)

    # Give a handful of cameras -- including some past the first 500-item
    # chunk boundary -- an actual snapshot (with a detection on one of
    # them) so the merge across chunks is exercised for both queries, not
    # just the camera roster itself.
    snapshot_camera_ids = [0, 250, 499, 500, 501, 649]
    for i in snapshot_camera_ids:
        snapshot = store.record_snapshot(
            _stored_image(
                name=f"cam-{i:04d}.jpg",
                relative_path=f"cam-{i:04d}/images/cam-{i:04d}.jpg",
                camera_id=f"cam-{i:04d}",
                captured_at=stored_at,
                stored_at=stored_at,
            )
        )
        if i in (499, 649):
            store.mark_snapshot_result(
                snapshot.id,
                detections=[
                    Detection(label="person", score=0.9, x1=0.1, y1=0.1, x2=0.5, y2=0.5)
                ],
            )

    summaries = store.list_camera_summaries()
    assert len(summaries) == camera_count

    summary_by_camera = {s.camera.camera_id: s for s in summaries}
    for i in snapshot_camera_ids:
        camera_id = f"cam-{i:04d}"
        summary = summary_by_camera[camera_id]
        assert summary.latest_snapshot is not None
        assert summary.latest_snapshot.filename == f"cam-{i:04d}.jpg"
        if i in (499, 649):
            assert summary.latest_detection_at is not None
        else:
            assert summary.latest_detection_at is None

    # A camera with no snapshot at all (most of the 650) still round-trips.
    untouched = summary_by_camera["cam-0300"]
    assert untouched.latest_snapshot is None
    assert untouched.latest_detection_at is None


def test_delete_snapshots_by_relative_paths_spans_multiple_sqlite_in_clause_chunks(
    tmp_path,
):
    store = SQLiteMetadataStore(tmp_path / "state.sqlite3")
    stored_at = datetime(2026, 3, 11, 12, 0, tzinfo=timezone.utc)
    count = 650

    relative_paths = []
    for i in range(count):
        stored = store.record_snapshot(
            _stored_image(
                name=f"bulk-{i:04d}.jpg",
                relative_path=f"cam-a/images/bulk-{i:04d}.jpg",
                camera_id="cam-a",
                captured_at=stored_at,
                stored_at=stored_at,
            )
        )
        relative_paths.append(stored.relative_path)

    deleted = store.delete_snapshots_by_relative_paths(relative_paths)

    assert deleted == count
    assert store.total_snapshot_count() == 0


def test_delete_videos_by_relative_paths_spans_multiple_sqlite_in_clause_chunks(
    tmp_path,
):
    store = SQLiteMetadataStore(tmp_path / "state.sqlite3")
    stored_at = datetime(2026, 3, 11, 12, 0, tzinfo=timezone.utc)
    count = 650

    relative_paths = []
    for i in range(count):
        stored = store.record_video(
            StoredVideo(
                name=f"bulk-{i:04d}.mp4",
                relative_path=f"cam-a/videos/bulk-{i:04d}.mp4",
                size=123,
                camera_id="cam-a",
                captured_at=stored_at,
                stored_at=stored_at,
                duration_ms=1000,
                content_type="video/mp4",
                sha256="0" * 64,
            )
        )
        relative_paths.append(stored.relative_path)

    deleted = store.delete_videos_by_relative_paths(relative_paths)

    assert deleted == count
    assert store.list_videos(limit=1) == []


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

    row = (
        store._get_connection()
        .execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='snapshots'")
        .fetchone()
    )
    assert "ON DELETE CASCADE" in row["sql"].upper()
    assert store.get_camera("cam-x") is not None
    assert len(store.list_camera_snapshots("cam-x")) == 1


def test_migrate_schema_adds_detection_track_id_to_existing_db(tmp_path):
    import sqlite3

    db_path = tmp_path / "legacy-detections.sqlite3"
    conn = sqlite3.connect(db_path)
    conn.executescript(
        """
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
            inference_error TEXT
        );
        CREATE TABLE detections (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            snapshot_id INTEGER NOT NULL,
            label TEXT NOT NULL,
            score REAL NOT NULL,
            x1 REAL NOT NULL,
            y1 REAL NOT NULL,
            x2 REAL NOT NULL,
            y2 REAL NOT NULL
        );
        INSERT INTO cameras VALUES ('cam-a', 'Cam A', 300, '2026-01-01T00:00:00+00:00', '2026-01-01T00:00:00+00:00', 1);
        INSERT INTO snapshots VALUES (1, 'img.jpg', 'cam-a/images/img.jpg', 100, 'cam-a', NULL, '2026-01-01T00:00:00+00:00', 'succeeded', NULL);
        INSERT INTO detections VALUES (1, 1, 'person', 0.9, 0.1, 0.1, 0.4, 0.4);
        """
    )
    conn.close()

    store = SQLiteMetadataStore(db_path)

    columns = store._get_connection().execute("PRAGMA table_info(detections)").fetchall()
    assert "track_id" in {column["name"] for column in columns}
    snapshot = store.get_snapshot(1)
    assert snapshot is not None
    assert snapshot.detections[0].track_id is None


def test_metadata_store_enables_wal_mode(tmp_path):
    store = SQLiteMetadataStore(tmp_path / "wal.sqlite3")
    row = store._get_connection().execute("PRAGMA journal_mode").fetchone()
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

    row = (
        store._get_connection()
        .execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='videos'")
        .fetchone()
    )

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


# ---------------------------------------------------------------------------
# Retention candidates sourced from SQLite instead of a filesystem rglob()
# walk (#200 / #225 / #246 / #260).
# ---------------------------------------------------------------------------


def test_list_snapshot_retention_candidates_orders_oldest_first(tmp_path):
    store = SQLiteMetadataStore(tmp_path / "state.sqlite3")
    older = datetime(2026, 1, 1, tzinfo=timezone.utc)
    newer = datetime(2026, 1, 2, tzinfo=timezone.utc)
    store.record_snapshot(
        _stored_image(
            name="new.jpg",
            relative_path="cam-a/images/new.jpg",
            camera_id="cam-a",
            captured_at=newer,
            stored_at=newer,
        )
    )
    store.record_snapshot(
        _stored_image(
            name="old.jpg",
            relative_path="cam-a/images/old.jpg",
            camera_id="cam-a",
            captured_at=older,
            stored_at=older,
        )
    )

    candidates, total_bytes = store.list_snapshot_retention_candidates()

    assert [relative_path for relative_path, _size, _stored_at in candidates] == [
        "cam-a/images/old.jpg",
        "cam-a/images/new.jpg",
    ]
    assert total_bytes == 246  # two 123-byte snapshots (see _stored_image())


def test_list_snapshot_retention_candidates_excludes_paths_but_keeps_their_bytes(tmp_path):
    store = SQLiteMetadataStore(tmp_path / "state.sqlite3")
    stored_at = datetime(2026, 1, 1, tzinfo=timezone.utc)
    store.record_snapshot(
        _stored_image(
            name="excluded.jpg",
            relative_path="cam-a/images/excluded.jpg",
            camera_id="cam-a",
            captured_at=stored_at,
            stored_at=stored_at,
        )
    )
    store.record_snapshot(
        _stored_image(
            name="kept.jpg",
            relative_path="cam-a/images/kept.jpg",
            camera_id="cam-a",
            captured_at=stored_at,
            stored_at=stored_at,
        )
    )

    candidates, total_bytes = store.list_snapshot_retention_candidates(
        exclude_relative_paths={"cam-a/images/excluded.jpg"}
    )

    # The excluded (just-uploaded) file must never be a deletion candidate,
    # but its bytes still count toward total_bytes for max_bytes accounting,
    # matching what a full filesystem listing would have summed.
    assert [relative_path for relative_path, _size, _stored_at in candidates] == [
        "cam-a/images/kept.jpg"
    ]
    assert total_bytes == 246


def test_list_snapshot_retention_candidates_empty_store(tmp_path):
    store = SQLiteMetadataStore(tmp_path / "state.sqlite3")

    candidates, total_bytes = store.list_snapshot_retention_candidates()

    assert candidates == []
    assert total_bytes == 0


def test_list_video_retention_candidates_orders_oldest_first(tmp_path):
    store = SQLiteMetadataStore(tmp_path / "state.sqlite3")
    older = datetime(2026, 1, 1, tzinfo=timezone.utc)
    newer = datetime(2026, 1, 2, tzinfo=timezone.utc)
    store.record_video(
        _stored_video(
            name="new.mp4",
            relative_path="cam-a/videos/new.mp4",
            camera_id="cam-a",
            captured_at=newer,
            stored_at=newer,
        )
    )
    store.record_video(
        _stored_video(
            name="old.mp4",
            relative_path="cam-a/videos/old.mp4",
            camera_id="cam-a",
            captured_at=older,
            stored_at=older,
        )
    )

    candidates, total_bytes = store.list_video_retention_candidates()

    assert [relative_path for relative_path, _size, _stored_at in candidates] == [
        "cam-a/videos/old.mp4",
        "cam-a/videos/new.mp4",
    ]
    assert total_bytes == 912  # two 456-byte videos (see _stored_video())


def test_list_video_retention_candidates_excludes_paths_but_keeps_their_bytes(tmp_path):
    store = SQLiteMetadataStore(tmp_path / "state.sqlite3")
    stored_at = datetime(2026, 1, 1, tzinfo=timezone.utc)
    store.record_video(
        _stored_video(
            name="excluded.mp4",
            relative_path="cam-a/videos/excluded.mp4",
            camera_id="cam-a",
            captured_at=stored_at,
            stored_at=stored_at,
        )
    )
    store.record_video(
        _stored_video(
            name="kept.mp4",
            relative_path="cam-a/videos/kept.mp4",
            camera_id="cam-a",
            captured_at=stored_at,
            stored_at=stored_at,
        )
    )

    candidates, total_bytes = store.list_video_retention_candidates(
        exclude_relative_paths={"cam-a/videos/excluded.mp4"}
    )

    assert [relative_path for relative_path, _size, _stored_at in candidates] == [
        "cam-a/videos/kept.mp4"
    ]
    assert total_bytes == 912
