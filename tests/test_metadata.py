from __future__ import annotations

from datetime import datetime, timedelta, timezone

from script.metadata import SQLiteMetadataStore
from script.storage import StoredImage


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
